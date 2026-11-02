"""Decision engine: regime -> bias -> location -> trigger -> strike -> probability -> risk.

Evaluates every enabled setup on both indices, records every candidate as a
signal (taken or rejected with reasons) and returns at most one trade plan:
the passing candidate with the highest confluence score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from engine.analytics.levels import nearest_zone
from engine.analytics.setups import MarketContext, SetupCandidate, load_setups
from engine.analytics.stats import PriorModel, ProbabilityEstimate, ProbabilityInputs
from engine.analytics.strike import StrikeCandidate, evaluate_strikes
from engine.broker_paper.fills import buy_fill_price, round_down_tick, round_up_tick
from engine.config.schemas import AppConfig
from engine.core.models import Direction, Index, OptionContract, OptionQuote
from engine.data.models import OptionChain
from engine.decision.reasons import build_reason_log
from engine.risk.engine import RiskEngine


@dataclass(frozen=True)
class IndexInputs:
    ctx: MarketContext
    chain: OptionChain
    lot_size: int
    reference_iv: float | None = None


@dataclass
class SignalRecord:
    ts: datetime
    index: Index
    setup: str
    direction: Direction
    score: float | None = None
    probability: ProbabilityEstimate | None = None
    taken: bool = False
    reject_reasons: list[str] = field(default_factory=list)
    layers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradePlan:
    index: Index
    contract: OptionContract
    direction: Direction
    setup: str
    score: float
    probability: ProbabilityEstimate
    est_entry: float
    planned_sl: float
    target_1: float
    target_2: float
    est_lots: int
    tags: tuple[str, ...]
    reason_log: dict[str, Any]


@dataclass
class DecisionResult:
    signals: list[SignalRecord]
    plan: TradePlan | None


class DecisionEngine:
    def __init__(self, cfg: AppConfig, risk: RiskEngine, priors: PriorModel | None = None) -> None:
        self.cfg = cfg
        self.dcfg = cfg.decision
        self.risk = risk
        self.priors = priors or PriorModel(cfg.decision.priors)
        self.setups = load_setups(cfg.decision.setups.enabled)

    def evaluate(self, now: datetime, inputs: list[IndexInputs], capital: float) -> DecisionResult:
        signals: list[SignalRecord] = []
        best: tuple[float, TradePlan, SignalRecord] | None = None
        for inp in inputs:
            for setup in self.setups:
                cand = setup.evaluate(inp.ctx)
                if cand is None:
                    continue
                sig, plan = self._assess(now, inp, cand, capital)
                signals.append(sig)
                if plan is not None and (best is None or plan.score > best[0]):
                    best = (plan.score, plan, sig)
        if best is None:
            return DecisionResult(signals, None)
        best[2].taken = True
        for s in signals:
            if s.reject_reasons == [] and not s.taken:
                s.reject_reasons.append("lower score than chosen candidate")
        return DecisionResult(signals, best[1])

    # ------------------------------------------------------------------
    def _assess(self, now: datetime, inp: IndexInputs, cand: SetupCandidate, capital: float) -> tuple[SignalRecord, TradePlan | None]:
        ctx, d = inp.ctx, self.dcfg
        s = 1 if cand.direction is Direction.LONG else -1
        sig = SignalRecord(ts=now, index=ctx.index, setup=cand.setup, direction=cand.direction)
        rej = sig.reject_reasons

        # 1. regime
        if cand.family not in ctx.regime.allowed_families:
            rej.append(f"family {cand.family} not allowed in {ctx.regime.label} regime")
        # 2. bias
        alignment = ctx.bias.score * s
        if alignment < -d.bias.max_counter_bias:
            rej.append(f"against bias ({ctx.bias.label} {ctx.bias.score:+.2f})")
        # 3. location
        zone = nearest_zone(list(ctx.zones), ctx.spot, d.location.max_distance_pct)
        if zone is None:
            rej.append("not at a confluence level")
        # 4. trigger
        if not ctx.trigger.supports(s, d.trigger.min_strength):
            rej.append(f"trigger not confirming (dir {ctx.trigger.direction:+d}, strength {ctx.trigger.strength:.2f})")
        # 5. strike
        exch = self.cfg.instruments[ctx.index.value].exchange  # type: ignore[index]
        strikes = evaluate_strikes(
            inp.chain, cand.direction, cand.target_points(ctx.spot), now,
            cfg=d.strike, charges=self.cfg.charges, exchange=exch, lot_size=inp.lot_size,
            capital=capital, min_delta=self.cfg.rules.min_delta, strike_range=self.cfg.rules.strike_range,
            reference_iv=inp.reference_iv,
        )
        pick: StrikeCandidate | None = strikes[0] if strikes and strikes[0].ok else None
        if pick is None:
            rej.append("no strike passes filters" + (f": {'; '.join(strikes[0].rejects)}" if strikes else ""))

        # 6. probability
        zone_sources = zone.source_count if zone else 0
        est = self.priors.estimate(
            ProbabilityInputs(
                setup=cand.setup,
                regime_confidence=ctx.regime.confidence,
                bias_alignment=alignment,
                zone_sources=zone_sources,
                trigger_strength=ctx.trigger.strength if ctx.trigger.direction == s else 0.0,
                strike_score=pick.score if pick else 0.0,
            )
        )
        sig.probability = est
        # 7. risk rules (locked) incl. probability threshold
        risk = self.risk.check_entry(now, ctx.index, cand.direction, est.p)
        rej.extend(risk.reasons)

        w = self.dcfg.score_weights
        score = (
            w.get("regime", 1) * ctx.regime.confidence
            + w.get("bias", 1) * max(alignment, 0)
            + w.get("location", 1) * min(zone_sources / 4, 1)
            + w.get("trigger", 1) * (ctx.trigger.strength if ctx.trigger.direction == s else 0)
            + w.get("strike", 1) * (max(min(pick.score, 1), 0) if pick else 0)
        )
        sig.score = round(score, 4)
        sig.layers = self._layers(ctx, cand, zone, pick)
        if sig.layers["strike"]:
            sig.layers["strike"]["expiry"] = inp.chain.expiry.isoformat()
        if rej or pick is None:
            return sig, None

        contract = OptionContract(
            index=ctx.index, expiry=inp.chain.expiry, strike=pick.row.strike,
            option_type=cand.direction.option_type, lot_size=inp.lot_size, symbol=pick.row.token,
        )
        q = OptionQuote(ts=now, bid=pick.row.bid, ask=pick.row.ask, ltp=pick.row.ltp)
        entry = buy_fill_price(q, self.cfg.charges)
        r = self.cfg.rules
        risk_unit = entry * r.sl_pct / 100
        tick = self.cfg.charges.tick_size
        plan_d = {
            "entry": entry,
            "sl": round_down_tick(entry - risk_unit, tick),
            "target_1": round_up_tick(entry + risk_unit * r.first_target_rr, tick),
            "target_2": round_up_tick(entry + risk_unit * r.second_target_rr, tick),
            "trail": f"{r.trail.timeframe_min}m swing low, {r.trail.pivot_bars} bars, -{r.trail.buffer_pct}%",
            "underlying_target": round(cand.target_level, 2),
            "underlying_invalidation": round(cand.invalidation, 2),
        }
        tags = tuple(risk.tags) + tuple(f"event:{e}" for e in ctx.events)
        log = build_reason_log(
            setup=cand.setup, direction=cand.direction.value, index=ctx.index.value,
            events=list(ctx.events), probability=est, plan=plan_d, tags=list(tags), score=sig.score, **sig.layers,
        )
        plan = TradePlan(
            index=ctx.index, contract=contract, direction=cand.direction, setup=cand.setup, score=score,
            probability=est, est_entry=entry, planned_sl=plan_d["sl"], target_1=plan_d["target_1"],
            target_2=plan_d["target_2"], est_lots=pick.lots, tags=tags, reason_log=log,
        )
        return sig, plan

    @staticmethod
    def _layers(ctx: MarketContext, cand: SetupCandidate, zone, pick: StrikeCandidate | None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        out: dict[str, Any] = {
            "regime": {"label": ctx.regime.label, "confidence": round(ctx.regime.confidence, 3), "drivers": list(ctx.regime.drivers)},
            "bias": {"label": ctx.bias.label, "score": round(ctx.bias.score, 3), "drivers": [{"name": x.name, "value": round(x.value, 3), "weight": x.weight} for x in ctx.bias.drivers]},
            "trigger": {"direction": ctx.trigger.direction, "strength": round(ctx.trigger.strength, 3), "evidence": list(ctx.trigger.evidence)},
            "setup_notes": list(cand.notes),
            "location": {},
            "strike": {},
        }
        if zone is not None:
            out["location"] = {"zone_price": round(zone.price, 2), "sources": sorted(set(zone.sources)), "distance_pct": round(abs(zone.price - ctx.spot) / ctx.spot * 100, 3)}
        if pick is not None and pick.greeks is not None:
            out["strike"] = {
                "strike": pick.row.strike, "option_type": pick.row.option_type.value, "expiry": None,
                "delta": round(pick.greeks.delta, 3), "iv": round(pick.greeks.iv, 4),
                "gain": round(pick.expected_gain, 2), "theta_cost": round(pick.theta_cost, 2), "score": round(pick.score, 3),
                "bid": pick.row.bid, "ask": pick.row.ask,
            }
        return out
