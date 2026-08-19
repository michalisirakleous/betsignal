#!/usr/bin/env python3
"""
Backtest script για το value-bet agent.

Διαβάζει state/pick_history.json και υπολογίζει βασικά metrics:
- Hit rate ανά confidence tier (προσεγγιστικά από model_prob_raw)
- Calibration curve (predicted vs actual ανά bucket)
- ROI με σταθερό stake (1 unit ανά pick)

Τρέξε το τοπικά: python backtest.py [--history path/to/pick_history.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_HISTORY = Path("state/pick_history.json")

# Αντίστοιχα thresholds με confidence_tier() — proxy tiers χωρίς edge/H2H στο history
TIER_HIGH_MIN_PROB = 0.60
TIER_MED_MIN_PROB = 0.50


def load_history(path: Path) -> dict:
    if not path.exists():
        print(f"Δεν βρέθηκε {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def tier_for_pick(model_prob_raw: float) -> str:
    if model_prob_raw >= TIER_HIGH_MIN_PROB:
        return "high (>=60% model)"
    if model_prob_raw >= TIER_MED_MIN_PROB:
        return "medium (50-60% model)"
    return "low (<50% model)"


def compute_metrics(picks: list[dict]) -> dict:
    resolved = [p for p in picks if p.get("result") in ("win", "loss")]
    voids = [p for p in picks if p.get("result") == "void"]
    pending = [p for p in picks if p.get("result") is None]

    tier_stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
    for p in resolved:
        tier = tier_for_pick(p["model_prob_raw"])
        tier_stats[tier]["total"] += 1
        if p["result"] == "win":
            tier_stats[tier]["wins"] += 1
        else:
            tier_stats[tier]["losses"] += 1

    # Calibration buckets (deciles)
    buckets: dict[str, dict] = defaultdict(lambda: {"predicted_sum": 0.0, "actual_wins": 0, "n": 0})
    for p in resolved:
        prob = p["model_prob_raw"]
        bucket_idx = min(int(prob * 10), 9)
        label = f"{bucket_idx * 10}-{(bucket_idx + 1) * 10}%"
        buckets[label]["predicted_sum"] += prob
        buckets[label]["n"] += 1
        if p["result"] == "win":
            buckets[label]["actual_wins"] += 1

    # ROI: flat 1-unit stake, void = push (0)
    stake = 0.0
    profit = 0.0
    for p in resolved:
        stake += 1.0
        if p["result"] == "win":
            profit += p["odds"] - 1.0
        else:
            profit -= 1.0
    roi = (profit / stake * 100) if stake > 0 else 0.0

    # By market type
    market_stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for p in resolved:
        mk = p.get("market_key", "unknown")
        if p["result"] == "win":
            market_stats[mk]["wins"] += 1
        else:
            market_stats[mk]["losses"] += 1

    return {
        "total_picks": len(picks),
        "resolved": len(resolved),
        "void": len(voids),
        "pending": len(pending),
        "overall_hit_rate": (sum(1 for p in resolved if p["result"] == "win") / len(resolved)) if resolved else 0,
        "avg_predicted_prob": (sum(p["model_prob_raw"] for p in resolved) / len(resolved)) if resolved else 0,
        "roi_pct": roi,
        "profit_units": profit,
        "staked_units": stake,
        "tier_stats": dict(tier_stats),
        "calibration_buckets": dict(buckets),
        "market_stats": dict(market_stats),
    }


def print_report(data: dict, history: dict) -> None:
    print("=" * 60)
    print("VALUE BET BACKTEST REPORT")
    print("=" * 60)
    print(f"Calibration factor (current): {history.get('calibration_factor', 1.0):.3f}")
    print(f"Total picks:    {data['total_picks']}")
    print(f"Resolved:       {data['resolved']}")
    print(f"Void/timeout:   {data['void']}")
    print(f"Pending:        {data['pending']}")
    print()

    if data["resolved"] == 0:
        print("Δεν υπάρχουν resolved picks για ανάλυση.")
        return

    print(f"Overall hit rate:     {data['overall_hit_rate']:.1%}")
    print(f"Avg predicted prob:   {data['avg_predicted_prob']:.1%}")
    print(f"Calibration gap:      {(data['overall_hit_rate'] - data['avg_predicted_prob']):+.1%}")
    print()
    print(f"Flat-stake ROI:       {data['roi_pct']:+.1f}%")
    print(f"Profit (units):       {data['profit_units']:+.2f} / {data['staked_units']:.0f} staked")
    print()

    print("--- Hit rate by confidence tier (model_prob proxy) ---")
    for tier, s in sorted(data["tier_stats"].items()):
        if s["total"] == 0:
            continue
        hr = s["wins"] / s["total"]
        print(f"  {tier:25s}  {s['wins']}/{s['total']}  ({hr:.1%})")
    print()

    print("--- Calibration curve (predicted vs actual) ---")
    for label in sorted(data["calibration_buckets"].keys(), key=lambda x: int(x.split("-")[0])):
        b = data["calibration_buckets"][label]
        if b["n"] == 0:
            continue
        avg_pred = b["predicted_sum"] / b["n"]
        actual = b["actual_wins"] / b["n"]
        print(f"  {label:8s}  n={b['n']:3d}  predicted={avg_pred:.1%}  actual={actual:.1%}  gap={actual - avg_pred:+.1%}")
    print()

    print("--- Hit rate by market ---")
    for mk, s in sorted(data["market_stats"].items()):
        total = s["wins"] + s["losses"]
        hr = s["wins"] / total if total else 0
        print(f"  {mk:10s}  {s['wins']}/{total}  ({hr:.1%})")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest value-bet pick history")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="Path to pick_history.json")
    args = parser.parse_args()

    history = load_history(args.history)
    picks = history.get("picks", [])
    metrics = compute_metrics(picks)
    print_report(metrics, history)


if __name__ == "__main__":
    main()
