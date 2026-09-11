"""forecast/costs.py: the operating-cost file and the cash-view arithmetic.

EVERY NUMBER HERE IS MADE UP (round, obviously fake), and so is every item
name. The operator's real costs are commercially sensitive and must never
appear in a committed file.
"""
import copy
import datetime as dt
import hashlib
import math
import os
import shutil
import subprocess
import sys
import types

import pytest
import yaml

from forecast import costs as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _valid():
    return {
        "schema": 1,
        "fixed_monthly": 1000.0,
        "oxygen_per_kg_feed": 0.5,
        "chemicals_per_kg_feed": 0.25,
        "feed_shipping_per_kg": 0.1,
        "egg_price": 0.01,
        "feed_prices": {
            "Starter 0.5": {"item": "Acme Crumble No.1", "price_per_kg": 4.0},
            "Grower 9.0": {"item": "Acme Grower Pellet 9mm",
                           "price_per_kg": 2.0},
        },
    }


def _write(d, text):
    (d / C.COSTS_FILE).write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# load: "not set" vs malformed
# --------------------------------------------------------------------------- #
def test_missing_file_is_not_set(tmp_path):
    assert C.load_costs(tmp_path) is None
    assert C.costs_sig(tmp_path) == "none"


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n"])
def test_empty_file_is_not_set(tmp_path, text):
    _write(tmp_path, text)
    assert C.load_costs(tmp_path) is None


def _drop(k):
    def f(d):
        del d[k]
    return f


def _set(k, v):
    def f(d):
        d[k] = v
    return f


def _price(name, entry):
    def f(d):
        d["feed_prices"][name] = entry
    return f


MALFORMED = [
    ("missing key", _drop("egg_price"), "egg_price"),
    ("missing shipping", _drop("feed_shipping_per_kg"), "feed_shipping_per_kg"),
    ("unknown key", _set("rent", 5.0), "rent"),
    ("negative", _set("oxygen_per_kg_feed", -1.0), "oxygen_per_kg_feed"),
    ("NaN", _set("chemicals_per_kg_feed", float("nan")),
     "chemicals_per_kg_feed"),
    ("inf", _set("feed_shipping_per_kg", float("inf")), "feed_shipping_per_kg"),
    ("string", _set("fixed_monthly", "lots"), "fixed_monthly"),
    ("numeric string", _set("egg_price", "0.01"), "egg_price"),
    ("bool", _set("fixed_monthly", True), "fixed_monthly"),
    ("null", _set("fixed_monthly", None), "fixed_monthly"),
    ("feed_prices list", _set("feed_prices", ["Grower 9.0", 2.0]),
     "feed_prices"),
    ("feed_prices null", _set("feed_prices", None), "feed_prices"),
    ("blank feed type", _price("   ", {"item": "", "price_per_kg": 1.0}),
     "blank"),
    ("no price_per_kg", _price("Grower 9.0", {"item": "Acme Grower"}),
     "price_per_kg"),
    ("bare number entry", _price("Grower 9.0", 2.0), "Grower 9.0"),
    ("negative price", _price("Grower 9.0", {"item": "", "price_per_kg": -2}),
     "price_per_kg"),
    ("NaN price", _price("Grower 9.0", {"item": "", "price_per_kg": math.nan}),
     "price_per_kg"),
    ("unknown entry key",
     _price("Grower 9.0", {"item": "", "price_per_kg": 2.0, "unit": "kg"}),
     "unit"),
    ("item not text", _price("Grower 9.0", {"item": 123, "price_per_kg": 2.0}),
     "item"),
    ("no item", _price("Grower 9.0", {"price_per_kg": 2.0}), "item"),
    ("huge int", _set("fixed_monthly", 10 ** 400), "fixed_monthly"),
    ("schema 2", _set("schema", 2), "schema"),
    ("schema true", _set("schema", True), "schema"),
]


@pytest.mark.parametrize("label,mutate,field", MALFORMED,
                         ids=[m[0] for m in MALFORMED])
