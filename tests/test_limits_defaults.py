"""Capacity limits as an editable operator input.

A capacity is a fact about the facility. It used to be expressed as 3,120
near-identical rows in scenario/limits.yaml — one per (week, system, metric)
— which made the rare change impossible to do by hand and the actual value
invisible. It is now stated ONCE per system, with per-week rows kept as the
exception.

What these tests hold in place:

* the PRECEDENCE chain, all four rungs, including that each rung really is
  beaten by the one above it;
* the 6N MODE BOUNDARY landing on exactly the week `sixn_production_start`
  falls in, and following that date when it moves;
* MIGRATION EQUIVALENCE — the operator's live file, before and after,
  resolves to the same cap for every (week, system, metric) the old file
  covered. This is the test that makes the migration safe;
* a missing cap still RAISES naming its address, in both engines;
* the config snapshot round-tripping the new schema.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from forecast.caps import (
    METRIC_BIOMASS, METRIC_FEED_DAY, MODE_PRODUCTION, MODE_PURGE,
    SystemLimits, carry_forward_cap_lookup, require_system_cap,
    resolve_system_cap, week_label_start,
)
from forecast.scenario_io import (
    limits_yaml_text, load_limits, system_defaults_from_dict,
    system_defaults_to_dict,
)

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenario"
CONFIG_DIR = ROOT / "config"

# The legacy row-only file stated ONE value per (system, metric) for every
# week, except OG6N biomass which stepped at sixn_production_start. That rule
# — not a copy of the 12,494-line file — is the thing the migration has to
# reproduce; it was verified against the real file before the migration ran
# (3,120 rows, 3 distinct values, the step at 2028-W01 and nowhere else).
LEGACY_UNIFORM = {METRIC_BIOMASS: 400_000.0, METRIC_FEED_DAY: 3_000.0}
LEGACY_6N_PURGE_BIOMASS = 700_000.0
LEGACY_FIRST_WEEK = "2026-W27"
LEGACY_LAST_WEEK = "2028-W51"
LEGACY_SYSTEMS = ["OG1N", "OG1S", "OG2N", "OG2S", "OG3N", "OG3S",
                  "OG4N", "OG4S", "OG5N", "OG5S", "OG6N", "OG6S"]


def _control(production_start="2028-01-01", growth=False):
    return SimpleNamespace(
        sixn_growth=growth,
        sixn_production_start=(datetime.fromisoformat(production_start)
                               if production_start else None))


def _legacy_weeks():
    """The 130 absolute ISO labels the old file covered, 2026-W27..2028-W51."""
    out, d = [], week_label_start(LEGACY_FIRST_WEEK)
    end = week_label_start(LEGACY_LAST_WEEK)
    while d <= end:
        y, w, _ = d.isocalendar()
        out.append(f"{y}-W{w:02d}")
        d = date.fromordinal(d.toordinal() + 7)
    return out


def _legacy_cap(week_label, system_id, metric):
    """What the row-only file resolved to for this cell."""
    if system_id == "OG6N" and metric == METRIC_BIOMASS:
        if week_label_start(week_label) < date(2028, 1, 1):
            return LEGACY_6N_PURGE_BIOMASS
    return LEGACY_UNIFORM[metric]


# =========================================================================== #
class TestPrecedence:
    """per-week row > system+mode default > system default > absent."""

    def _sl(self, **kw):
        sl = SystemLimits(
            caps={("2027-W10", "OG6N", METRIC_BIOMASS): 111.0},
            defaults={("OG6N", METRIC_BIOMASS): 333.0},
            mode_defaults={("OG6N", MODE_PURGE, METRIC_BIOMASS): 222.0},
            **kw)
        return sl.bind_sixn_mode(_control())

    def test_a_per_week_row_beats_everything(self):
        assert self._sl().resolve("2027-W10", "OG6N", METRIC_BIOMASS) == 111.0

    def test_a_mode_default_beats_the_system_default(self):
        # 2027-W11 has no row, and is in purge mode (before 2028-01-01).
        assert self._sl().resolve("2027-W11", "OG6N", METRIC_BIOMASS) == 222.0

    def test_the_system_default_applies_where_no_mode_default_does(self):
        # Production mode has no mode default here, so it falls to 333.
        assert self._sl().resolve("2028-W10", "OG6N", METRIC_BIOMASS) == 333.0

    def test_absent_stays_absent(self):
        """A cap nobody set must not become an invented number."""
        assert self._sl().resolve("2027-W11", "OG6N", METRIC_FEED_DAY) is None
        assert self._sl().resolve("2027-W11", "OG3N", METRIC_BIOMASS) is None

    def test_the_module_level_resolver_agrees_with_the_method(self):
        sl = self._sl()
        for wk in ("2027-W10", "2027-W11", "2028-W10"):
            assert (resolve_system_cap(METRIC_BIOMASS, wk, "OG6N", sl)
                    == sl.resolve(wk, "OG6N", METRIC_BIOMASS))

    def test_a_default_covers_every_week_there_is(self):
        """The point of the redesign: no horizon can fall off the end.

        The row-only file covered 2026-W27..2028-W51. The operator's own PR
        (closing 2026-08-12) runs to 2029-W05, so six weeks resolved to NO
        CAP AT ALL, invisibly. A default has no week axis.
        """
        sl = self._sl()
        for wk in ("2019-W01", "2029-W05", "2035-W52"):
            assert sl.resolve(wk, "OG6N", METRIC_BIOMASS) is not None


class TestSixNModeBoundary:
    def _sl(self, **kw):
        return SystemLimits(mode_defaults={
            ("OG6N", MODE_PURGE, METRIC_BIOMASS): 700_000.0,
            ("OG6N", MODE_PRODUCTION, METRIC_BIOMASS): 400_000.0,
        }, **kw)

    def test_the_step_lands_on_the_week_containing_the_start_date(self):
        sl = self._sl().bind_sixn_mode(_control("2028-01-01"))
        # 2027-W52 starts 2027-12-27 (before) -> purge.
        assert sl.mode_for_week("2027-W52") == MODE_PURGE
        assert sl.resolve("2027-W52", "OG6N", METRIC_BIOMASS) == 700_000.0
        # 2028-W01 starts 2028-01-03 (on/after) -> production.
        assert sl.mode_for_week("2028-W01") == MODE_PRODUCTION
        assert sl.resolve("2028-W01", "OG6N", METRIC_BIOMASS) == 400_000.0

    def test_the_boundary_follows_the_date_when_it_moves(self):
        """The step is DERIVED, so retuning Control moves it. Under the old
        file it was baked into 130 rows and would simply have disagreed."""
        sl = self._sl().bind_sixn_mode(_control("2027-01-01"))
        assert sl.resolve("2027-W10", "OG6N", METRIC_BIOMASS) == 400_000.0

    def test_run_6n_as_growout_makes_every_week_production(self):
        sl = self._sl().bind_sixn_mode(_control("2028-01-01", growth=True))
        assert sl.resolve("2026-W30", "OG6N", METRIC_BIOMASS) == 400_000.0

    def test_no_production_start_makes_every_week_purge(self):
        sl = self._sl().bind_sixn_mode(_control(None))
        assert sl.resolve("2030-W30", "OG6N", METRIC_BIOMASS) == 700_000.0

    def test_it_agrees_with_the_engines_own_purge_rule(self):
        """caps and the 6N phase machine must not have two boundaries."""
        from forecast.sixn import is_purge_mode
        c = _control("2028-01-01")
        sl = self._sl().bind_sixn_mode(c)
        for wk in ("2026-W27", "2027-W52", "2028-W01", "2028-W20"):
            expected = (MODE_PURGE if is_purge_mode(c, week_label_start(wk))
                        else MODE_PRODUCTION)
            assert sl.mode_for_week(wk) == expected

    def test_resolving_unbound_mode_defaults_raises_instead_of_guessing(self):
        """Half a horizon under the wrong ceiling would read as a planner
        defect, not a config error — so it must not be possible."""
        with pytest.raises(ValueError, match="never bound to Control"):
            self._sl().resolve("2027-W10", "OG6N", METRIC_BIOMASS)

    def test_a_file_without_mode_defaults_needs_no_binding(self):
        sl = SystemLimits(defaults={("OG3N", METRIC_BIOMASS): 5.0})
        assert sl.resolve("2027-W10", "OG3N", METRIC_BIOMASS) == 5.0


class TestMigrationEquivalence:
    """The deliverable that makes the migration safe."""

    @pytest.fixture(scope="class")
    def live(self):
        if not (SCENARIO_DIR / "limits.yaml").exists():
            pytest.skip("no scenario/limits.yaml")
        from forecast.config_io import load_control
        return load_limits(SCENARIO_DIR, load_control(CONFIG_DIR))[1]

    def test_every_cell_the_row_only_file_covered_resolves_identically(self, live):
        weeks = _legacy_weeks()
        assert len(weeks) == 130 and weeks[-1] == LEGACY_LAST_WEEK
        diffs = []
        for wk in weeks:
            for s in LEGACY_SYSTEMS:
                for m in (METRIC_BIOMASS, METRIC_FEED_DAY):
                    before = _legacy_cap(wk, s, m)
                    after = live.resolve(wk, s, m)
                    if before != after:
                        diffs.append((wk, s, m, before, after))
        assert not diffs, f"{len(diffs)} cap(s) changed, first 5: {diffs[:5]}"

    def test_it_really_swept_the_whole_horizon(self):
        """Guard the guard: a proof over an empty set proves nothing."""
        assert len(_legacy_weeks()) * len(LEGACY_SYSTEMS) * 2 == 3120

    def test_the_file_now_states_each_capacity_once(self, live):
        """NEGATIVE CONTROL: fails on the parent commit, where limits.yaml
        held 3,120 per-week rows and no defaults at all."""
        assert live.defaults, "no system_defaults — the migration did not run"
        assert len(live.caps) < 100, (
            f"{len(live.caps)} per-week rows remain; the uniform ones should "
            f"have collapsed into system_defaults")

    def test_the_6n_split_survived_the_migration(self, live):
        assert live.resolve("2026-W40", "OG6N", METRIC_BIOMASS) == 700_000.0
        assert live.resolve("2028-W20", "OG6N", METRIC_BIOMASS) == 400_000.0

    def test_every_og_system_has_both_capacities(self, live):
        for s in LEGACY_SYSTEMS:
            for m in (METRIC_BIOMASS, METRIC_FEED_DAY):
                assert live.resolve("2027-W01", s, m) is not None, (s, m)


class TestMissingCapStillRaisesWithItsAddress:
    """No capacity number may live in code (guardrail preserved from the
    previous pass, and now extended to the L3 planner, which still held a
    literal 400,000 / 3,000)."""

    def test_require_system_cap_names_the_absent_input(self):
        sl = SystemLimits()
        with pytest.raises(ValueError) as e:
            require_system_cap(METRIC_BIOMASS, "2027-W10", "OG4S", sl)
        msg = str(e.value)
        assert "OG4S" in msg and "2027-W10" in msg and METRIC_BIOMASS in msg
        assert "system_defaults" in msg          # tells you where to put it
        assert "will not invent one" in msg

    def test_the_milp_placement_pass_raises(self):
        from forecast.global_placement_milp_poc import _scap, _DEFAULT_BIO_CAP
        assert _DEFAULT_BIO_CAP is None
        with pytest.raises(ValueError, match="OG4S"):
            _scap(METRIC_BIOMASS, "2027-W10", "OG4S", SystemLimits(),
                  _DEFAULT_BIO_CAP)

    def test_the_l3_planner_raises_too(self):
        from forecast.global_planner_l3_poc import (
            _system_cap, _DEFAULT_BIO_CAP, _DEFAULT_FEED_CAP)
        assert (_DEFAULT_BIO_CAP, _DEFAULT_FEED_CAP) == (None, None)
        with pytest.raises(ValueError, match="OG4S"):
            _system_cap(METRIC_BIOMASS, "2027-W10", "OG4S", SystemLimits(),
                        _DEFAULT_BIO_CAP)

    def test_no_capacity_literal_is_left_in_the_planners(self):
        """NEGATIVE CONTROL: passes only because L3's literals are gone."""
        for mod in ("global_planner_l3_poc.py", "global_placement_milp_poc.py"):
            src = (ROOT / "forecast" / mod).read_text(encoding="utf-8")
            for line in src.splitlines():
                if line.lstrip().startswith("#"):
                    continue                     # prose may quote the history
                assert "_DEFAULT_BIO_CAP = 400000" not in line.replace("_", "")
                assert "400000.0" not in line.replace("_", ""), (mod, line)


