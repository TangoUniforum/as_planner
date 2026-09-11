"""Operating costs: `config/costs.yaml` and the pure arithmetic that prices a
plan's cost DRIVERS (feed kg by type, eggs stocked, months covered).

CASH VIEW (operator ruling, 2026-09-10/11). Each month's SPEND is set against
that month's sales. There is no per-batch cost carrying (no COGS matched to the
fish sold). So a month with little harvest shows a loss even while the fish in
the tanks gain value.

    feed       = sum over types of (feed kg x price per kg of that MODEL feed type)
    shipping   = ALL feed kg x feed_shipping_per_kg
    oxygen     = ALL feed kg x oxygen_per_kg_feed
    chemicals  = ALL feed kg x chemicals_per_kg_feed
    eggs       = eggs stocked (BatchInput.input_count) x egg_price
    fixed      = fixed_monthly x months covered (a part month pro-rates by days)
    variable   = feed + shipping + oxygen + chemicals + eggs
    total      = variable + fixed
    profit     = revenue - total

"ALL feed kg" includes freshwater/hatchery feed and feed of a type that has no
price. Shipping, oxygen and chemicals follow the physical kilogram, not the
price list.

The file (schema 1; every key required; currency comes from economics.yaml):

    schema: 1
    fixed_monthly: <number>          # per calendar month
    oxygen_per_kg_feed: <number>
    chemicals_per_kg_feed: <number>
    feed_shipping_per_kg: <number>
    egg_price: <number>              # per egg
    feed_prices:                     # keyed by the MODEL feed type name
      "<biology.yaml feed type>": {item: "<operator item name>", price_per_kg: <number>}

`item` is the operator's own name for the product. It is carried for people
to read and NEVER affects pricing: the engine prices by the model feed type.

The numbers are commercially sensitive. The file is kept out of git
(.gitignore) and edited in Configure -> Targets & prices -> Costs.

This module never invents a price or a zero:
  * a missing or empty file means "costs not set" (None);
  * a malformed file raises ValueError naming the field;
  * feed of a type with no price is left UNPRICED and reported, never priced at 0.

Pure: no Streamlit, no engine, no openpyxl.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import math
import numbers
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

import yaml

from .yaml_atomic import (_RETRIES, _RETRY_DELAY_S, read_text_resilient,
                          write_text_atomic)

COSTS_FILE = "costs.yaml"
SCHEMA = 1

_SCALARS = ("fixed_monthly", "oxygen_per_kg_feed", "chemicals_per_kg_feed",
            "feed_shipping_per_kg", "egg_price")
_KEYS = ("schema",) + _SCALARS + ("feed_prices",)
_PRICE_KEYS = ("item", "price_per_kg")

_HEADER = (
    "# config/costs.yaml - operating costs. Commercially sensitive: kept OUT of\n"
    "# git (.gitignore). Edit it in Configure -> Targets & prices -> Costs.\n"
    "# Currency = economics.yaml `currency`. feed_prices are keyed by the MODEL\n"
    "# feed type (biology.yaml feed_types); `item` is a label only.\n"
)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Fields summed from month rows into the year rows and the total row.
_SUM_FIELDS = ("feed_kg", "feed", "shipping", "oxygen", "chemicals", "eggs_n",
               "eggs", "fixed", "variable", "total", "hog_kg", "revenue",
               "profit", "unpriced_kg")


def _real(value, what: str) -> float:
    """A finite, non-negative real number, or ValueError naming `what`."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{what} must be a number (got {value!r})")
    try:
        x = float(value)
    except OverflowError:
        raise ValueError(f"{what} is too large to be a number "
                         f"(got {value!r})") from None
    if not math.isfinite(x):
        raise ValueError(f"{what} must be a finite number (got {value!r})")
    if x < 0:
        raise ValueError(f"{what} must not be negative (got {value!r})")
    return x


def _fail(msg: str) -> ValueError:
    return ValueError(f"{COSTS_FILE}: {msg}")


