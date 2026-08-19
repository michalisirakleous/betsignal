"""Independent verification tests for combo bankroll tracking."""
from __future__ import annotations

import pytest

import value_bet_agent as vba


@pytest.fixture(autouse=True)
def tmp_bankroll(tmp_path, monkeypatch):
    monkeypatch.setattr(vba, "BANKROLL_FILE", str(tmp_path / "bankroll.json"))
    yield


def _pick(day, result, odds=2.0, pid=1):
    return {
        "date": day,
        "day_group_id": day,
        "source": "fd",
        "fixture_id": pid,
        "market_key": "home",
        "match": f"Match {pid}",
        "pick": "Home win",
        "odds": odds,
        "model_prob_raw": 0.6,
        "result": result,
    }


class TestLoadBankroll:
    def test_first_run_default(self, tmp_path):
        b = vba.load_bankroll()
        assert b["balance"] == vba.STARTING_BANKROLL
        assert b["starting_balance"] == vba.STARTING_BANKROLL


class TestResolveDailyCombos:
    def test_combo_win_all_legs(self):
        history = {"picks": [_pick("D1", "win", 2.0, 1), _pick("D1", "win", 1.5, 2)]}
        bankroll = vba.default_bankroll()
        bankroll = vba.resolve_daily_combos(history, bankroll)
        assert history["computed_combos"]["D1"]["result"] == "win"
        assert bankroll["balance"] == pytest.approx(vba.STARTING_BANKROLL + vba.DAILY_COMBO_STAKE * (3.0 - 1))

    def test_combo_loss_any_leg_lost(self):
        history = {"picks": [_pick("D1", "win", 2.0, 1), _pick("D1", "loss", 1.8, 2)]}
        bankroll = vba.default_bankroll()
        bankroll = vba.resolve_daily_combos(history, bankroll)
        assert history["computed_combos"]["D1"]["result"] == "loss"
        assert bankroll["balance"] == vba.STARTING_BANKROLL - vba.DAILY_COMBO_STAKE

    def test_all_void_no_change(self):
        history = {"picks": [_pick("D1", "void", 2.0, 1), _pick("D1", "void", 1.5, 2)]}
        bankroll = vba.default_bankroll()
        bankroll = vba.resolve_daily_combos(history, bankroll)
        assert history["computed_combos"]["D1"]["result"] == "all_void"
        assert bankroll["balance"] == vba.STARTING_BANKROLL

    def test_partial_void_excluded_from_combined_odds(self):
        history = {"picks": [
            _pick("D1", "void", 2.0, 1),
            _pick("D1", "win", 1.8, 2),
            _pick("D1", "win", 1.6, 3),
        ]}
        bankroll = vba.default_bankroll()
        bankroll = vba.resolve_daily_combos(history, bankroll)
        expected_odds = 1.8 * 1.6
        assert history["computed_combos"]["D1"]["combined_odds"] == pytest.approx(round(expected_odds, 2))

    def test_pending_day_skipped(self):
        history = {"picks": [_pick("D1", "win", 2.0, 1), _pick("D1", None, 1.8, 2)]}
        bankroll = vba.default_bankroll()
        bankroll = vba.resolve_daily_combos(history, bankroll)
        assert "D1" not in history.get("computed_combos", {})
        assert bankroll["balance"] == vba.STARTING_BANKROLL

    def test_already_computed_not_reapplied(self):
        history = {
            "picks": [_pick("D1", "win", 2.0, 1)],
            "computed_combos": {"D1": {"result": "win", "balance_delta": 5.0}},
        }
        bankroll = {"balance": 20.0, "starting_balance": 10.0}
        bankroll = vba.resolve_daily_combos(history, bankroll)
        assert bankroll["balance"] == 20.0

    def test_single_pick_combo(self):
        history = {"picks": [_pick("D1", "win", 2.5, 1)]}
        bankroll = vba.default_bankroll()
        bankroll = vba.resolve_daily_combos(history, bankroll)
        assert bankroll["balance"] == pytest.approx(vba.STARTING_BANKROLL + vba.DAILY_COMBO_STAKE * 1.5)


class TestBankrollWarnings:
    def test_no_warning_above_half(self):
        assert vba.bankroll_warning_lines({"balance": 8.0, "starting_balance": 10.0}) == []

    def test_warning_at_half(self):
        lines = vba.bankroll_warning_lines({"balance": 5.0, "starting_balance": 10.0})
        assert len(lines) == 1

    def test_warning_at_zero(self):
        lines = vba.bankroll_warning_lines({"balance": 0.0, "starting_balance": 10.0})
        assert len(lines) == 1


class TestComboSummaryLine:
    def test_combined_odds_and_prob(self):
        top = [{"odds": 2.0, "model_prob": 60.0}, {"odds": 1.5, "model_prob": 50.0}]
        line = vba.combo_summary_line(top)
        assert "3.00" in line
        assert "30.0%" in line

    def test_empty_top_returns_empty_string(self):
        assert vba.combo_summary_line([]) == ""
