"""Independent verification tests for odds API quota tracking (Claude's own tests,
not copy-pasted from Cursor, to verify behavior against the real implementation)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import value_bet_agent as vba


@pytest.fixture(autouse=True)
def reset_globals(tmp_path, monkeypatch):
    monkeypatch.setattr(vba, "ODDS_API_USAGE_FILE", str(tmp_path / "odds_api_usage.json"))
    vba._odds_usage_state = None
    vba._sport_keys_no_double_chance = set()
    vba._disable_double_chance_this_run = False
    vba._odds_api_calls_this_run = 0
    yield
    vba._odds_usage_state = None
    vba._sport_keys_no_double_chance = set()
    vba._disable_double_chance_this_run = False


class TestMonthKeyAndProjection:
    def test_current_month_key_format(self):
        dt = datetime(2026, 8, 19, tzinfo=timezone.utc)
        assert vba.current_month_key(dt) == "2026-08"

    def test_days_in_month_august(self):
        assert vba.days_in_month(2026, 8) == 31

    def test_projected_monthly_calls_midmonth(self):
        # 15 calls by day 15 of a 31-day month -> ~31 projected
        dt = datetime(2026, 8, 15, tzinfo=timezone.utc)
        projected = vba.projected_monthly_calls(15, dt)
        assert projected == pytest.approx(31.0, abs=0.1)

    def test_projected_monthly_calls_day_one_no_division_by_zero(self):
        dt = datetime(2026, 8, 1, tzinfo=timezone.utc)
        # should not raise ZeroDivisionError
        result = vba.projected_monthly_calls(5, dt)
        assert result > 0


class TestLoadOddsApiUsage:
    def test_first_run_no_file(self, tmp_path):
        data = vba.load_odds_api_usage()
        assert data["calls"] == 0
        assert data["sport_keys_no_double_chance"] == []
        assert data["month"] == vba.current_month_key()

    def test_resets_on_new_month(self, tmp_path):
        old_month_data = {"month": "2020-01", "calls": 400, "sport_keys_no_double_chance": ["soccer_epl"]}
        vba.save_json_state(vba.ODDS_API_USAGE_FILE, old_month_data)
        data = vba.load_odds_api_usage()
        assert data["calls"] == 0
        assert data["sport_keys_no_double_chance"] == []
        assert data["month"] == vba.current_month_key()

    def test_persists_within_same_month(self, tmp_path):
        current_data = {
            "month": vba.current_month_key(),
            "calls": 42,
            "sport_keys_no_double_chance": ["soccer_epl"],
        }
        vba.save_json_state(vba.ODDS_API_USAGE_FILE, current_data)
        data = vba.load_odds_api_usage()
        assert data["calls"] == 42
        assert data["sport_keys_no_double_chance"] == ["soccer_epl"]


class TestRecordOddsApiCall:
    def test_increments_and_persists(self, tmp_path):
        vba.init_odds_api_usage()
        vba.record_odds_api_call()
        vba.record_odds_api_call()
        reloaded = vba.load_odds_api_usage()
        assert reloaded["calls"] == 2

    def test_mark_sport_key_persists_across_init(self, tmp_path):
        vba.init_odds_api_usage()
        vba.mark_sport_key_no_double_chance("soccer_epl")
        # Simulate a fresh run: reset in-memory state, re-init
        vba._odds_usage_state = None
        vba._sport_keys_no_double_chance = set()
        vba.init_odds_api_usage()
        assert "soccer_epl" in vba._sport_keys_no_double_chance


class TestAdaptiveThrottle:
    def test_throttle_disabled_when_low_usage(self, tmp_path):
        data = {"month": vba.current_month_key(), "calls": 10, "sport_keys_no_double_chance": []}
        vba.save_json_state(vba.ODDS_API_USAGE_FILE, data)
        vba.init_odds_api_usage()
        assert vba.is_double_chance_disabled_this_run() is False

    def test_throttle_enabled_when_projected_over_threshold(self, tmp_path):
        # Force a high call count early in the month -> high projection
        dt = datetime.now(timezone.utc)
        data = {"month": vba.current_month_key(), "calls": 9999, "sport_keys_no_double_chance": []}
        vba.save_json_state(vba.ODDS_API_USAGE_FILE, data)
        vba.init_odds_api_usage()
        assert vba.is_double_chance_disabled_this_run() is True


class TestOddsApiUsagePercent:
    def test_percent_calculation(self, tmp_path):
        data = {"month": vba.current_month_key(), "calls": 250, "sport_keys_no_double_chance": []}
        vba.save_json_state(vba.ODDS_API_USAGE_FILE, data)
        vba.init_odds_api_usage()
        assert vba.odds_api_usage_percent() == pytest.approx(50.0, abs=0.1)