class _DuplicateKey(yaml.constructor.ConstructorError):
    pass


class _StrictLoader(yaml.SafeLoader):
    """safe_load that refuses a repeated mapping key. Plain PyYAML keeps the
    last one silently, so one of two prices for the same feed type would be
    dropped without a word."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    dup = key in seen
                except TypeError:          # unhashable: the base class says so
                    continue
                if dup:
                    raise _DuplicateKey(None, None, f"duplicate key {key!r}",
                                        key_node.start_mark)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _read_bytes(path: Path) -> bytes:
    """The file's bytes, retrying briefly on a OneDrive lock (the same retry
    as yaml_atomic.read_text_resilient)."""
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            return path.read_bytes()
        except PermissionError as exc:
            last = exc
            time.sleep(_RETRY_DELAY_S * (attempt + 1))
    raise PermissionError(
        f"{path} is locked (likely OneDrive sync or another program has it "
        f"open) after {_RETRIES} retries - close it and try again") from last


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def validate_costs(d) -> dict:
    """Check a costs mapping and return a normalised copy (plain floats and
    str; an `item` written as null becomes ""). load_costs and save_costs
    both use it. Raises ValueError naming the field. Nothing is defaulted:
    every price entry must carry both `item` (blank allowed) and
    `price_per_kg`."""
    if not isinstance(d, Mapping):
        raise _fail(f"must be a mapping of fields (got {type(d).__name__})")
    unknown = [k for k in d if k not in _KEYS]
    if unknown:
        raise _fail(f"unknown key(s) {', '.join(map(repr, unknown))} - "
                    f"expected exactly: {', '.join(_KEYS)}")
    for k in _KEYS:
        if k not in d:
            raise _fail(f"{k} is missing - every field is required; "
                        f"nothing is defaulted")
    s = d["schema"]
    if isinstance(s, bool) or not isinstance(s, int) or s != SCHEMA:
        raise _fail(f"schema must be {SCHEMA} (got {s!r})")
    out: dict = {"schema": SCHEMA}
    for k in _SCALARS:
        out[k] = _real(d[k], f"{COSTS_FILE}: {k}")
    fp = d["feed_prices"]
    if not isinstance(fp, Mapping):
        raise _fail("feed_prices must be a mapping of model feed type -> "
                    f"{{item, price_per_kg}} (got {fp!r})")
    prices: dict = {}
    for name, entry in fp.items():
        if not isinstance(name, str) or not name.strip():
            raise _fail(f"feed_prices: blank or non-text feed type {name!r}")
        name = str(name)                   # a str subclass (np.str_) -> str
        where = f"feed_prices[{name!r}]"
        if not isinstance(entry, Mapping):
            raise _fail(f"{where} must be a mapping {{item, price_per_kg}} "
                        f"(got {entry!r})")
        extra = [k for k in entry if k not in _PRICE_KEYS]
        if extra:
            raise _fail(f"{where}: unknown key(s) "
                        f"{', '.join(map(repr, extra))} - expected item, "
                        f"price_per_kg")
        for k in _PRICE_KEYS:
            if k not in entry:
                raise _fail(f"{where}: {k} is missing - every price entry "
                            f"needs item (blank allowed) and price_per_kg")
        item = entry["item"]
        if item is None:
            item = ""
        elif not isinstance(item, str):
            raise _fail(f"{where}: item must be text (got {item!r}) - quote it")
        item = str(item)
        prices[name] = {
            "item": item,
            "price_per_kg": _real(entry["price_per_kg"],
                                  f"{COSTS_FILE}: {where}.price_per_kg"),
        }
    out["feed_prices"] = prices
    return out


def load_costs(config_dir) -> Optional[dict]:
    """The validated costs, or None when `config_dir/costs.yaml` is missing or
    empty ("costs not set"). A file that is present but malformed raises
    ValueError naming the field."""
    path = Path(config_dir) / COSTS_FILE
    if not path.is_file():
        return None
    try:
        data = yaml.load(read_text_resilient(path), Loader=_StrictLoader)
    except UnicodeDecodeError as exc:
        raise _fail(f"is not UTF-8 text (byte {exc.start}) - save it as "
                    f"UTF-8, or re-enter the costs in Configure") from exc
    except _DuplicateKey as exc:
        raise _fail(f"{exc.problem} at line {exc.problem_mark.line + 1} - "
                    f"YAML would silently keep only the last one; remove "
                    f"the other") from exc
    except yaml.YAMLError as exc:
        raise _fail(f"not valid YAML ({exc})") from exc
    if data is None:
        return None
    return validate_costs(data)


def save_costs(config_dir, costs) -> Path:
    """Validate, then atomically write ONLY `config_dir/costs.yaml`. An
    invalid mapping raises ValueError and leaves any existing file untouched."""
    clean = validate_costs(costs)
    path = Path(config_dir) / COSTS_FILE
    write_text_atomic(path, _HEADER + yaml.safe_dump(
        clean, sort_keys=False, allow_unicode=True))
    return path


def _file_sig(path: Path) -> str:
    """md5 of a file's bytes, or "none" when it is missing. Never decodes."""
    if not path.is_file():
        return "none"
    return hashlib.md5(_read_bytes(path)).hexdigest()