class TestSerializerRoundTrip:
    def _sl(self):
        return SystemLimits(
            caps={("2027-W10", "OG3N", METRIC_BIOMASS): 1.0},
            defaults={("OG3N", METRIC_BIOMASS): 2.0,
                      ("OG6N", METRIC_FEED_DAY): 3.0},
            mode_defaults={("OG6N", MODE_PURGE, METRIC_BIOMASS): 4.0,
                           ("OG6N", MODE_PRODUCTION, METRIC_BIOMASS): 5.0})

    def test_defaults_survive_a_dict_round_trip(self):
        sl = self._sl()
        d, md = system_defaults_from_dict(system_defaults_to_dict(sl))
        assert d == sl.defaults and md == sl.mode_defaults

    def test_the_written_file_reloads_to_the_same_caps(self, tmp_path):
        from forecast.caps import FacilityLimits
        (tmp_path / "limits.yaml").write_text(
            limits_yaml_text(FacilityLimits(), self._sl()), encoding="utf-8")
        (tmp_path / "batches.yaml").write_text("batches: []\n", encoding="utf-8")
        _fl, back = load_limits(tmp_path, _control())
        sl = self._sl()
        assert (back.caps, back.defaults, back.mode_defaults) == (
            sl.caps, sl.defaults, sl.mode_defaults)

    def test_the_header_documents_the_precedence_it_implements(self):
        from forecast.caps import FacilityLimits
        text = limits_yaml_text(FacilityLimits(), self._sl())
        assert "Precedence" in text and "system_defaults" in text
        assert yaml.safe_load(text)["system_defaults"]["OG6N"]["modes"]

    def test_a_legacy_row_only_file_still_loads(self, tmp_path):
        """Nothing already configured breaks."""
        (tmp_path / "limits.yaml").write_text(
            "facility: []\nsystem:\n- {week: 2026-W27, system: OG1N, "
            "metric: biomass, value: 400000.0}\n", encoding="utf-8")
        _fl, sl = load_limits(tmp_path)
        assert sl.resolve("2026-W27", "OG1N", METRIC_BIOMASS) == 400_000.0
        assert sl.resolve("2026-W28", "OG1N", METRIC_BIOMASS) is None