def test_malformed_raises_naming_the_field(tmp_path, label, mutate, field):
    d = _valid()
    mutate(d)
    with pytest.raises(ValueError, match="costs.yaml") as ei:
        C.validate_costs(d)
    assert field in str(ei.value)
    # load_costs refuses the same file, with the same field named.
    _write(tmp_path, yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
    with pytest.raises(ValueError) as ei2:
        C.load_costs(tmp_path)
    assert field in str(ei2.value)


def test_every_key_is_required_nothing_defaulted():
    for k in _valid():
        d = _valid()
        del d[k]
        with pytest.raises(ValueError, match=k):
            C.validate_costs(d)


@pytest.mark.parametrize("text", ["- a\n- b\n", "just words\n", "42\n"])
def test_non_mapping_file_raises(tmp_path, text):
    _write(tmp_path, text)
    with pytest.raises(ValueError, match="costs.yaml"):
        C.load_costs(tmp_path)


def test_invalid_yaml_raises_valueerror(tmp_path):
    _write(tmp_path, "fixed_monthly: [1,\n")
    with pytest.raises(ValueError, match="costs.yaml"):
        C.load_costs(tmp_path)


# --------------------------------------------------------------------------- #
# save
# --------------------------------------------------------------------------- #
def test_save_load_round_trip(tmp_path):
    src = _valid()
    src["egg_price"] = 0.00002          # dumps in exponent form; must reload
    C.save_costs(tmp_path, src)
    assert sorted(os.listdir(tmp_path)) == [C.COSTS_FILE]
    got = C.load_costs(tmp_path)
    assert got == C.validate_costs(src)
    assert got["egg_price"] == 0.00002
    text = (tmp_path / C.COSTS_FILE).read_text(encoding="utf-8")
    head = text.splitlines()[0]
    assert head.startswith("#")
    assert "git" in text.split("schema:")[0]
    assert "Configure" in text.split("schema:")[0]


def test_item_names_round_trip(tmp_path):
    src = _valid()
    src["feed_prices"]["Finisher 12.0"] = {"item": None, "price_per_kg": 1.0}
    src["feed_prices"]["Tiny 0.1"] = {"item": "", "price_per_kg": 9.0}
    C.save_costs(tmp_path, src)
    got = C.load_costs(tmp_path)["feed_prices"]
    assert got["Starter 0.5"]["item"] == "Acme Crumble No.1"
    assert got["Grower 9.0"]["item"] == "Acme Grower Pellet 9mm"
    assert got["Finisher 12.0"]["item"] == ""
    assert got["Tiny 0.1"]["item"] == ""
    assert list(got) == ["Starter 0.5", "Grower 9.0", "Finisher 12.0",
                         "Tiny 0.1"]


_VALID_TEXT = """\
schema: 1
fixed_monthly: 1000.0
oxygen_per_kg_feed: 0.5
chemicals_per_kg_feed: 0.25
feed_shipping_per_kg: 0.1
egg_price: 0.01
feed_prices:
  Starter 0.5: {item: Acme Crumble No.1, price_per_kg: 4.0}
  Grower 9.0: {item: Acme Grower Pellet 9mm, price_per_kg: 2.0}
"""


def test_valid_hand_written_file_loads(tmp_path):
    _write(tmp_path, _VALID_TEXT)
    assert C.load_costs(tmp_path) == C.validate_costs(_valid())


@pytest.mark.parametrize("text,key", [
    (_VALID_TEXT + "fixed_monthly: 5.0\n", "fixed_monthly"),
    (_VALID_TEXT + "  Grower 9.0: {item: Acme Other Pellet, "
                   "price_per_kg: 3.0}\n", "Grower 9.0"),
])
def test_duplicate_key_is_refused_not_last_wins(tmp_path, text, key):
    # PyYAML's safe_load would keep the last value silently.
    assert yaml.safe_load(text) is not None
    _write(tmp_path, text)
    with pytest.raises(ValueError, match="duplicate key") as ei:
        C.load_costs(tmp_path)
    assert key in str(ei.value) and "costs.yaml" in str(ei.value)


def test_non_utf8_file_is_named_and_still_has_a_signature(tmp_path):
    text = _VALID_TEXT.replace("Acme Crumble No.1", "Acme Créme")
    (tmp_path / C.COSTS_FILE).write_bytes(text.encode("latin-1"))
    with pytest.raises(ValueError, match="costs.yaml.*UTF-8"):
        C.load_costs(tmp_path)
    sig = C.costs_sig(tmp_path)
    assert sig == hashlib.md5(text.encode("latin-1")).hexdigest()


def test_str_subclass_names_are_normalised_and_saved(tmp_path):
    np = pytest.importorskip("numpy")
    d = _valid()
    d["feed_prices"] = {np.str_("Grower 9.0"): {"item": np.str_("Acme Pellet"),
                                                "price_per_kg": np.float64(2.0)}}
    d["fixed_monthly"] = np.int64(1000)
    C.save_costs(tmp_path, d)
    got = C.load_costs(tmp_path)
    (name,) = got["feed_prices"]
    assert type(name) is str and name == "Grower 9.0"
    assert type(got["feed_prices"][name]["item"]) is str
    assert got["fixed_monthly"] == 1000.0


def test_save_writes_only_costs_yaml(tmp_path):
    other = tmp_path / "economics.yaml"
    other.write_bytes(b"currency: XYZ\n")
    before = other.read_bytes()
    C.save_costs(tmp_path, _valid())
    assert sorted(os.listdir(tmp_path)) == [C.COSTS_FILE, "economics.yaml"]
    assert other.read_bytes() == before


@pytest.mark.parametrize("label,mutate,field", MALFORMED,
                         ids=[m[0] for m in MALFORMED])
def test_save_refuses_invalid_and_leaves_file_byte_identical(
        tmp_path, label, mutate, field):
    C.save_costs(tmp_path, _valid())
    path = tmp_path / C.COSTS_FILE
    before = path.read_bytes()
    listing = sorted(os.listdir(tmp_path))
    bad = _valid()
    mutate(bad)
    with pytest.raises(ValueError):
        C.save_costs(tmp_path, bad)
    assert path.read_bytes() == before
    assert sorted(os.listdir(tmp_path)) == listing      # no temp left behind


def test_save_refuses_invalid_when_no_file_exists(tmp_path):
    bad = _valid()
    del bad["egg_price"]
    with pytest.raises(ValueError, match="egg_price"):
        C.save_costs(tmp_path, bad)
    assert os.listdir(tmp_path) == []


def test_costs_sig_tracks_content(tmp_path):
    assert C.costs_sig(tmp_path) == "none"
    C.save_costs(tmp_path, _valid())
    s1 = C.costs_sig(tmp_path)
    assert len(s1) == 32 and s1 != "none"
    C.save_costs(tmp_path, _valid())
    assert C.costs_sig(tmp_path) == s1                  # same content
    d = _valid()
    d["feed_prices"]["Grower 9.0"]["price_per_kg"] = 2.5
    C.save_costs(tmp_path, d)
    assert C.costs_sig(tmp_path) != s1


# --------------------------------------------------------------------------- #
# price coverage: model feed types vs item names
# --------------------------------------------------------------------------- #
NAMES = ["Starter 0.5", "Grower 9.0", "Finisher 12.0"]


def test_missing_and_orphan_prices():
    c = C.validate_costs(_valid())
    assert C.missing_feed_prices(c, NAMES) == ["Finisher 12.0"]
    assert C.orphan_feed_prices(c, NAMES) == []
    assert C.orphan_feed_prices(c, ["Starter 0.5"]) == ["Grower 9.0"]
    assert C.missing_feed_prices(None, NAMES) == NAMES
    assert C.orphan_feed_prices(None, NAMES) == []


def test_price_keyed_by_item_name_is_orphan_and_type_missing():
    d = _valid()
    d["feed_prices"] = {
        "Starter 0.5": {"item": "Acme Crumble No.1", "price_per_kg": 4.0},
        # keyed by the ITEM name, not the model feed type
        "Acme Grower Pellet 9mm": {"item": "", "price_per_kg": 2.0},
    }
    c = C.validate_costs(d)
    assert C.orphan_feed_prices(c, NAMES) == ["Acme Grower Pellet 9mm"]
    assert "Grower 9.0" in C.missing_feed_prices(c, NAMES)
    fc = C.feed_cost({"Grower 9.0": 100.0}, c)
    assert fc["feed"] == 0.0
    assert fc["unpriced_kg"] == 100.0
    assert fc["unpriced_types"] == ["Grower 9.0"]


def test_item_names_never_affect_pricing():
    a = _valid()
    b = copy.deepcopy(a)
    b["feed_prices"]["Starter 0.5"]["item"] = "Totally Different Name"
    b["feed_prices"]["Grower 9.0"]["item"] = ""
    feed = {"Starter 0.5": 30.0, "Grower 9.0": 70.0}
    assert C.feed_cost(feed, a) == C.feed_cost(feed, b)
    assert C.period_cost(feed, 100, 1.0, a) == C.period_cost(feed, 100, 1.0, b)


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
def test_period_cost_hand_computed():
    feed = {"Starter 0.5": 100.0, "Grower 9.0": 50.0, "Mystery 3.0": 10.0}
    got = C.period_cost(feed, 1000, 0.5, _valid())
    # feed 100x4 + 50x2 = 500 (Mystery unpriced); all 160 kg carry the per-kg
    # extras: shipping 16, oxygen 80, chemicals 40; eggs 1000x0.01 = 10;
    # fixed 0.5 months x 1000 = 500.
    assert got["feed"] == pytest.approx(500.0)
    assert got["feed_kg"] == pytest.approx(160.0)
    assert got["shipping"] == pytest.approx(16.0)
    assert got["oxygen"] == pytest.approx(80.0)
    assert got["chemicals"] == pytest.approx(40.0)
    assert got["eggs_n"] == 1000.0
    assert got["eggs"] == pytest.approx(10.0)
    assert got["fixed"] == pytest.approx(500.0)
    assert got["variable"] == pytest.approx(646.0)
    assert got["total"] == pytest.approx(1146.0)
    assert got["unpriced_kg"] == pytest.approx(10.0)
    assert got["unpriced_types"] == ["Mystery 3.0"]
    assert set(got) == {"feed", "shipping", "oxygen", "chemicals", "eggs_n",
                        "eggs", "fixed", "variable", "total", "feed_kg",
                        "unpriced_kg", "unpriced_types"}


def test_unpriced_feed_is_reported_never_priced_at_zero():
    c = _valid()
    fc = C.feed_cost({"Mystery 3.0": 40.0, "Other 1.0": 0.0}, c)
    assert fc["feed"] == 0.0
    assert fc["unpriced_kg"] == 40.0
    assert fc["unpriced_types"] == ["Mystery 3.0"]   # zero kg is not a gap
    # A price typed as 0 IS a price: the kg are priced, not unpriced.
    c["feed_prices"]["Mystery 3.0"] = {"item": "", "price_per_kg": 0.0}
    fc0 = C.feed_cost({"Mystery 3.0": 40.0}, c)
    assert fc0["unpriced_kg"] == 0.0 and fc0["unpriced_types"] == []


def test_shipping_oxygen_chemicals_on_all_feed_kg():
    c = _valid()
    # a freshwater starter, a grow-out feed and an unpriced type
    feed = {"Starter 0.5": 20.0, "Grower 9.0": 60.0, "Mystery 3.0": 20.0}
    fc = C.feed_cost(feed, c)
    assert fc["feed_kg"] == pytest.approx(100.0)
    assert fc["shipping"] == pytest.approx(100.0 * 0.1)
    assert fc["oxygen"] == pytest.approx(100.0 * 0.5)
    assert fc["chemicals"] == pytest.approx(100.0 * 0.25)
    # Changing only the shipping rate moves only shipping (and the totals).
    c2 = copy.deepcopy(c)
    c2["feed_shipping_per_kg"] = 0.3
    a = C.period_cost(feed, 0, 1.0, c)
    b = C.period_cost(feed, 0, 1.0, c2)
    assert b["shipping"] - a["shipping"] == pytest.approx(100.0 * 0.2)
    for k in ("feed", "oxygen", "chemicals", "eggs", "fixed"):
        assert a[k] == b[k]
    assert b["total"] - a["total"] == pytest.approx(20.0)


def test_pricing_refuses_missing_inputs():
    with pytest.raises(ValueError, match="costs not set"):
        C.feed_cost({"Grower 9.0": 1.0}, None)
    with pytest.raises(ValueError, match="costs not set"):
        C.period_cost({}, 0, 1.0, None)
    with pytest.raises(ValueError, match="feed_kg_by_type"):
        C.feed_cost(None, _valid())
    with pytest.raises(ValueError, match="eggs"):
        C.period_cost({}, None, 1.0, _valid())
    with pytest.raises(ValueError, match="fixed months"):
        C.period_cost({}, 0, None, _valid())
    with pytest.raises(ValueError, match="Grower 9.0"):
        C.feed_cost({"Grower 9.0": -5.0}, _valid())
    with pytest.raises(ValueError, match="feed_kg_by_type"):
        C.feed_cost([("Grower 9.0", 1.0)], _valid())
    bad = _valid()
    del bad["feed_shipping_per_kg"]                # hand-built, incomplete
    with pytest.raises(ValueError, match="feed_shipping_per_kg"):
        C.feed_cost({"Grower 9.0": 1.0}, bad)


def _months_by_hand(first_day, days):
    """Fixed-cost months of `days` calendar days from first_day, one day
    at a time: each day is 1 / (days in its month) of a month."""
    import calendar
    out = 0.0
    for i in range(days):
        d = first_day + dt.timedelta(days=i)
        out += 1.0 / calendar.monthrange(d.year, d.month)[1]
    return out


def test_iso_weeks_fixed_months_charges_the_calendar_days():
    assert dt.date(2026, 12, 28).isocalendar()[1] == 53
    assert dt.date(2027, 12, 28).isocalendar()[1] == 52
    # 52 weeks of 2029: Mon 2029-01-01 .. Sun 2029-12-30 - December 30/31.
    assert C.iso_weeks_fixed_months("2029-W01", 52) == pytest.approx(
        11 + 30 / 31)
    # 53 weeks of 2026: Mon 2025-12-29 .. Sun 2027-01-03 = 371 days, never
    # "12 months" (the sanity check's 53-week flag).
    assert C.iso_weeks_fixed_months("2026-W01", 53) == pytest.approx(
        3 / 31 + 12 + 3 / 31)
    # A part year from the run's first week (2026-W36 is Mon 08-31).
    assert C.iso_weeks_fixed_months("2026-W36", 18) == pytest.approx(
        _months_by_hand(dt.date(2026, 8, 31), 18 * 7))
    for wk, n in (("2027-W01", 26), ("2028-W10", 30), ("2030-W01", 35)):
        y, w = int(wk[:4]), int(wk[6:])
        assert C.iso_weeks_fixed_months(wk, n) == pytest.approx(
            _months_by_hand(dt.date.fromisocalendar(y, w, 1), 7 * n))
    assert C.iso_weeks_fixed_months("2027-W01", 0) == 0.0
    for bad in (("2027-53", 1), ("2027-W53", 1), ("2027-W01", -1),
                ("2027-W01", 1.5), ("2027-W01", True)):
        with pytest.raises(ValueError):
            C.iso_weeks_fixed_months(*bad)


def test_the_ideal_year_and_the_sheet_charge_the_same_days_alike():
    """One day split: a label-year's fixed months equal monthly_pl's
    pro-rating of the same calendar days (the sanity check's Ideal-vs-sheet
    comparison differed by exactly 1/31 of a month before)."""
    first, last = dt.date(2029, 1, 1), dt.date(2029, 12, 30)
    md = C.month_days(C.span_months(first, last), first, last)
    pl = C.monthly_pl({}, {}, None, {}, md, _valid())
    y = types.SimpleNamespace(year=2029, weeks=52, feed_kg_by_type={},
                              eggs=0, first_week="2029-W01")
    assert C.year_cost(y, _valid())["fixed"] == pytest.approx(
        pl["total"]["fixed"], rel=1e-12)


def test_year_weeks_fixed_months_needs_to_know_a_part_years_weeks():
    ns = types.SimpleNamespace
    # A whole ISO year needs no first_week: its weeks are W01 on.
    assert C.year_weeks_fixed_months(ns(year=2029, weeks=52)) == \
        pytest.approx(11 + 30 / 31)
    assert C.year_weeks_fixed_months(ns(year=2026, weeks=0)) == 0.0
    # A part year without first_week is refused, never guessed.
    with pytest.raises(ValueError, match="without cost drivers.*first_week"):
        C.year_weeks_fixed_months(ns(year=2026, weeks=18))
    assert C.year_weeks_fixed_months(
        ns(year=2026, weeks=18, first_week="2026-W36")) == pytest.approx(
        C.iso_weeks_fixed_months("2026-W36", 18))
    for bad in (ns(year=2027, weeks=53),                       # 2027 has 52
                ns(year=2026, weeks=-1),
                ns(year=2026, weeks=18, first_week="2027-W01"),  # other year
                ns(year=2026, weeks=18, first_week="2026-W40")):  # past W53
        with pytest.raises(ValueError):
            C.year_weeks_fixed_months(bad)


def test_year_cost_prices_the_drivers():
    feed = {"Starter 0.5": 10.0, "Grower 9.0": 90.0}
    y = types.SimpleNamespace(year=2027, weeks=26, feed_kg_by_type=feed,
                              eggs=500, first_week="2027-W01")
    months = _months_by_hand(dt.date(2027, 1, 4), 26 * 7)  # 01-04 .. 07-04
    assert C.year_cost(y, _valid()) == C.period_cost(
        feed, 500, C.iso_weeks_fixed_months("2027-W01", 26), _valid())
    assert C.year_cost(y, _valid())["fixed"] == pytest.approx(1000.0 * months)


@pytest.mark.parametrize("fields", [
    {"feed_kg_by_type": None, "eggs": 5},
    {"feed_kg_by_type": {"Grower 9.0": 1.0}, "eggs": None},
    {},                                            # a read with no drivers at all
])
def test_year_cost_refuses_missing_drivers_loudly(fields):
    y = types.SimpleNamespace(year=2027, weeks=52, **fields)
    with pytest.raises(ValueError, match="without cost drivers"):
        C.year_cost(y, _valid())


def test_cost_per_kg_hog():
    assert C.cost_per_kg_hog(100.0, 50.0) == 2.0
    assert C.cost_per_kg_hog(100.0, 0.0) is None
    assert C.cost_per_kg_hog(100.0, -1.0) is None
    assert C.cost_per_kg_hog(100.0, None) is None
    assert C.cost_per_kg_hog(None, 10.0) is None


# --------------------------------------------------------------------------- #
# monthly_pl
# --------------------------------------------------------------------------- #
def _pl_inputs():
    report_start = dt.date(2026, 9, 16)
    first = (dt.date(2026, 9, 30) - report_start).days + 1       # 15 of 30
    month_days = {"2026-09": (first, 30), "2026-10": (31, 31),
                  "2026-11": (30, 30), "2026-12": (31, 31),
                  "2027-01": (10, 31)}
    feed = {"2026-09": {"Starter 0.5": 10.0, "Grower 9.0": 100.0},
            "2026-10": {"Grower 9.0": 200.0},
            "2026-11": {"Grower 9.0": 210.0, "Mystery 3.0": 5.0},
            "2026-12": {"Grower 9.0": 220.0},
            "2027-01": {"Grower 9.0": 70.0}}
    eggs = {"2026-10": 3000, "2027-01": 1000}
    revenue = {"2026-09": 2500.0, "2026-11": 4000.0, "2027-01": 900.0}
    hog = {"2026-09": 500.0, "2026-11": 800.0, "2027-01": 150.0}
    return feed, eggs, revenue, hog, month_days


NUM = ("feed_kg", "feed", "shipping", "oxygen", "chemicals", "eggs_n", "eggs",
       "fixed", "variable", "total", "hog_kg", "revenue", "profit",
       "unpriced_kg")


def test_monthly_pl_sums_tie():
    feed, eggs, revenue, hog, days = _pl_inputs()
    pl = C.monthly_pl(feed, eggs, revenue, hog, days, _valid())
    months = pl["months"]
    assert [r["period"] for r in months] == [
        "2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]
    assert {r["kind"] for r in months} == {"Month"}
    assert [r["period"] for r in pl["years"]] == ["2026", "2027"]
    assert {r["kind"] for r in pl["years"]} == {"Year"}
    assert pl["total"]["kind"] == "Total"
    for y in pl["years"]:
        mine = [r for r in months if r["period"].startswith(y["period"])]
        for f in NUM:
            assert y[f] == pytest.approx(sum(r[f] for r in mine)), (y, f)
    for f in NUM:
        assert pl["total"][f] == pytest.approx(sum(y[f] for y in pl["years"]))
        assert pl["total"][f] == pytest.approx(sum(r[f] for r in months))
    for r in months + pl["years"] + [pl["total"]]:
        assert r["profit"] == pytest.approx(r["revenue"] - r["total"])
        assert r["total"] == pytest.approx(r["variable"] + r["fixed"])
    assert pl["total"]["unpriced_types"] == ["Mystery 3.0"]
    assert pl["total"]["unpriced_kg"] == pytest.approx(5.0)


def test_monthly_pl_pins_month_rows_by_hand():
    # The sum-tie test cannot see a flow dropped from every month (the year
    # and total rows are sums of the month rows), so pin rows by hand.
    feed, eggs, revenue, hog, days = _pl_inputs()
    pl = C.monthly_pl(feed, eggs, revenue, hog, days, _valid())
    by = {r["period"]: r for r in pl["months"]}
    # 2026-09: feed 10x4 + 100x2 = 240 on 110 kg; shipping 11, oxygen 55,
    # chemicals 27.5; no eggs; fixed 15/30 x 1000 = 500.
    sep = by["2026-09"]
    assert sep["feed"] == pytest.approx(240.0)
    assert sep["shipping"] == pytest.approx(11.0)
    assert sep["oxygen"] == pytest.approx(55.0)
    assert sep["chemicals"] == pytest.approx(27.5)
    assert sep["eggs_n"] == 0 and sep["eggs"] == 0
    assert sep["variable"] == pytest.approx(333.5)
    assert sep["total"] == pytest.approx(833.5)
    assert sep["revenue"] == pytest.approx(2500.0)
    assert sep["profit"] == pytest.approx(2500.0 - 833.5)
    # 2026-10: feed 200x2 = 400; shipping 20, oxygen 100, chemicals 50;
    # 3000 eggs x 0.01 = 30; fixed 1000; no harvest, no revenue.
    oct_ = by["2026-10"]
    assert oct_["eggs_n"] == 3000 and oct_["eggs"] == pytest.approx(30.0)
    assert oct_["variable"] == pytest.approx(600.0)
    assert oct_["total"] == pytest.approx(1600.0)
    assert oct_["revenue"] == 0.0
    assert oct_["profit"] == pytest.approx(-1600.0)
    tot = pl["total"]
    assert tot["revenue"] == pytest.approx(7400.0)
    assert tot["eggs_n"] == 4000
    assert tot["eggs"] == pytest.approx(40.0)
    assert tot["hog_kg"] == pytest.approx(1450.0)


def test_monthly_pl_partial_months_prorate_fixed():
    feed, eggs, revenue, hog, days = _pl_inputs()
    pl = C.monthly_pl(feed, eggs, revenue, hog, days, _valid())
    by = {r["period"]: r for r in pl["months"]}
    assert by["2026-09"]["days_covered"] == 15
    assert by["2026-09"]["days_in_month"] == 30
    assert by["2026-09"]["fixed"] == pytest.approx(1000.0 * 15 / 30)
    assert by["2026-10"]["fixed"] == pytest.approx(1000.0)
    assert by["2027-01"]["fixed"] == pytest.approx(1000.0 * 10 / 31)


def test_monthly_pl_month_without_harvest_is_a_loss_with_no_cost_per_kg():
    feed, eggs, revenue, hog, days = _pl_inputs()
    pl = C.monthly_pl(feed, eggs, revenue, hog, days, _valid())
    by = {r["period"]: r for r in pl["months"]}
    oct_ = by["2026-10"]
    assert oct_["hog_kg"] == 0.0 and oct_["revenue"] == 0.0
    assert oct_["cost_per_kg_hog"] is None
    assert oct_["variable_per_kg_hog"] is None
    assert oct_["profit"] == pytest.approx(-oct_["total"])
    sep = by["2026-09"]
    assert sep["cost_per_kg_hog"] == pytest.approx(sep["total"] / 500.0)
    assert sep["variable_per_kg_hog"] == pytest.approx(sep["variable"] / 500.0)
    tot = pl["total"]
    assert tot["cost_per_kg_hog"] == pytest.approx(tot["total"] / 1450.0)


def test_monthly_pl_without_revenue_leaves_profit_unset():
    feed, eggs, _, hog, days = _pl_inputs()
    pl = C.monthly_pl(feed, eggs, None, hog, days, _valid())
    for r in pl["months"] + pl["years"] + [pl["total"]]:
        assert r["revenue"] is None and r["profit"] is None
        assert r["total"] > 0


def test_monthly_pl_refuses_bad_inputs():
    feed, eggs, revenue, hog, days = _pl_inputs()
    no_dec = {k: v for k, v in days.items() if k != "2026-12"}
    with pytest.raises(ValueError, match="2026-12"):
        C.monthly_pl(feed, eggs, revenue, hog, no_dec, _valid())
    with pytest.raises(ValueError, match="YYYY-MM"):
        C.monthly_pl({"2026-9": {}}, eggs, revenue, hog, days, _valid())
    over = dict(days, **{"2026-10": (32, 31)})
    with pytest.raises(ValueError, match="2026-10"):
        C.monthly_pl(feed, eggs, revenue, hog, over, _valid())
    with pytest.raises(ValueError, match="costs not set"):
        C.monthly_pl(feed, eggs, revenue, hog, days, None)
    with pytest.raises(ValueError, match="eggs_by_month"):
        C.monthly_pl(feed, None, revenue, hog, days, _valid())


@pytest.mark.parametrize("month,entry,msg", [
    ("2026-09", (15, 31), "30 days"),         # off-by-one days in month
    ("2026-10", (31, 30), "31 days"),
    ("2026-10", 31, "2026-10"),               # not a pair
    ("2026-10", (1, 2, 3), "2026-10"),
])
def test_monthly_pl_refuses_month_days_off_the_calendar(month, entry, msg):
    feed, eggs, revenue, hog, days = _pl_inputs()
    days = dict(days, **{month: entry})
    with pytest.raises(ValueError, match=msg) as ei:
        C.monthly_pl(feed, eggs, revenue, hog, days, _valid())
    assert month in str(ei.value)


def test_monthly_pl_leap_february():
    c = _valid()
    ok = C.monthly_pl({}, {}, None, {}, {"2028-02": (29, 29)}, c)
    assert ok["months"][0]["fixed"] == pytest.approx(1000.0)
    with pytest.raises(ValueError, match="2027-02 has 28 days"):
        C.monthly_pl({}, {}, None, {}, {"2027-02": (29, 29)}, c)


def test_monthly_pl_harvest_month_needs_a_revenue_entry():
    feed, eggs, revenue, hog, days = _pl_inputs()
    no_sep = {k: v for k, v in revenue.items() if k != "2026-09"}
    with pytest.raises(ValueError, match="2026-09.*no revenue"):
        C.monthly_pl(feed, eggs, no_sep, hog, days, _valid())
    # an explicit 0 is a revenue entry
    pl = C.monthly_pl(feed, eggs, dict(no_sep, **{"2026-09": 0}), hog, days,
                      _valid())
    assert pl["months"][0]["revenue"] == 0.0
    # revenue not priced at all: no entry needed
    C.monthly_pl(feed, eggs, None, hog, days, _valid())


def test_monthly_pl_empty_is_all_zero_not_an_error():
    pl = C.monthly_pl({}, {}, {}, {}, {}, _valid())
    assert pl["months"] == [] and pl["years"] == []
    assert pl["total"]["total"] == 0 and pl["total"]["cost_per_kg_hog"] is None


# --------------------------------------------------------------------------- #
# revenue: the P&L's per-month revenue_for calls add up to one call
# --------------------------------------------------------------------------- #
def _econ(aug_override=None):
    return {"currency": "XYZ", "basis": "hog", "model_cv_pct": 18.0,
            "price_bands": [
                {"min_kg": 0.0, "max_kg": 3.0, "price_per_kg": 5.0,
                 "monthly": {}},
                {"min_kg": 3.0, "max_kg": 100.0, "price_per_kg": 7.0,
                 "monthly": ({"2026-08": aug_override}
                             if aug_override is not None else {})}]}


def _hrow(week, count, avg):
    return {"week": week, "count": count, "gross_avg_kg": avg * 1.1,
            "gross_kg": count * avg * 1.1, "hog_kg": count * avg,
            "hog_avg_kg": avg}


@pytest.mark.parametrize("aug_override", [None, 9.0])
def test_revenue_per_month_sums_to_one_call_with_first_week_clip(aug_override):
    from forecast.analysis import revenue_for
    from forecast.time_grid import iso_week_month_split
    report_start = dt.date(2026, 9, 1)        # the day after a month-end PR
    rows = [_hrow("2026-W36", 1000, 4.0),     # Monday 2026-08-31: clipped
            _hrow("2026-W37", 1200, 2.5),
            _hrow("2026-W40", 900, 4.5),
            _hrow("2026-W45", 1100, 3.2)]
    econ = _econ(aug_override)
    groups = {}
    for r in rows:
        y, w = int(r["week"][:4]), int(r["week"][6:8])
        monday = dt.date.fromisocalendar(y, w, 1)
        (ym,) = iso_week_month_split(monday, clip_start=report_start)
        groups.setdefault(f"{ym[0]}-{ym[1]:02d}", []).append(r)
    # The clipped first week is booked in the first month the report covers.
    assert "2026-08" not in groups
    assert rows[0] in groups["2026-09"]
    one = revenue_for(rows, econ)["total"]
    per = sum(revenue_for(g, econ)["total"] for g in groups.values())
    assert per == pytest.approx(one, rel=1e-12)
    # Documented caveat (design R11): revenue_for prices the clipped week by
    # its MONDAY's month, so an August override still applies to it although
    # the P&L books it in September.
    w36 = revenue_for([rows[0]], econ)["total"]
    w36_plain = revenue_for([rows[0]], _econ(None))["total"]
    if aug_override is None:
        assert w36 == pytest.approx(w36_plain)
    else:
        assert w36 > w36_plain


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #
def test_costs_yaml_is_ignored_by_git():
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    inside = subprocess.run([git, "rev-parse", "--is-inside-work-tree"],
                            cwd=ROOT, capture_output=True, text=True)
    if inside.returncode != 0:
        pytest.skip("not a git work tree")
    # the real file, and the temp file write_text_atomic stages it through
    # (left behind if the process is killed between write and rename)
    for rel in ("config/costs.yaml", "config/costs.yaml.tmp-12345"):
        rc = subprocess.run([git, "check-ignore", "-q", rel],
                            cwd=ROOT).returncode
        assert rc == 0, f"{rel} is NOT git-ignored (.gitignore)"


def test_module_is_pure():
    code = ("import sys, forecast.costs; "
            "bad = [m for m in ('streamlit', 'openpyxl', 'forecast.run', "
            "'forecast.ideal_engine', 'forecast.analysis') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
