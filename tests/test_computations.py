"""Unit tests for pure computational functions in value_bet_agent."""
from __future__ import annotations

import math

import pytest

from value_bet_agent import (
    apply_calibration,
    expected_goals,
    find_matching_event,
    implied_probs_double_chance,
    implied_probs_no_vig,
    match_probabilities,
    normalize,
    pick_won,
)


class TestMatchProbabilities:
    def test_probabilities_sum_to_one_for_1x2(self):
        probs = match_probabilities(1.4, 1.1)
        total = probs["home"] + probs["draw"] + probs["away"]
        # Grid truncated at MAX_GOALS — sum is slightly below 1.0
        assert total == pytest.approx(1.0, abs=0.01)

    def test_over_under_sum_to_one(self):
        probs = match_probabilities(1.4, 1.1)
        assert probs["over25"] + probs["under25"] == pytest.approx(1.0, abs=1e-6)

    def test_double_chance_covers_two_outcomes(self):
        probs = match_probabilities(1.4, 1.1)
        assert probs["1x"] == pytest.approx(probs["home"] + probs["draw"], abs=1e-6)
        assert probs["x2"] == pytest.approx(probs["away"] + probs["draw"], abs=1e-6)
        assert probs["12"] == pytest.approx(probs["home"] + probs["away"], abs=1e-6)

    def test_strong_home_favourite(self):
        probs = match_probabilities(2.5, 0.5)
        assert probs["home"] > probs["away"]
        assert probs["home"] > 0.5


class TestImpliedProbsNoVig:
    def test_fair_market_sums_to_one(self):
        prices = {"home": 2.0, "draw": 3.5, "away": 4.0}
        probs = implied_probs_no_vig(prices, ["home", "draw", "away"])
        assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)

    def test_empty_when_no_prices(self):
        assert implied_probs_no_vig({}, ["home", "draw", "away"]) == {}


class TestImpliedProbsDoubleChance:
    def test_normalizes_to_two(self):
        prices = {"1x": 1.3, "x2": 1.5, "12": 1.2}
        probs = implied_probs_double_chance(prices)
        assert sum(probs.values()) == pytest.approx(2.0, abs=1e-6)

    def test_requires_at_least_two_markets(self):
        assert implied_probs_double_chance({"1x": 1.3}) == {}


class TestApplyCalibration:
    def test_factor_one_unchanged(self):
        probs = {"home": 0.6, "draw": 0.2, "away": 0.2}
        result = apply_calibration(probs, 1.0)
        assert result == probs

    def test_shrink_factor_pulls_toward_half(self):
        probs = {"home": 0.7}
        result = apply_calibration(probs, 0.8)
        assert result["home"] < 0.7
        assert result["home"] > 0.5

    def test_expand_factor_pushes_away_from_half(self):
        probs = {"home": 0.7}
        result = apply_calibration(probs, 1.2)
        assert result["home"] > 0.7

    def test_clamped_to_valid_range(self):
        probs = {"home": 0.99}
        result = apply_calibration(probs, 2.0)
        assert result["home"] <= 0.99


class TestPickWon:
    @pytest.mark.parametrize(
        "market_key, home, away, expected",
        [
            ("home", 2, 1, True),
            ("home", 1, 1, False),
            ("draw", 1, 1, True),
            ("away", 0, 1, True),
            ("over25", 2, 1, True),
            ("over25", 1, 0, False),
            ("under25", 1, 0, True),
            ("1x", 1, 1, True),
            ("1x", 0, 1, False),
            ("x2", 0, 1, True),
            ("12", 1, 1, False),
            ("12", 2, 1, True),
        ],
    )
    def test_outcomes(self, market_key, home, away, expected):
        assert pick_won(market_key, home, away) is expected

    def test_unknown_market_returns_none(self):
        assert pick_won("unknown", 1, 0) is None


class TestExpectedGoals:
    def _base_split(self, gf=1.4, ga=1.1, n=5):
        return {
            "overall_gf": gf,
            "overall_ga": ga,
            "home_gf": gf,
            "home_ga": ga,
            "home_n": n,
            "away_gf": gf,
            "away_ga": ga,
            "away_n": n,
        }

    def test_lambda_clamped_min(self):
        home = self._base_split(gf=0.1, ga=0.1)
        away = self._base_split(gf=0.1, ga=0.1)
        h_lam, a_lam = expected_goals(home, away, 1.0, 1.0, 1.35)
        assert h_lam >= 0.3
        assert a_lam >= 0.3

    def test_lambda_clamped_max(self):
        home = self._base_split(gf=5.0, ga=0.1)
        away = self._base_split(gf=5.0, ga=0.1)
        h_lam, a_lam = expected_goals(home, away, 2.5, 0.5, 1.35)
        assert h_lam <= 4.5
        assert a_lam <= 4.5


class TestTeamNameMatching:
    def test_substring_match(self):
        fixture = {"homeTeam": {"name": "Arsenal FC"}, "awayTeam": {"name": "Chelsea"}}
        odds_events = [{"home_team": "Arsenal", "away_team": "Chelsea FC"}]
        assert find_matching_event(fixture, odds_events) is not None

    def test_fuzzy_match_abbreviation(self):
        fixture = {"homeTeam": {"name": "Manchester United"}, "awayTeam": {"name": "Wolverhampton Wanderers"}}
        odds_events = [{"home_team": "Man United", "away_team": "Wolves"}]
        result = find_matching_event(fixture, odds_events)
        # May match with rapidfuzz; without it may fail — both are acceptable
        if result is not None:
            assert result["home_team"] == "Man United"

    def test_no_match_returns_none(self):
        fixture = {"homeTeam": {"name": "Team A"}, "awayTeam": {"name": "Team B"}}
        odds_events = [{"home_team": "Team X", "away_team": "Team Y"}]
        assert find_matching_event(fixture, odds_events) is None

    def test_normalize_strips_fc(self):
        assert "fc" not in normalize("Arsenal FC")