class TestCarryForwardLookup:
    """The reporting sweep (SystemLimitsAudit / LNS) — one shared copy."""

    def test_defaults_win_over_a_carried_exception(self):
        """An exception is for ONE week. Carrying it into every later week
        would be a silent policy change; the default sits above it."""
        sl = SystemLimits(caps={("2027-W10", "OG3N", METRIC_BIOMASS): 9.0},
                          defaults={("OG3N", METRIC_BIOMASS): 5.0})
        cap = carry_forward_cap_lookup(sl)
        assert cap("2027-W10", "OG3N", METRIC_BIOMASS) == 9.0
        assert cap("2027-W11", "OG3N", METRIC_BIOMASS) == 5.0

    def test_a_row_only_file_still_carries_forward(self):
        """Compatibility: with no default there is nothing else to report."""
        sl = SystemLimits(caps={("2027-W10", "OG3N", METRIC_BIOMASS): 9.0})
        cap = carry_forward_cap_lookup(sl)
        assert cap("2027-W11", "OG3N", METRIC_BIOMASS) == 9.0
        assert cap("2027-W01", "OG3N", METRIC_BIOMASS) == 9.0

    def test_the_audit_and_the_optimizer_use_the_same_lookup(self):
        from forecast import lns_placement
        sl = SystemLimits(defaults={("OG3N", METRIC_BIOMASS): 5.0})
        assert (lns_placement._carry_forward_caps(sl)(
            "2027-W11", "OG3N", METRIC_BIOMASS) == 5.0)