def costs_sig(config_dir) -> str:
    """md5 of costs.yaml's bytes, or "none" when the file is missing. Used by
    the UI staleness signatures. Never decodes, so a file that is not UTF-8
    still gets a signature (load_costs is where it is refused)."""
    return _file_sig(Path(config_dir) / COSTS_FILE)


def economics_sig(config_dir) -> str:
    """md5 of economics.yaml's bytes (the price bands revenue, and so profit,
    is priced with), or "none" when the file is missing. The CostsAndProfit
    sheet carries it and the Ideal page keeps it with a result, so a page can
    say when the price bands changed since the figures were priced."""
    from .analysis import ECONOMICS_FILE
    return _file_sig(Path(config_dir) / ECONOMICS_FILE)


def missing_feed_prices(costs, feed_names) -> list:
    """Model feed types (in `feed_names` order) that have no price, for
    example after a rename in Biology. When costs are not set, every type is
    missing."""
    priced = (costs or {}).get("feed_prices") or {}
    return [n for n in dict.fromkeys(feed_names) if n not in priced]


def orphan_feed_prices(costs, feed_names) -> list:
    """Priced names (in file order) that are not a model feed type. This
    includes a price keyed by the operator's ITEM name instead of the model
    name."""
    names = set(feed_names)
    return [n for n in ((costs or {}).get("feed_prices") or {})
            if n not in names]


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def _priced(costs) -> dict:
    if costs is None:
        raise ValueError(
            "costs not set (config/costs.yaml is missing or empty) - nothing "
            "can be priced; set them in Configure -> Targets & prices -> Costs")
    return validate_costs(costs)


def feed_cost(feed_kg_by_type, costs) -> dict:
    """{feed, shipping, oxygen, chemicals, feed_kg, unpriced_kg,
    unpriced_types} for `{model feed type: kg}`.

    Feed kg of a type with no price is left out of `feed`, and its kg and type
    are reported. It is never priced at 0. Shipping, oxygen and chemicals are
    charged on ALL feed kg, priced or not."""
    c = _priced(costs)
    if feed_kg_by_type is None:
        raise ValueError("no feed drivers (feed_kg_by_type is None) - cannot "
                         "price feed")
    if not isinstance(feed_kg_by_type, Mapping):
        raise ValueError("feed_kg_by_type must be a mapping of model feed "
                         f"type -> kg (got {type(feed_kg_by_type).__name__})")
    prices = c["feed_prices"]
    feed = total_kg = unpriced_kg = 0.0
    unpriced_types: list = []
    for name, kg in feed_kg_by_type.items():
        kg = _real(kg, f"feed kg of {name!r}")
        total_kg += kg
        if kg == 0:
            continue
        p = prices.get(name)
        if p is None:
            unpriced_kg += kg
            unpriced_types.append(name)
        else:
            feed += kg * p["price_per_kg"]
    return {"feed": feed,
            "shipping": total_kg * c["feed_shipping_per_kg"],
            "oxygen": total_kg * c["oxygen_per_kg_feed"],
            "chemicals": total_kg * c["chemicals_per_kg_feed"],
            "feed_kg": total_kg,
            "unpriced_kg": unpriced_kg,
            "unpriced_types": unpriced_types}


