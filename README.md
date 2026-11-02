# papertrade

Fully automated, intraday-only **paper-trading** system for Nifty and Sensex index options
(option buying only). It never calls a broker order API.

> Not financial advice. Most retail F&O traders lose money; this system exists to test the idea safely on paper.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation: config loader + validation, models, JSON logging, IST time, holiday calendar, DB + migrations | done |
| 3 | Paper broker + risk engine: fills, slippage, charges, lot sizing, SL/target/trail ladder, windows, daily limits | done |
| 2 | Data interfaces + mocks: MarketDataProvider, synthetic market, replay provider, feed-gap monitor, Parquet tick store | done |
| 4 | Analytics: Greeks/IV, levels, VWAP, opening range, volume profile, confluence, failed breaks, OI map, GEX + flip, straddle, opening type, order flow, futures OI, breadth | done |
| 5 | Decision layer: regime, bias, triggers, strike selection, prior probability model (labelled uncalibrated), setup plug-ins, confluence score, reason log | done |
| 6–11 | News/LLM, scheduler, shadow, dashboard, real integrations | pending |

## Quick start (replay mode, no credentials needed)

```bash
pip install -e ".[dev]"
cp .env.example .env
docker compose -f docker/docker-compose.yml up -d   # local Postgres
alembic upgrade head
pytest
python -m engine.main
```

The engine refuses to start if any file in `config/` is invalid.

## Layout

```
engine/
  config/        settings (env) + YAML schemas + loader
  core/          IST time utils, trading calendar, JSON logs, domain models
  data/          MarketDataProvider interface, mock replay + synthetic market, feed monitor, Parquet ticks
  analytics/     Greeks/IV, GEX, OI map, straddle, opening type, levels, order flow, breadth
  analytics/setups/  named setups (plug-in registry)
  decision/      decision engine, trade plan, reason log
  db/            SQLAlchemy models + session (migrations in migrations/)
  broker_paper/  charges, fills, lot sizing, exit ladder, swing trail, paper broker
  risk/          locked trading rules (windows, daily limits, 2nd-trade thresholds)
config/          rules.yaml, decision.yaml, charges.yaml, instruments.yaml, shadow_rules.yaml, holidays.yaml
```

## Locked exit ladder (R = SL distance = sl_pct of entry premium)

| Lots | At 1R | At 1.5R | Rest |
|---|---|---|---|
| 1 | SL -> cost +5% | SL -> +20% | trail 1 |
| 2 | book 1, SL -> cost +5% | SL -> +20% | trail 1 |
| 3 | book 1, SL -> cost +5% | book 1, SL -> +20% | trail 1 |
| n >= 4 | book floor(n/2), SL -> cost +5% | book max(1, floor(n/4)) (keeping >= 1), SL -> +20% | trail remainder |

Trail: confirmed pivot low on 3-min option-premium candles (2 bars each side), minus 2% buffer,
active after the 1.5R step; the SL never moves down.

Day rules: a trade that ever reached 1R counts as a target hit -> stop for the day. After an SL, a
second trade (either index, either direction) needs P >= 0.70. A 3:15 exit before 1R is a "time exit".
