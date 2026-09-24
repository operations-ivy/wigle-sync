# wigle-sync
application to sync and upload wigle CSV files

Pulls finished `.kismet` / `.wiglecsv` logs off the wardriving Raspberry Pi over
SFTP, uploads each to [wigle.net](https://wigle.net), then moves it into an
`uploaded/` directory on the Pi so it's never sent twice.

Each run is a one-shot sync, designed to run as a Kubernetes `CronJob`:

- **Pi unreachable** (out driving, powered off): logs it and exits 0.
- **File modified within `MIN_FILE_AGE_SECONDS`**: assumed still open by Kismet, left for a later run.
- **Upload fails**: the file stays in place on the Pi, is retried next run, and the job exits 1.
- **Interrupted sessions**: when Kismet was killed mid-write, a `.kismet` has a `-journal` beside it. The app downloads both and lets SQLite roll the journal back before uploading, then archives both.
- **Empty sessions**: header-only `.wiglecsv` and 0-byte files are archived without uploading.
- **Compression**: files are tar.gz'd before upload (WiGLE's limit is 180 MiB, and Kismet's sqlite logs compress well).

## Setup

```bash
cp .env.template .env   # then fill in
```

### Pi side (one time)

The Pi is `brick69` at 192.168.1.208 (static DHCP lease); Kismet logs to
`/var/log/kismet`. The app connects as a dedicated `wigle-sync` user that can
only use SFTP, and whose only access is an ACL on that log dir.
`pi/setup-pi-user.sh` sets this up and is safe to re-run:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/wigle_sync_ed25519 -N "" -C wigle-sync
ssh zaphod@192.168.1.208 "sudo bash -s -- '$(cat ~/.ssh/wigle_sync_ed25519.pub)'" < pi/setup-pi-user.sh
ssh-keyscan -t ed25519 192.168.1.208 > known_hosts   # pins the Pi's host key
```

## Running

```bash
poetry install
poetry run pre-commit install

poetry run python sync.py --check-auth   # verify WiGLE credentials
poetry run python sync.py --dry-run      # list what would be uploaded
poetry run python sync.py                # sync

poetry run pytest
```

With Docker (mounts `~/.ssh/wigle_sync_ed25519`, override with `PI_SSH_KEY_HOST_PATH`):

```bash
docker compose build
docker compose run --rm wigle-sync --dry-run
docker compose run --rm wigle-sync
```

## Configuration

See `.env.template` for everything. In short: `WIGLE_API_NAME`/`WIGLE_API_TOKEN`
come from wigle.net → Account → API Token, and `PI_*` describes how to reach the
Pi and where its logs live.

## Console (`http://wigle.local`)

A read-only status page in the same style as chucks-wisdom's console
(`console.py`, `stats.py`, `templates/console.html`, image built from
`Dockerfile.web`). It shows next/last sync, what the last run uploaded, the
Pi's live status, a 96h run log, faults, and your WiGLE rank plus each
upload's processing state. `/api/status` serves the same data as JSON.

It reads from Prometheus (Pushgateway + kube-state-metrics), Loki and the WiGLE
API (cached 10 min). Each source is independent, so one being down only blanks
its own panel.

```bash
kubectl apply -f k8s_config/console/
echo "<main-node-ip> wigle.local" | sudo tee -a /etc/hosts
```

Locally: port-forward Prometheus (9090) and Loki (3100), then
`docker compose up wigle-console` and open http://localhost:5000.
