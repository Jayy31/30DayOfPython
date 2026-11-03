"""Re-run a recorded trade under different sizing / trail settings on the same quote path."""

from __future__ import annotations

from engine.broker_paper.position import Outcome, Position
from engine.config.schemas import ChargesConfig, RulesConfig, TrailConfig


def resimulate(pos: Position, *, lots: int | None = None, rules: RulesConfig | None = None, charges: ChargesConfig | None = None, trail: TrailConfig | None = None) -> Position:
    """Same entry, same quotes, same decision times; only lots and/or exit settings differ."""
    r = rules or pos.rules
    if trail is not None:
        r = r.model_copy(update={"trail": trail})
    sim = Position(
        contract=pos.contract, exchange=pos.exchange, direction=pos.direction, lots=lots or pos.lots,
        entry_price=pos.entry_price, entry_ts=pos.entry_ts, rules=r, charges=charges or pos.charges_cfg,
        setup=pos.setup, probability=pos.probability, tags=pos.tags,
    )
    path = pos.path
    if not path:
        return sim
    for q in path:
        if not sim.is_open:
            break
        if q.ts.time() >= r.square_off:
            sim.square_off(q)
        else:
            sim.on_quote(q)
    if sim.is_open:   # path ended (data stopped) before the variant exited: close at the last quote
        sim.square_off(path[-1])
    return sim


def outcome_of(p: Position) -> Outcome | None:
    return p.outcome