def period_cost(feed_kg_by_type, eggs, fixed_months, costs) -> dict:
    """The spend of one period: {feed, shipping, oxygen, chemicals, eggs_n,
    eggs, fixed, variable, total, feed_kg, unpriced_kg, unpriced_types}.

    `eggs` is the number of eggs stocked in the period. `fixed_months` is the
    number of calendar months the period covers (a fraction for a part month)."""
    c = _priced(costs)
    fc = feed_cost(feed_kg_by_type, c)
    eggs_n = _real(eggs, "eggs stocked")
    months = _real(fixed_months, "fixed months")
    egg_cost = eggs_n * c["egg_price"]
    fixed = months * c["fixed_monthly"]
    variable = (fc["feed"] + fc["shipping"] + fc["oxygen"] + fc["chemicals"]
                + egg_cost)
    return {"feed": fc["feed"], "shipping": fc["shipping"],
            "oxygen": fc["oxygen"], "chemicals": fc["chemicals"],
            "eggs_n": eggs_n, "eggs": egg_cost, "fixed": fixed,
            "variable": variable, "total": variable + fixed,
            "feed_kg": fc["feed_kg"], "unpriced_kg": fc["unpriced_kg"],
            "unpriced_types": fc["unpriced_types"]}


def month_days(months, start: dt.date, end: dt.date) -> dict:
    """{"YYYY-MM": (days of [start, end] in the month, days in the month)}:
    the day split every fixed cost is pro-rated by (monthly_pl's
    `month_days` input, and `fixed_months_between`)."""
    out = {}
    for m in months:
        y, mo = int(m[:4]), int(m[5:7])
        n = calendar.monthrange(y, mo)[1]
        lo = max(dt.date(y, mo, 1), start)
        hi = min(dt.date(y, mo, n), end)
        out[m] = ((hi - lo).days + 1 if hi >= lo else 0, n)
    return out


