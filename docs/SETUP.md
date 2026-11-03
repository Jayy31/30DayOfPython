# papertrade setup guide

Everything needed to take papertrade from this repo to a running paper-trading system.
Do the parts in order: accounts and keys first, then the server.

> Menu names in Kotak, Supabase, Oracle and Cloudflare change from time to time. If a label
> differs slightly from what is written here, look for the closest equivalent.
>
> Nothing in this system places orders. The Kotak client only exposes login and market data.

**Contents**

1. [Kotak Neo (market data only)](#part-1-kotak-neo-market-data-only)
2. [Supabase (database)](#part-2-supabase-database)
3. [Object storage (ticks and backups)](#part-3-object-storage-ticks-and-backups)
4. [Telegram (alerts)](#part-4-telegram-alerts)
5. [Anthropic (LLM)](#part-5-anthropic-llm)
6. [Finnhub (optional)](#part-6-finnhub-optional)
7. [A domain name for the API](#part-7-a-domain-name-for-the-api)
8. [Oracle Cloud server](#part-8-oracle-cloud-server-runs-the-engine)
9. [Configure the engine on the server](#part-9-configure-the-engine-on-the-server)
10. [Check the Kotak account](#part-10-check-the-kotak-account-before-going-live)
11. [Start it permanently](#part-11-start-it-permanently)
12. [Dashboard on Vercel](#part-12-dashboard-on-vercel)
13. [Validation run](#part-13-what-a-normal-day-looks-like-validation-run-12-weeks)

---

## Part 1: Kotak Neo (market data only)

1. **Open a Kotak Neo account** if you don't have one, and complete KYC.
2. **Activate the F&O segment** in the Neo app. You won't place trades, but derivatives data most
   likely needs the segment active.
3. **Create the Trade API app.** In the Neo app go to More → Trade API → generate an application,
   and copy the token. This is `KOTAK_CONSUMER_KEY`.
4. **Register for TOTP** via the Trade API registration page:
   https://www.kotaksecurities.com/platform/kotak-neo-trade-api/
   - Scan the QR code with Google Authenticator or Authy.
   - **Also save the text secret shown with the QR code.** This is `KOTAK_TOTP_SECRET`, and the
     engine generates codes from it. If you only scanned the QR, re-register and copy the text
     this time.
5. **Note the rest:**
   - `KOTAK_UCC`: your client code, found under Profile in the app.
   - `KOTAK_MOBILE`: the registered mobile number with country code, e.g. `+919876543210`.
   - `KOTAK_MPIN`: your 6-digit MPIN.

> **Security:** the TOTP secret plus the MPIN give full access to your account. Never paste them
> into chat, email or git; they only go into the server's `.env` file.

## Part 2: Supabase (database)

1. Sign up at https://supabase.com and create a new project.
   - **Region:** Mumbai (`ap-south-1`).
   - **Database password:** set a strong one and save it.
2. Click **Connect**, choose the **Session pooler** connection string (port 5432), and copy it.
   Use the pooler rather than the direct connection, because the direct connection is
   IPv6-only on the free plan.
3. Turn it into the engine's format by adding `+psycopg` after `postgresql` and
   `?sslmode=require` at the end:
   ```
   DATABASE_URL=postgresql+psycopg://postgres.<project-ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   If the password contains `@`, `:` or `/`, URL-encode it (for example `@` becomes `%40`).
4. You don't need to create tables yourself. The engine runs the migrations every time it starts.
5. `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` aren't used by the engine. Leave them blank.
6. **Limits to know:**
   - The free tier is 500 MB. Raw ticks go to object storage, not the database.
   - Free projects pause after about a week without activity. The engine writes every trading
     day, so this shouldn't happen while it's running.

## Part 3: Object storage (ticks and backups)

**Use Cloudflare R2.** The current code signs requests with region `auto`, which suits R2.
Oracle Object Storage would need a small code change to set its region (`ap-mumbai-1`).

1. Sign up at https://dash.cloudflare.com, open **R2**, and enable it. The free tier includes 10 GB.
2. Create a bucket, for example `papertrade`. This is `STORAGE_BUCKET`.
3. Go to R2 → **Manage API tokens** → create a token with **Object Read & Write** on that bucket.
   Copy:
   - the Access Key ID → `STORAGE_ACCESS_KEY`
   - the Secret Access Key → `STORAGE_SECRET_KEY`
   - the endpoint, `https://<account-id>.r2.cloudflarestorage.com` → `STORAGE_ENDPOINT`

## Part 4: Telegram (alerts)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. Copy the bot
   token. This is `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it any message, such as "hi".
3. Open this URL in a browser, using your token:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Find `"chat":{"id":...}` in the response. That number is `TELEGRAM_CHAT_ID`. For a group it's
   negative; keep the minus sign.

## Part 5: Anthropic (LLM)

1. Go to https://console.anthropic.com, add billing, and create an API key. This is `LLM_API_KEY`.
2. Under **Limits**, set a monthly spend limit, for example $40. This is a hard ceiling on top of
   the engine's own budget setting (`LLM_MONTHLY_BUDGET_USD=40`).

## Part 6: Finnhub (optional)

Get a free API key at https://finnhub.io. This is `FINNHUB_API_KEY`. Without it, Finnhub is
skipped and the other news sources still work.

## Part 7: A domain name for the API

The dashboard needs an HTTPS address for the engine API, and Caddy gets the certificate
automatically.

- **Free option:** https://www.duckdns.org gives you a name like `papertrade-yourname.duckdns.org`.
- **If you own a domain:** use a subdomain such as `api.yourdomain.com`.

You'll point it at the server's IP in Part 8.

## Part 8: Oracle Cloud server (runs the engine)

1. **Sign up** at https://www.oracle.com/cloud/free/ and **choose Mumbai as your home region**.
   This can't be changed later, and Always Free resources only exist in the home region.
2. **Create the server** under Compute → Instances → Create instance:
   - **Image:** Ubuntu 24.04.
   - **Shape:** `VM.Standard.A1.Flex` (Ampere ARM), with 2 OCPUs and 12 GB RAM. That's inside
     the free 4 OCPU / 24 GB.
   - **SSH key:** upload your public key, or download the generated one.
   - **Public IP:** make sure one is assigned.
   - If you get **"Out of capacity"**, retry later or try another availability domain. Upgrading
     the account to Pay-As-You-Go usually fixes it, and Always Free resources still cost nothing
     there.
3. **Reserve the public IP** under Networking → Reserved IPs, so it doesn't change. Then point
   your domain from Part 7 at this IP. In DuckDNS, paste the IP into your subdomain.
4. **Open ports 80 and 443 in Oracle's firewall.** Go to Networking → VCN → Security Lists →
   Default → Add Ingress Rules: source `0.0.0.0/0`, TCP, destination ports `80` and `443`.
5. **SSH in:**
   ```bash
   ssh ubuntu@<your-ip>
   ```
6. **Open the same ports in Ubuntu's own firewall.** Oracle's Ubuntu images block them by default.
   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   ```
   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   ```
   ```bash
   sudo netfilter-persistent save
   ```
7. **Set the server's timezone:**
   ```bash
   sudo timedatectl set-timezone Asia/Kolkata
   ```
8. **Install Docker and git:**
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
   ```
   ```bash
   sudo usermod -aG docker $USER
   ```
   Then log out and SSH back in, so the docker group applies.
9. **Get the code.** If the repo is private, create a GitHub fine-grained token with read access
   to it and use it as the password when git asks.
   ```bash
   sudo git clone https://github.com/Jayy31/30DayOfPython.git /opt/papertrade
   ```
   ```bash
   sudo chown -R $USER /opt/papertrade && cd /opt/papertrade
   ```

## Part 9: Configure the engine on the server

1. **Create the secrets file:**
   ```bash
   cp .env.example .env && chmod 600 .env && nano .env
   ```
2. **Fill in every value** from Parts 1–6. Write `KEY=value` with no comment after the value on
   the same line, because Docker's env-file parsing can include it in the value.
   ```
   KOTAK_CONSUMER_KEY=...
   KOTAK_UCC=...
   KOTAK_MOBILE=+919876543210
   KOTAK_MPIN=...
   KOTAK_TOTP_SECRET=...
   KOTAK_ENV=prod
   FINNHUB_API_KEY=...
   LLM_PROVIDER=anthropic
   LLM_API_KEY=...
   LLM_MODEL_FAST=claude-haiku-5-5
   LLM_MODEL_SMART=claude-sonnet-5-5
   LLM_MONTHLY_BUDGET_USD=40
   DATABASE_URL=postgresql+psycopg://...
   STORAGE_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
   STORAGE_BUCKET=papertrade
   STORAGE_ACCESS_KEY=...
   STORAGE_SECRET_KEY=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ENGINE_API_TOKEN=...
   DASHBOARD_ORIGINS=https://<your-app>.vercel.app
   TIMEZONE=Asia/Kolkata
   ```
   `APP_MODE=live_data` and `STORE_BACKEND=postgres` are set by the production compose file, so
   you don't need them here.
3. **Generate the API token.** Put the output in `ENGINE_API_TOKEN` and keep a copy for Vercel.
   ```bash
   openssl rand -hex 32
   ```
4. **Set your domain:**
   ```bash
   cp deploy/compose.env.example deploy/compose.env && nano deploy/compose.env
   ```
   Set `API_DOMAIN=` to your domain from Part 7.
5. **Check the trading config** in `config/`:
   - `charges.yaml`: confirm the STT, exchange, SEBI, stamp duty and GST rates against current
     circulars.
   - `rules.yaml`: your fixed trading rules. Change only if you mean to.
   - Holidays are fetched from NSE automatically, so there's nothing to fill in.

## Part 10: Check the Kotak account (before going live)

1. **Run the read-only check**, ideally on a weekday between 9:15 and 15:30 so the live-feed
   check gets ticks:
   ```bash
   cd /opt/papertrade && export $(cat deploy/compose.env) && docker compose -f docker/compose.prod.yml run --rm engine verify-kotak
   ```
   The first run builds the Docker image, which takes a few minutes.
2. **Read the result.** The report should show `"required_ok": true`. It covers:
   - login
   - the NIFTY and SENSEX option chains (with lot sizes 65 and 20)
   - candles
   - the extra tokens (India VIX, Bank Nifty, heavyweight stocks)
   - live feed ticks
3. **If something is wrong**, the SENSEX names, the India VIX token or a heavyweight token are
   corrected in `config/feeds.yaml` or `config/instruments.yaml`. These are config changes, not
   code. Remove secrets before sharing the report with anyone.

## Part 11: Start it permanently

1. **Install the services:**
   ```bash
   sudo cp deploy/papertrade.service deploy/papertrade-health.service deploy/papertrade-health.timer /etc/systemd/system/
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now papertrade.service papertrade-health.timer
   ```
2. **Watch the logs:**
   ```bash
   docker compose -f docker/compose.prod.yml logs -f engine
   ```
3. **Test the API** from your own computer. It should return `{"ok":true,...}`.
   ```bash
   curl https://<your-domain>/health
   ```
4. **What happens from then on:**
   - Docker restarts the engine if it crashes.
   - systemd starts everything again after a reboot.
   - A health check every minute sends a Telegram alert if the API stops answering, or if no
     ticks arrive for 2 minutes during market hours.

## Part 12: Dashboard on Vercel

1. Go to https://vercel.com → Add New → Project → import `Jayy31/30DayOfPython`.
2. Set **Root Directory** to `dashboard`. The framework should be detected as Next.js.
3. Add these environment variables:
   - `ENGINE_API_URL`: `https://<your-domain>`
   - `ENGINE_API_TOKEN`: the same value as on the server
   - `DASHBOARD_USER`: e.g. `admin`
   - `DASHBOARD_PASSWORD`: a strong password. Without it the dashboard won't serve at all.
4. Click **Deploy**, then open `https://<project>.vercel.app` and log in with that user and
   password.
5. Put that Vercel URL in `DASHBOARD_ORIGINS` in the server's `.env`, then restart:
   ```bash
   sudo systemctl restart papertrade
   ```

## Part 13: What a normal day looks like (validation run, 1–2 weeks)

- **On Telegram:**
  - 8:45: premarket report
  - an alert on every entry, partial exit and exit
  - 16:00: post-market review
  - Saturday: weekly report with metrics and shadow rules
- **On the dashboard:** check the live index cards and signals during market hours.
- **For two weeks, check:**
  - no feed-silence alerts
  - trades only inside 9:30–11:30 and 13:30–14:30
  - everything closed by 15:15
  - fills that look realistic against the bid/ask you see in the Neo app
- **Remember:** judge the strategy only after about 100 trades. Until then probabilities are
  labelled "uncalibrated (priors)".

---

## Updating the code later

```bash
cd /opt/papertrade && git pull && sudo systemctl restart papertrade
```

## Running cost

The Oracle server, Supabase, R2, Vercel and Telegram are all free at this scale. Expect about
$30–35 a month for the LLM, capped by your Anthropic spend limit.

## Caveats

- **Not yet run live:** none of this has been tested against real accounts. Part 10 is where
  wrong assumptions show up.
- **Oracle storage:** to use Oracle Object Storage instead of R2, the storage client first needs
  a region setting added.