class TestSnapshotRoundTrip:
    def test_the_new_schema_survives_snapshot_and_import(self, tmp_path):
        import openpyxl
        from forecast.caps import FacilityLimits
        from forecast.config_snapshot import (
            import_config_snapshot, read_config_snapshot, write_config_snapshot)

        cfg, scn = tmp_path / "config", tmp_path / "scenario"
        cfg.mkdir(); scn.mkdir()
        for n in ("control.yaml", "biology.yaml", "facility.yaml"):
            (cfg / n).write_text("{}\n", encoding="utf-8")
        (scn / "batches.yaml").write_text("batches: []\n", encoding="utf-8")
        sl = SystemLimits(
            defaults={("OG3N", METRIC_BIOMASS): 400_000.0},
            mode_defaults={("OG6N", MODE_PURGE, METRIC_BIOMASS): 700_000.0})
        (scn / "limits.yaml").write_text(
            limits_yaml_text(FacilityLimits(), sl), encoding="utf-8")

        wb = openpyxl.Workbook()
        write_config_snapshot(wb, cfg, scn)
        assert "scenario/limits.yaml" in read_config_snapshot(wb)

        out_cfg, out_scn = tmp_path / "c2", tmp_path / "s2"
        out_cfg.mkdir(); out_scn.mkdir()
        import_config_snapshot(wb, out_cfg, out_scn)
        _fl, back = load_limits(out_scn, _control())
        assert back.defaults == sl.defaults
        assert back.mode_defaults == sl.mode_defaults
        assert back.resolve("2027-W01", "OG6N", METRIC_BIOMASS) == 700_000.0