def span_months(start: dt.date, end: dt.date) -> list:
    """Every "YYYY-MM" from start's month to end's, in order ([] when end
    is before start)."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fixed_months_between(first_day: dt.date, last_day: dt.date) -> float:
    """Months of fixed cost from first_day to last_day, both included: each
    calendar month's days in the span over that month's length - the rule
    monthly_pl pro-rates a part month by, so a span priced here and the same
    days on the CostsAndProfit sheet carry the same fixed cost."""
    if last_day < first_day:
        return 0.0
    md = month_days(span_months(first_day, last_day), first_day, last_day)
    return float(sum(c / n for c, n in md.values()))


_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def iso_weeks_fixed_months(first_week, weeks) -> float:
    """Months of fixed cost in `weeks` consecutive ISO weeks from
    `first_week` ("YYYY-Www"): the calendar days they span, Monday of the
    first to Sunday of the last, split by `fixed_months_between`. So the 52
    weeks of 2029 (Mon 2029-01-01 .. Sun 2029-12-30) are 11 + 30/31 months
    and the 53 weeks of 2026 (Mon 2025-12-29 .. Sun 2027-01-03) are
    3/31 + 12 + 3/31: a 53-week year is 371 days, never "12 months"."""
    m = _ISO_WEEK_RE.match(str(first_week))
    if not m:
        raise ValueError(f"first week {first_week!r} is not an ISO week "
                         f"label 'YYYY-Www'")
    try:
        monday = dt.date.fromisocalendar(int(m[1]), int(m[2]), 1)
    except ValueError:
        raise ValueError(f"first week {first_week!r} is not a week of "
                         f"{m[1]}") from None
    if isinstance(weeks, bool) or not isinstance(weeks, numbers.Integral) \
            or weeks < 0:
        raise ValueError(f"weeks must be a whole number of weeks, got "
                         f"{weeks!r}")
    if weeks == 0:
        return 0.0
    return fixed_months_between(monday,
                                monday + dt.timedelta(days=7 * weeks - 1))


def year_weeks_fixed_months(yread) -> float:
    """Months of fixed cost for one engine label-year read: its `weeks`
    ISO weeks from its `first_week` (iso_weeks_fixed_months). A read that
    covers the whole ISO year needs no first_week (its weeks are W01 on); a
    PART year without one cannot be placed and raises - its fixed cost is
    never guessed. The weeks must lie inside the label-year."""
    year = int(yread.year)
    weeks = yread.weeks
    n = dt.date(year, 12, 28).isocalendar()[1]
    if isinstance(weeks, bool) or not isinstance(weeks, numbers.Integral) \
            or not 0 <= weeks <= n:
        raise ValueError(f"{year} has {n} ISO weeks; got weeks={weeks!r}")
    first = getattr(yread, "first_week", None)
    if first is None:
        if weeks not in (0, n):
            raise ValueError(
                f"year {year}: this run was read without cost drivers "
                f"(first_week is None, and {weeks} of its {n} ISO weeks do "
                f"not say which) - its fixed cost cannot be pro-rated")
        first = f"{year}-W01"
    m = _ISO_WEEK_RE.match(str(first))
    if not m or int(m[1]) != year or int(m[2]) + weeks - 1 > n:
        raise ValueError(f"year {year}: first week {first!r} and {weeks} "
                         f"week(s) do not lie inside the year's {n} ISO "
                         f"weeks")
    return iso_weeks_fixed_months(first, weeks)


def year_cost(yread, costs) -> dict:
    """period_cost for one engine label-year. `yread` must carry the cost
    drivers `feed_kg_by_type` and `eggs` (and `first_week` for a part
    year). A read without them raises ValueError. Its cost is never passed
    off as 0. The fixed cost covers the calendar days of the year's weeks
    (year_weeks_fixed_months), as the CostsAndProfit sheet charges days."""
    feed = getattr(yread, "feed_kg_by_type", None)
    eggs = getattr(yread, "eggs", None)
    if feed is None or eggs is None:
        gone = [n for n, v in (("feed_kg_by_type", feed), ("eggs", eggs))
                if v is None]
        raise ValueError(
            f"year {getattr(yread, 'year', '?')}: this run was read without "
            f"cost drivers ({', '.join(gone)} is None) - it cannot be priced")
    return period_cost(feed, eggs, year_weeks_fixed_months(yread), costs)


def cost_per_kg_hog(total, hog_kg) -> Optional[float]:
    """total / HOG kg, or None when nothing was harvested. Never 0, never a
    division by zero."""
    if total is None or hog_kg is None or hog_kg <= 0:
        return None
    return total / hog_kg


def _rollup(period: str, kind: str, rows: list) -> dict:
    out: dict = {"period": period, "kind": kind}
    for f in _SUM_FIELDS:
        vals = [r[f] for r in rows]
        out[f] = None if any(v is None for v in vals) else sum(vals)
    out["unpriced_types"] = list(dict.fromkeys(
        t for r in rows for t in r["unpriced_types"]))
    out["cost_per_kg_hog"] = cost_per_kg_hog(out["total"], out["hog_kg"])
    out["variable_per_kg_hog"] = cost_per_kg_hog(out["variable"],
                                                 out["hog_kg"])
    return out


def monthly_pl(feed_by_type_month, eggs_by_month, revenue_by_month,
               hog_kg_by_month, month_days, costs) -> dict:
    """Cash-view P&L by calendar month: {"months": [...], "years": [...],
    "total": {...}}.

    Inputs are keyed by "YYYY-MM":
      feed_by_type_month  {month: {model feed type: kg}}
      eggs_by_month       {month: eggs stocked}
      revenue_by_month    {month: revenue}, or None when revenue is not
                          priced (revenue and profit are then None)
      hog_kg_by_month     {month: HOG kg harvested}
      month_days          {month: (days covered, days in month)}; the fixed
                          cost pro-rates by it. "days in month" must match
                          the calendar.
    A month that is absent from a flow has none of that flow, except that a
    month with HOG kg > 0 must have a revenue entry when revenue is priced
    (pass 0 explicitly if it truly sold for nothing). Every month must have a
    month_days entry.

    Month rows carry period_cost's fields plus days_covered, days_in_month,
    hog_kg, revenue, profit, cost_per_kg_hog and variable_per_kg_hog. Year
    rows (one per calendar year) and the total row are sums of the month
    rows. Their per-kg figures are recomputed from those sums."""
    c = _priced(costs)
    for label, m in (("feed_by_type_month", feed_by_type_month),
                     ("eggs_by_month", eggs_by_month),
                     ("hog_kg_by_month", hog_kg_by_month),
                     ("month_days", month_days)):
        if not isinstance(m, Mapping):
            raise ValueError(f"monthly_pl: {label} must be a mapping keyed "
                             f"'YYYY-MM' (got {type(m).__name__})")
    if revenue_by_month is not None and not isinstance(revenue_by_month,
                                                       Mapping):
        raise ValueError("monthly_pl: revenue_by_month must be a mapping "
                         "keyed 'YYYY-MM' or None")
    keys = (set(feed_by_type_month) | set(eggs_by_month)
            | set(hog_kg_by_month) | set(month_days)
            | set(revenue_by_month or {}))
    for m in keys:
        if not isinstance(m, str) or not _MONTH_RE.match(m):
            raise ValueError(f"monthly_pl: month key {m!r} is not 'YYYY-MM'")
    rows: list = []
    for m in sorted(keys):
        if m not in month_days:
            raise ValueError(f"monthly_pl: {m} has no month_days entry - its "
                             f"fixed cost cannot be pro-rated")
        try:
            covered, in_month = month_days[m]
        except (TypeError, ValueError):
            raise ValueError(f"monthly_pl: {m} month_days must be (days "
                             f"covered, days in month) (got "
                             f"{month_days[m]!r})") from None
        covered = _real(covered, f"{m} days covered")
        in_month = _real(in_month, f"{m} days in month")
        actual = calendar.monthrange(int(m[:4]), int(m[5:7]))[1]
        if in_month != actual:
            raise ValueError(f"monthly_pl: {m} has {actual} days, but "
                             f"month_days says {in_month:g}")
        if covered > in_month:
            raise ValueError(f"monthly_pl: {m} covers {covered:g} of "
                             f"{in_month:g} days")
        pc = period_cost(feed_by_type_month.get(m, {}),
                         eggs_by_month.get(m, 0), covered / in_month, c)
        hog = _real(hog_kg_by_month.get(m, 0), f"{m} HOG kg")
        if revenue_by_month is None:
            rev = None
        elif hog > 0 and m not in revenue_by_month:
            raise ValueError(f"monthly_pl: {m} harvests {hog:g} kg HOG but "
                             f"has no revenue entry - pass 0 explicitly if "
                             f"it truly sold for nothing")
        else:
            rev = _real(revenue_by_month.get(m, 0), f"{m} revenue")
        row = {"period": m, "kind": "Month", "days_covered": covered,
               "days_in_month": in_month, **pc, "hog_kg": hog,
               "revenue": rev,
               "profit": None if rev is None else rev - pc["total"]}
        row["cost_per_kg_hog"] = cost_per_kg_hog(row["total"], hog)
        row["variable_per_kg_hog"] = cost_per_kg_hog(row["variable"], hog)
        rows.append(row)
    years = [_rollup(y, "Year", [r for r in rows if r["period"][:4] == y])
             for y in sorted({r["period"][:4] for r in rows})]
    return {"months": rows, "years": years,
            "total": _rollup("Total", "Total", rows)}
