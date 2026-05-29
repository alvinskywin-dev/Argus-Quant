# Deployment Guide — AI Futures Signal System

End-to-end setup on a fresh Ubuntu 24.04 VPS, from zero to running 24/7.

---

## 1. VPS prerequisites

**Recommended minimum:** 2 vCPU, 4 GB RAM, 40 GB SSD, Ubuntu 24.04 LTS.
A $6–10/mo VPS (Hetzner CX22, DigitalOcean Basic, Vultr, Contabo) is plenty
for the default scan cadence over the full USDT-M universe.

### 1.1 First login, basic hardening

```bash
ssh root@YOUR_VPS_IP

# Update everything
apt-get update && apt-get -y upgrade

# Create non-root user (replace `botuser`)
adduser botuser
usermod -aG sudo botuser

# Optional: enable ufw firewall
apt-get -y install ufw
ufw allow OpenSSH
ufw allow 8000/tcp        # dashboard
ufw --force enable

# (Recommended) disable root SSH after copying your key to botuser
```

From here on, work as `botuser`:

```bash
su - botuser
```

### 1.2 Set the system timezone (keep UTC for trading)

```bash
sudo timedatectl set-timezone UTC
```

---

## 2. Install Docker + Docker Compose

```bash
# Remove any old versions
sudo apt-get remove -y docker docker-engine docker.io containerd runc || true

# Install prerequisites
sudo apt-get install -y ca-certificates curl gnupg lsb-release

# Add Docker's official GPG key + repo
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# Run docker without sudo
sudo usermod -aG docker $USER
newgrp docker

# Sanity check
docker --version
docker compose version
```

---

## 3. Create the Telegram bot

1. Open Telegram, start a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, choose a name (e.g. *AI Futures Signals*) and a username
   ending in `bot` (e.g. `myfutures_signals_bot`).
3. BotFather returns a **token** like `1234567890:AAH...`. Save it.
4. Create the channel/group where signals will be auto-posted:
   - Create a new Telegram **channel** (public or private).
   - Add your bot as an **administrator** with "Post messages" permission.
