# Deploying papertrade (Oracle Cloud Always Free, Mumbai)

Nothing here places orders: the Kotak client is wrapped so only login and market-data calls
exist in our code (a test fails the build if any order method is referenced).

## 1. Accounts (once)

- [ ] Kotak Neo: NEO app -> More -> Trade API -> generate application, copy the consumer key; register
      TOTP and keep the TOTP **secret** (the text under the QR code). TOTP secret + MPIN = full account
      access: keep them only in the VM's `.env` (chmod 600).
- [ ] Supabase project -> Settings -> Database -> connection string (session pooler, port 5432):
      `DATABASE_URL=postgresql+psycopg://postgres.<ref>:<password>@<host>:5432/postgres?sslmode=require`
- [ ] Object storage bucket (Oracle Object Storage S3-compat or Cloudflare R2) + access keys
- [ ] Telegram bot (@BotFather) + chat id; Anthropic API key with a monthly spend limit set in the console
- [ ] Finnhub API key (optional; news source)
- [ ] A DNS name for the API (e.g. `api.yourdomain`) pointing at the VM's public IP

## 2. VM

Ubuntu 24.04, Ampere A1 (arm64). Open ports 80/443 in the VCN security list and in `iptables`.

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER   # re-login afterwards
sudo git clone <your repo> /opt/papertrade && sudo chown -R $USER /opt/papertrade
cd /opt/papertrade
cp .env.example .env && chmod 600 .env        # fill in every value
cp deploy/compose.env.example deploy/compose.env   # set API_DOMAIN
```

## 3. Verify the Kotak account before anything else

```bash
docker compose -f docker/compose.prod.yml run --rm engine verify-kotak
```

Read-only. It logs in, then checks:

- whether the option chain has OI and IV (the engine computes IV itself)
- whether historical candles carry OI
- the SENSEX `bse_fo` segment and underlying names
- the `INDIA VIX` and heavyweight tokens in `config/feeds.yaml`
- quote latency
- whether the WebSocket delivers ticks

Fix anything it flags in `config/instruments.yaml` / `config/feeds.yaml` (no code changes needed).

## 4. Start

```bash
sudo cp deploy/papertrade.service deploy/papertrade-health.service deploy/papertrade-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now papertrade.service papertrade-health.timer
docker compose -f docker/compose.prod.yml logs -f engine
```

- Docker restarts the engine if it crashes; systemd brings the stack back after a reboot.
- The health timer sends a Telegram alert if the API stops answering, or if no tick arrives for 2 minutes during market hours.
- Migrations run automatically on every start (`alembic upgrade head` is idempotent).

## 5. Dashboard (Vercel)

Import the repo, set the root directory to `dashboard/`, and set these environment variables:

- `ENGINE_API_URL=https://api.yourdomain`
- `ENGINE_API_TOKEN` (the same value as in the VM's `.env`)
- `DASHBOARD_USER`
- `DASHBOARD_PASSWORD`

Then set `DASHBOARD_ORIGINS` in the VM's `.env` to the Vercel URL.

## 6. Validation run (Phase 11)

Run 1-2 weeks in `live_data` mode and watch the Telegram alerts, the premarket and post-market reports, and the dashboard before trusting any numbers.

## Updating

```bash
cd /opt/papertrade && git pull && sudo systemctl restart papertrade
```
