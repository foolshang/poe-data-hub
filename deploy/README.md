# Deploying poe-data-hub (GCE VM, alongside the existing Discord bot)

1. Copy the repo to `/opt/poe-data-hub` on the VM and create a dedicated
   user:
   ```bash
   sudo useradd --system --home /opt/poe-data-hub --shell /usr/sbin/nologin poe-data-hub
   sudo mkdir -p /opt/poe-data-hub
   # rsync/git clone the repo into /opt/poe-data-hub
   sudo chown -R poe-data-hub:poe-data-hub /opt/poe-data-hub
   ```

2. Set up the venv as the `poe-data-hub` user:
   ```bash
   cd /opt/poe-data-hub
   sudo -u poe-data-hub python3 -m venv .venv
   sudo -u poe-data-hub .venv/bin/pip install -r requirements.txt
   ```

3. Place the Firebase service-account key at
   `/opt/poe-data-hub/data/service-account.json` (owned by `poe-data-hub`,
   mode 600). This path matches `Environment=GOOGLE_APPLICATION_CREDENTIALS=`
   in `poe-data-hub.service` -- change both together if you move it.

4. Install and start the service:
   ```bash
   sudo cp deploy/poe-data-hub.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now poe-data-hub
   ```

5. Check it came up and survives a restart:
   ```bash
   sudo systemctl status poe-data-hub
   journalctl -u poe-data-hub -f
   sudo reboot   # then re-check status after the VM comes back
   ```

## What the service runs

One long-lived process (`python -m src.main --serve`) with three independent
APScheduler jobs (see `run_scheduler` in `src/main.py`):

- **prices** -- every `schedule.prices_interval_minutes` (default 20min)
- **meta** -- `schedule.meta_cron_hour_thailand` (default 02:00 ICT); bump
  `schedule.meta_runs_per_day_league_start` above 1 in `config/config.yaml`
  and restart the service during the first few days of a new league for
  more frequent runs
- **cleanup** -- daily at `schedule.cleanup_hour_thailand` (default 04:00 ICT)

Logs go to journald (`journalctl -u poe-data-hub`) and to
`data/hub.log` (rotated daily, kept for `retention.log_rotate_days` days).
A crashing job is caught, logged, and does not take down the other two
jobs or the process (`Restart=on-failure` in the unit only kicks in if the
whole process dies, e.g. an unhandled crash outside job execution).
