"""Trade reason log: generated for every trade, layer by layer."""

from __future__ import annotations

from typing import Any

from engine.analytics.stats import ProbabilityEstimate


def confidence_1_to_5(p: float) -> int:
    for i, cut in enumerate((0.60, 0.65, 0.70, 0.75), start=1):
        if p < cut:
            return i
    return 5


def build_reason_log(**layers: Any) -> dict[str, Any]:
    """Expected keys: regime, bias, location, trigger, strike, events, probability(ProbabilityEstimate), plan, setup."""
    log = dict(layers)
    est: ProbabilityEstimate = log.pop("probability")
    log["probability"] = {
        "p": est.p,
        "label": est.label,
        "calibrated": est.calibrated,
        "model_version": est.model_version,
        "n_samples": est.n_samples,
        "components": {k: round(v, 4) for k, v in est.components.items()},
    }
    log["confidence"] = confidence_1_to_5(est.p)
    return log


def add_exit(log: dict[str, Any], *, outcome: str, legs: list[dict[str, Any]], net_pnl: float, r_multiple: float, review: str | None = None) -> dict[str, Any]:
    log = dict(log)
    log["exit"] = {"outcome": outcome, "legs": legs, "net_pnl": net_pnl, "r_multiple": r_multiple}
    if review:
        log["review"] = review
    return log


def render_text(log: dict[str, Any]) -> str:
    r, b, loc, trg, stk, pr, plan = (log.get(k, {}) for k in ("regime", "bias", "location", "trigger", "strike", "probability", "plan"))
    lines = [
        f"Setup: {log.get('setup')} ({log.get('direction')}) on {log.get('index')}",
        f"Regime: {r.get('label')} (conf {r.get('confidence', 0):.2f}) - {'; '.join(r.get('drivers', []))}",
        f"Bias: {b.get('label')} {b.get('score', 0):+.2f} - " + ", ".join(f"{d['name']} {d['value']:+.2f}" for d in b.get("drivers", [])),
        f"Location: {loc.get('zone_price')} [{', '.join(loc.get('sources', []))}], {loc.get('distance_pct', 0):.2f}% away",
        f"Trigger: strength {trg.get('strength', 0):.2f} - {'; '.join(trg.get('evidence', []))}",
        f"Strike: {stk.get('strike')} {stk.get('option_type')} exp {stk.get('expiry')} - delta {stk.get('delta')}, IV {stk.get('iv')}, gain/theta {stk.get('gain')}/{stk.get('theta_cost')}",
        f"Events: {', '.join(log.get('events', [])) or 'none'}",
        f"Probability: {pr.get('p')} [{pr.get('label')}, {pr.get('model_version')}] - confidence {log.get('confidence')}/5",
        f"Plan: entry ~{plan.get('entry')} SL {plan.get('sl')} T1 {plan.get('target_1')} T1.5 {plan.get('target_2')} then trail ({plan.get('trail')})",
    ]
    if "exit" in log:
        e = log["exit"]
        lines.append(f"Exit: {e['outcome']} net {e['net_pnl']:+.2f} ({e['r_multiple']:+.2f}R)")
    if "review" in log:
        lines.append(f"Review: {log['review']}")
    return "\n".join(lines)