class TestExcelTemplateRoundTrip:
    """Configure → Template & import must not lose the capacities.

    The template's SystemLimits sheet is the per-week EXCEPTION grid, which is
    now empty — so without a sheet of its own, an export/import cycle would
    have written back a limits.yaml with no capacities at all and left the
    whole facility uncapped.
    """

    def _template(self, tmp_path):
        import openpyxl
        from forecast.config_template import write_config_template
        p = write_config_template(tmp_path / "t.xlsx", config_dir=CONFIG_DIR,
                                  scenario_dir=SCENARIO_DIR, horizon_weeks=3,
                                  forecast_start=date(2026, 8, 13))
        return openpyxl.load_workbook(p)

    @pytest.fixture(autouse=True)
    def _needs_live_config(self):
        if not (SCENARIO_DIR / "limits.yaml").exists():
            pytest.skip("no scenario/limits.yaml")

    def test_capacities_survive_export_then_import(self, tmp_path):
        from forecast.config_template import import_config_template
        cfg, scn = tmp_path / "c", tmp_path / "s"
        cfg.mkdir(); scn.mkdir()
        import_config_template(self._template(tmp_path), cfg, scn)
        _fl, sl = load_limits(scn, _control())
        assert sl.resolve("2026-W40", "OG6N", METRIC_BIOMASS) == 700_000.0
        assert sl.resolve("2028-W20", "OG6N", METRIC_BIOMASS) == 400_000.0
        assert sl.resolve("2026-W40", "OG3N", METRIC_BIOMASS) == 400_000.0

    def test_a_template_without_the_sheet_keeps_what_is_on_disk(self, tmp_path):
        """An older template has no SystemCapacities sheet. Importing it must
        not silently delete every capacity."""
        import shutil
        from forecast.config_template import import_config_template
        cfg, scn = tmp_path / "c2", tmp_path / "s2"
        cfg.mkdir(); scn.mkdir()
        shutil.copy(SCENARIO_DIR / "limits.yaml", scn / "limits.yaml")
        wb = self._template(tmp_path)
        del wb["SystemCapacities"]
        import_config_template(wb, cfg, scn)
        _fl, sl = load_limits(scn, _control())
        assert sl.resolve("2026-W40", "OG6N", METRIC_BIOMASS) == 700_000.0
        assert sl.defaults, "the capacities were wiped by a legacy template"


class TestTheAppEditsOneCellPerCapacity:
    """The grid<->model mapping behind the compact editor."""

    def test_a_system_row_round_trips(self):
        import app
        defaults = {("OG3N", METRIC_BIOMASS): 400_000.0,
                    ("OG3N", METRIC_FEED_DAY): 3_000.0}
        metrics = [METRIC_BIOMASS, METRIC_FEED_DAY]
        recs = app._system_defaults_records(defaults, ["OG3N", "OG4N"], metrics)
        assert len(recs) == 2                      # one ROW per system
        assert recs[0] == {"system": "OG3N", METRIC_BIOMASS: 400_000.0,
                           METRIC_FEED_DAY: 3_000.0}
        assert app._system_defaults_from_records(recs, metrics) == defaults

    def test_a_blank_cell_means_no_cap_not_zero(self):
        import app
        recs = [{"system": "OG3N", METRIC_BIOMASS: None, METRIC_FEED_DAY: 5.0}]
        out = app._system_defaults_from_records(recs, [METRIC_BIOMASS,
                                                       METRIC_FEED_DAY])
        assert ("OG3N", METRIC_BIOMASS) not in out
        assert out[("OG3N", METRIC_FEED_DAY)] == 5.0

    def test_mode_rows_round_trip_and_half_filled_rows_are_dropped(self):
        import app
        md = {("OG6N", MODE_PURGE, METRIC_BIOMASS): 700_000.0}
        recs = app._mode_default_records(md)
        assert app._mode_defaults_from_records(recs) == md
        # The dynamic editor appends a blank row the moment you click +.
        assert app._mode_defaults_from_records(
            recs + [{"system": "", "mode": "", "metric": "", "value": None}]) == md
