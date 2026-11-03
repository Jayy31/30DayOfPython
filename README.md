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
| 6 | News + LLM: RSS/GDELT/Finnhub fetchers, dedupe, relevance, LLM scoring (JSON schema, retry, rule fallback), spike detection, market confirmation, news bias, token/cost ledger + budget, mock LLM | done |
| 7 | Live engine + market state, scheduler (all daily jobs, idempotent, holiday-aware), reports (LLM narrative + template fallback), Telegram alerts, Postgres store, feed watchdog, `engine.main replay` | done |
| 8 | Shadow-rule simulator (exact re-simulation on recorded quote paths, config-driven rules) + metrics (expectancy, PF, drawdown, streaks, capital curve, breakdowns, LLM cost) in weekly/monthly reports | done |
| 9 | Dashboard: read-only engine API (FastAPI, bearer token) + Next.js app (Today, Trades, Metrics, Shadow rules, Reports) with Basic auth | done |
| 10 | Real integrations: Kotak Neo provider (SDK 3.0.13, SFeed WebSocket, REST), `verify-kotak`, NSE (holidays, FII/DII, participant OI, bhavcopy), global prices + overnight score, news polling, object storage, Supabase backups, Docker/Caddy/systemd deploy | done (untested against live accounts) |
| 11 | Validation run: 1–2 weeks in `live_data` mode | pending (needs your accounts) |

## Quick start (replay mode, no credentials needed)

```bash
pip install -e ".[dev]"
cp .env.example .env
docker compose -f docker/docker-compose.yml up -d   # local Postgres
alembic upgrade head
pytest
python -m engine.main                      # validate config
python -m engine.main replay 2026-11-03    # run one synthetic day end to end (premarket -> live -> postmarket)
python -m engine.main run                  # start the scheduler with all daily jobs + the dashboard API
```

## Deploying

See [deploy/README.md](deploy/README.md): accounts, `verify-kotak`, Docker on the Oracle VM, Caddy HTTPS, systemd, health alerts, Vercel.

## Dashboard

```
engine (Oracle VM)                      Vercel
  FastAPI :8000  <-- Bearer token --  Next.js server components  <-- Basic auth --  you
  reads Postgres/Supabase + live state  (token never reaches the browser)
```

The dashboard talks only to the engine API, so the Supabase service key stays on the VM.

```bash
# engine: serve 3 synthetic days on the API
ENGINE_API_TOKEN=dev-token STORE_BACKEND=postgres python -m engine.main replay 2026-11-02 --days 3 --serve
# dashboard
cd dashboard && npm ci
ENGINE_API_URL=http://127.0.0.1:8000 ENGINE_API_TOKEN=dev-token DASHBOARD_PASSWORD=dev npm run dev
```

Vercel: set the project root to `dashboard/`, and set `ENGINE_API_URL` (the VM's HTTPS URL),
`ENGINE_API_TOKEN`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`. On the engine set the same
`ENGINE_API_TOKEN` and `DASHBOARD_ORIGINS`. The API is GET-only and cannot change trading state.

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
  llm/           provider wrapper (Anthropic + mock), usage/cost ledger, budget guard
  news/          fetchers, dedupe, relevance, scoring, spikes, confirmation, pipeline
  state/         per-index live market state -> MarketContext
  runtime/       live engine (ticks -> decision -> paper broker), feed watchdog
  scheduler/     daily jobs (IST, holiday-aware, idempotent)
  reports/       premarket / post-market / weekly / monthly
  alerts/        Telegram + in-memory notifier
  store/         persistence (memory + Postgres)
  shadow/        shadow-rule plug-ins, re-simulation, simulator
  metrics/       metrics, breakdowns, report data
  api/           read-only dashboard API (FastAPI)
  db/            SQLAlchemy models + session (migrations in migrations/)
  broker_paper/  charges, fills, lot sizing, exit ladder, swing trail, paper broker
  risk/          locked trading rules (windows, daily limits, 2nd-trade thresholds)
dashboard/       Next.js dashboard (Vercel)
docker/, deploy/  production image, compose, Caddy, systemd units, health check
config/          rules.yaml, feeds.yaml, decision.yaml, llm.yaml, news_sources.yaml, charges.yaml, instruments.yaml, shadow_rules.yaml, holidays.yaml
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
