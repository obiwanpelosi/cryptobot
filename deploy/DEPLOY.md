# Deploying cryptobot to a VPS

This takes about 30–45 minutes the first time. Commands marked **Mac** run in your Mac's terminal
(inside the `cryptobot` folder). Commands marked **Server** run on the VPS over SSH.

> **Only one copy of the bot can run.** Before the server copy starts, stop the bot on your Mac
> (`Ctrl+C`, or `docker compose down` if you ran it in Docker). Otherwise Telegram reports
> "Conflict: terminated by other getUpdates request".

---

## 1. Rent the server

Recommended: **Hetzner Cloud CX22** (2 vCPU, 4 GB RAM, 40 GB SSD, about €4.50/month).

- **Image:** Ubuntu 24.04
- **Location:** Falkenstein, Nuremberg or Helsinki (EU). **Not a US region**, because Binance.com blocks US IP addresses.
- **SSH key:** add your Mac's public key. If you don't have one yet, create it on your Mac:
  ```bash
  ssh-keygen -t ed25519          # Mac: press Enter through the questions
  cat ~/.ssh/id_ed25519.pub      # Mac: paste this into Hetzner's "SSH keys" box
  ```

DigitalOcean or Vultr (Frankfurt or Amsterdam, about $6/month) work the same way.

Note the server's **IP address** when it's created. Below it's written as `SERVER_IP`.

## 2. Check the server can reach everything (before installing anything)

```bash
ssh root@SERVER_IP                                                            # Mac
curl -fsSL https://raw.githubusercontent.com/obiwanpelosi/cryptobot/main/deploy/check-network.sh | bash   # Server
```

All five lines should say `OK`. If **Binance spot** or **Binance futures** fails, that region is
blocked. Delete the server and create one in a different location; it's billed by the hour.

## 3. Basic server setup

Everything in this step runs on the server, as `root`:

```bash
# A normal user for the bot (you'll log in as this from now on)
adduser --disabled-password --gecos "" bot
usermod -aG sudo bot
mkdir -p /home/bot/.ssh && cp ~/.ssh/authorized_keys /home/bot/.ssh/
chown -R bot:bot /home/bot/.ssh && chmod 700 /home/bot/.ssh
echo "bot ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/bot

# Firewall: only SSH in (the bot makes outgoing connections only)
ufw allow OpenSSH && ufw --force enable

# Automatic security updates
apt-get update && apt-get install -y unattended-upgrades git rsync sqlite3
dpkg-reconfigure -f noninteractive unattended-upgrades

# Docker, from Docker's official repository
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
usermod -aG docker bot

# The app folder
mkdir -p /opt/cryptobot && chown bot:bot /opt/cryptobot
exit
```

## 4. Get the code

```bash
ssh bot@SERVER_IP                                                             # Mac
git clone https://github.com/obiwanpelosi/cryptobot.git /opt/cryptobot        # Server
cd /opt/cryptobot && id -u                                                    # Server: note this number
```

If `id -u` printed something other than `1000`, add `export BOT_UID=<that number>` to
`~/.bashrc` on the server and log in again. The container's user has to match, so it can write to
`data/` and `logs/`.

## 5. Copy your secrets (and, optionally, your history)

From your Mac, **not** through GitHub (`.env` is git-ignored on purpose):

```bash
scp .env bot@SERVER_IP:/opt/cryptobot/.env                     # Mac
ssh bot@SERVER_IP "chmod 600 /opt/cryptobot/.env"              # Mac
```

`config.yaml` comes from the repo. If you changed it locally without committing, copy it too:
`scp config.yaml bot@SERVER_IP:/opt/cryptobot/config.yaml`.

**Optional: keep your paper-trading history** (signals, positions, AI calls). **Stop the bot on
your Mac first**, then:

```bash
ssh bot@SERVER_IP "mkdir -p /opt/cryptobot/data"               # Mac
scp data/bot.db bot@SERVER_IP:/opt/cryptobot/data/bot.db       # Mac
```

Skip this to start clean. Either way works; your old test signals are excluded from `/stats` anyway.

## 6. Start the bot

```bash
cd /opt/cryptobot
docker compose up -d --build       # Server: first build takes 1–2 minutes
docker compose logs -f             # Server: watch it start; Ctrl+C stops watching, not the bot
```

You should see candles loaded, `Price stream connected`, then `Telegram bot @sol_lnk_alert_bot polling`,
and the **🟢 cryptobot started** message arrives on your phone. Send `/start`, `/price` and `/ai`.

After about 2 minutes, `docker compose ps` shows `(healthy)`.

## 7. Lock your Binance API key to the server

In Binance, go to **Account → API Management → your key → Edit restrictions → Restrict access to
trusted IPs only**, and add `SERVER_IP`. Keep "Enable Reading" on, and trading and withdrawals **off**.
Then restart the bot, and the "not IP-restricted" warning is gone:

```bash
docker compose restart             # Server
```

> After this, the key **only works from the server**. Running the bot on your Mac with the same
> key will fail, which is fine: the server is the one copy now.

## 8. Nightly backups

```bash
crontab /opt/cryptobot/deploy/backup-cron        # Server: installs the 03:30 UTC backup job
crontab -l                                       # Server: check it's there
docker compose exec -T bot python -m bot.storage.backup   # Server: run one now to check it works
```

Backups go to `/opt/cryptobot/data/backups/`, and 14 days are kept. To copy them to your Mac
whenever you like:

```bash
deploy/pull-backups.sh bot@SERVER_IP             # Mac: copies into ./backups/
```

## 9. Check that it survives problems

```bash
sudo reboot                                      # Server: the bot comes back by itself (🟢 message)
```

Docker also restarts it if it ever crashes (`restart: unless-stopped`).

---

## Everyday use

| Task | Command (on the server, in `/opt/cryptobot`) |
|---|---|
| Is it running and healthy? | `docker compose ps` |
| Live logs | `docker compose logs -f` (or `tail -f logs/bot.log`) |
| Change a setting | `nano config.yaml`, then `docker compose restart` |
| Ship a new version (after `git push` from your Mac) | `deploy/update.sh` |
| Stop the bot | `docker compose stop` (start again: `docker compose start`) |
| Run an AI model comparison | `docker compose exec bot python -m bot.ai.compare --live SOL` |
| Full stats report | `docker compose exec bot python -m bot.evaluation.report` |
| Back up now | `docker compose exec -T bot python -m bot.storage.backup` |

Commands with `docker compose exec` run inside the container, using the same code and data as the bot.

## Troubleshooting

- **"Conflict: another instance of this bot is already running"**: the bot is still running on
  your Mac (or twice on the server). Stop the extra copy.
- **`(unhealthy)` in `docker compose ps`**: run `docker compose exec bot python -m bot.health` to
  see why. "No price data" usually means a network or Binance issue, and the bot keeps reconnecting
  by itself; you'll also have had a Telegram alert.
- **Binance errors after locking the key:** check the IP you added matches `curl -4 ifconfig.me`
  on the server.
- **Permission denied writing `data/` or `logs/`:** the container user doesn't match the folder
  owner. Set `BOT_UID` (step 4) and run `docker compose up -d --build`.