5. Get the channel chat id:
   - Forward any message from the channel to **[@userinfobot](https://t.me/userinfobot)**.
   - It replies with an id like `-1001234567890`. Save this as
     `TELEGRAM_SIGNAL_CHAT_ID`.
6. Get **your own** Telegram user id from `@userinfobot` and put it in
   `TELEGRAM_ADMIN_IDS` so `/pause` and `/resume` work.

---

## 4. Create the Binance API key (read-only)

The bot **does not place trades**, so a read-only key is all you need.

1. Sign in to Binance → **API Management** → *Create API*.
2. Name it (e.g. `futures-signals-readonly`).
3. **Restrictions:**
   - ✅ Enable Reading
   - ❌ Enable Spot & Margin Trading
   - ❌ Enable Futures
   - ❌ Enable Withdrawals
4. **Restrict IP** to your VPS IP for safety.
5. Save the key + secret.

> Many public endpoints used by the scanner don't require auth at all, but
> setting the keys lets you raise your weight-per-minute budget.

---

## 5. Clone the project and configure

```bash
cd ~
# git clone https://github.com/you/futures-signal-bot.git
# or scp the project tarball:
#   scp futures-signal-bot.zip botuser@VPS:/home/botuser/
#   unzip futures-signal-bot.zip
cd futures-signal-bot

cp .env.example .env
nano .env       # fill in the values below
```

**Required fields** in `.env`:

| Key | Where it comes from |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Binance API Management |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_SIGNAL_CHAT_ID` | channel id (with leading `-100…`) |
| `TELEGRAM_ADMIN_IDS` | your user id(s), comma-separated |
| `POSTGRES_PASSWORD` | choose a strong one |
| `DASHBOARD_SECRET` | random long string |

**Common tuning knobs:**

| Key | Default | Effect |
|---|---|---|
| `SCAN_TIMEFRAMES` | `5m,15m,1h,4h` | which TFs are aggregated |
| `SCAN_INTERVAL_SEC` | `30` | seconds between scan cycles |
| `MIN_CONFIDENCE` | `72` | raise for fewer, higher-quality signals |
| `MAX_SIGNALS_PER_HOUR` | `12` | spam control |
| `SIGNAL_COOLDOWN_SEC` | `3600` | per-symbol/side dedup |
| `MIN_QUOTE_VOLUME_USDT` | `5000000` | filter illiquid pairs |
| `MIN_RR` | `1.8` | reject low risk/reward setups |

---

## 6. Launch

```bash
docker compose up -d --build
docker compose ps
```

Tail the logs:

```bash
docker compose logs -f bot
```

Expected output within ~30 s:

```
database initialized
universe refreshed: 312 symbols tradable
telegram bot started
=== all services running ===
scan cycle complete — analyzed=312 emitted=0
```

Hit the dashboard:

```
http://YOUR_VPS_IP:8000/
```

In Telegram, message your bot privately:

```
/start
/status
/market
```

The bot will start posting signals to your channel as soon as setups
qualify (confidence ≥ MIN_CONFIDENCE, RR ≥ MIN_RR, cooldown clear).

---

## 7. Day-to-day operations

```bash
# Tail logs
docker compose logs -f bot

# Restart only the bot (keeps DB + Redis up)
docker compose restart bot

# Stop everything
docker compose down

# Update the code, rebuild, restart
git pull          # or scp new files
docker compose up -d --build

# Open a shell inside the bot container
docker compose exec bot bash

# Open a psql shell
docker compose exec postgres psql -U signals -d signals
```

Logs auto-rotate at 20 MB and are kept 14 days
(`logs/app.log`, `logs/errors.log`).

---

## 8. Backups

A two-line cron job is enough.

```bash
mkdir -p ~/futures-signal-bot/backups
crontab -e
```

Add (daily 03:30 UTC):

```cron
30 3 * * * cd /home/botuser/futures-signal-bot && \
  docker compose exec -T postgres pg_dump -U signals signals | gzip > \
  backups/signals_$(date +\%Y\%m\%d).sql.gz && \
  find backups -type f -name 'signals_*.sql.gz' -mtime +14 -delete
```

Restore:

```bash
gunzip -c backups/signals_YYYYMMDD.sql.gz | \
  docker compose exec -T postgres psql -U signals -d signals
```

For off-site backups, sync `backups/` to S3 / B2 / Wasabi with `rclone`.

---

## 9. Updates

```bash
cd ~/futures-signal-bot
git pull                    # or replace files manually
docker compose pull         # pull latest postgres/redis if you bumped versions
docker compose up -d --build
docker compose logs -f bot
```

Database schema is auto-created on first boot (`Base.metadata.create_all`).
For breaking schema changes later, switch to Alembic migrations — the
project already pins `alembic` in `requirements.txt`.

---

## 10. Scaling

The default config scans the **entire** USDT-M futures universe (300+
symbols) every 30 s on 4 timeframes. That works on 2 vCPU / 4 GB. To go
further:

| Goal | What to do |
|---|---|
| Faster cycles | Lower `SCAN_INTERVAL_SEC`, raise `MAX_SYMBOLS` only if needed |
| Less load | Raise `MIN_QUOTE_VOLUME_USDT`, drop `1m`/`5m` from `SCAN_TIMEFRAMES` |
| More throughput | Bump `concurrency=12` in `MarketScanner(...)` to 24 |
| Multiple regions | Run a second instance against `TELEGRAM_SIGNAL_CHAT_ID=<other_chat>` with its own `.env` |
| Move DB off-box | Point `POSTGRES_HOST` at managed Postgres, remove the `postgres` service |
| Public dashboard | Put nginx + Let's Encrypt in front of port 8000 |

### nginx + HTTPS for the dashboard (optional)

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo nano /etc/nginx/sites-available/signals
```

```nginx
server {
    listen 80;
    server_name signals.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/signals /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d signals.yourdomain.com
```

Then close port 8000 in the firewall:

```bash
sudo ufw delete allow 8000/tcp
```

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `telegram bot disabled` in logs | `TELEGRAM_BOT_TOKEN` empty in `.env` |
| Signals not appearing in channel | bot isn't an admin of the channel, or `TELEGRAM_SIGNAL_CHAT_ID` wrong |
| `binance 429` warnings | normal; the client backs off — only worry if continuous |
| `universe refreshed: 0 symbols` | check VPS can reach `fapi.binance.com` |
| `redis unreachable` | starts up first time only; benign — cache is optional |
| Dashboard 502 behind nginx | check `docker compose ps`, then `docker compose logs bot` |
| High CPU | drop `1m` from `SCAN_TIMEFRAMES`, raise `MIN_QUOTE_VOLUME_USDT` |

---

## 12. Security checklist

- ✅ `.env` is in `.gitignore` — never commit it
- ✅ Binance key is read-only and IP-restricted to the VPS
- ✅ Bot runs as non-root inside the container
- ✅ Admin Telegram commands check user id whitelist
- ✅ Postgres only listens on the docker network
- ✅ Use ufw + SSH keys + disable root login
- ✅ Put nginx + TLS in front of the dashboard if exposing it publicly

---

You're done. The system will keep running across reboots
(`restart: unless-stopped`) and auto-recover from crashes.
