# wigle-sync
application to sync and upload wigle CSV files

Pulls finished `.wiglecsv` logs off the wardriving Raspberry Pi over SFTP,
uploads each to [wigle.net](https://wigle.net), then moves it into an
`uploaded/` directory on the Pi so it's never sent twice. Kismet's `.kismet`
databases are moved into `uploaded/` too, without being downloaded or sent.

Each run is a one-shot sync, designed to run as a Kubernetes `CronJob`:

- **Pi unreachable** (out driving, powered off): logs it and exits 0.
- **File modified within `MIN_FILE_AGE_SECONDS`**: assumed still open by Kismet, left for a later run.
- **Upload fails**: the file stays in place on the Pi, is retried next run, and the job exits 1.
- **Only `.wiglecsv` is uploaded** (`UPLOAD_EXTENSIONS`). Until 2026-10-10 the `.kismet` databases were uploaded too, tar.gz'd, and WiGLE recorded zero networks from every one (57 in a row, some over 100 MB); the same session's `.wiglecsv` already carries every network. The `.kismet` files (with any `-journal`) are archived on the Pi as a local record and counted as `files_archived_kept`.
- **Empty sessions**: header-only `.wiglecsv` and 0-byte files are archived without uploading.
- **Compression**: files are tar.gz'd before upload (WiGLE's limit is 180 MiB).

## Architecture

**Sync data flow.** The hourly CronJob pulls finished Kismet sessions off the
Pi and pushes them to WiGLE. Numbers follow one run.

```text
  brick69 (Raspberry Pi)                       k3s cluster, namespace "wigle"
 +--------------------------------+          +-----------------------------------+
 | Kismet                         |          | CronJob wigle-sync     0 * * * *  |
 |   | writes                     |  [1] TCP |                                   |
 |   v                            |<---------| 1. probe :22 (away? exit in ~3s)  |
 | /var/log/kismet/               |   probe  | 2. list sessions quiet > 5 min    |
 |   Kismet-<ts>.kismet           |          |    (aged by the Pi's own clock)   |
 |   Kismet-<ts>.kismet-journal   | [2] SFTP | 3. download, roll back journal,   |
 |   Kismet-<ts>.wiglecsv         |<-------->|    tar.gz                         |
 |   uploaded/  <-- [4] archive   |          | 4. upload, then archive on Pi     |
 |                                |          |                                   |
 | sshd: wigle-sync (SFTP only)   |          +-----------------+-----------------+
 +--------------------------------+                            |
                                                               | [3] POST /file/upload
                                                               v
                                             +-----------------------------------+
                                             | wigle.net API                     |
                                             |   upload queue -> processed       |
                                             +-----------------------------------+
```

- **[1]** The Pi is usually out driving, so a bare TCP probe runs first and an
  unreachable Pi is a clean exit, not a failure.
- **[2]** A session is only ready once *all* its files have been quiet for 5
  minutes by the Pi's clock. The Pi has no RTC, and a parked session's
  `.wiglecsv` can sit idle at its header while the `.kismet` is still live.
- **[3]** A failed upload leaves the file in place for the next run. If WiGLE
  can't be reached at all (the home internet is down), the run stops trying,
  counts the remaining files as *deferred* rather than failed, and exits
  cleanly: the internet dropping is not a wigle-sync fault.
- **[4]** Archiving is what stops a session from being uploaded twice.

**Observability and the console.**

```text
 CronJob wigle-sync
   |-- stdout JSON logs --> promtail -------> Loki --------+
   |-- OTLP spans --------> otel-collector -> Tempo -------+--> Grafana "WiGLE Sync"
   '-- push run metrics --> Pushgateway ----> Prometheus --+    (logs <-> traces via trace_id)
                                              ^
                      kube-state-metrics -----'  (CronJob next/last run, failed jobs)

 browser --> Traefik Ingress (wigle.brick...) --> wigle-console (Flask)
                                                 |-- PromQL --> Prometheus  run metrics, schedule
                                                 |-- LogQL ---> Loki        96h runs, faults
                                                 |-- HTTPS ---> wigle.net   rank + upload queue (cached)
                                                 '-- TCP :22 -> brick69     is the Pi home right now?

 SealedSecret --> Secret wigle-sync-secrets --+--> CronJob        WiGLE creds, SSH key, Pi host key
                                              '--> wigle-console  WiGLE creds
```

## Setup

```bash
cp .env.template .env   # then fill in
```

### Pi side (one time)

The Pi is `brick69` at 192.168.1.208 (static DHCP lease); Kismet logs to
`/var/log/kismet`. It has three radios, named by udev `.link` files that match
the USB chip rather than a MAC, so swapping a dongle keeps its name:

| Name | Radio | Job |
| --- | --- | --- |
| `wlan_host` | onboard (`brcmfmac`) | Joins home wifi; wigle-sync reaches the Pi through it |
| `wlan_mon0` | RTL8821CU (`0bda:c811`) | Kismet's capture source |
| `wlan_ap` | RTL8188EUS (`0bda:8179`) | hostapd AP, so a phone can open Kismet's UI at `http://192.168.50.1:2501` |

GPS is a u-blox 7 on USB, read through gpsd. Once home wifi has been up for 15
minutes, a NetworkManager dispatcher script stops Kismet, and deletes the session
if the Pi never left home (a boot in the driveway shouldn't upload the home
network).

`pi/bootstrap.sh` builds all of this on a fresh Raspberry Pi OS (or Kali) install
and is safe to re-run. Kismet comes from Kismet's own apt repo on Raspberry Pi OS.
The SSIDs and the AP passphrase live only on the Pi, in `/etc/default/kismet-pi`
(start from `pi/kismet-pi.env.example`):

```bash
scp -r pi zaphod@192.168.1.208:/tmp/wigle-pi
ssh zaphod@192.168.1.208 'sudo install -m 600 /tmp/wigle-pi/kismet-pi.env.example /etc/default/kismet-pi'
ssh -t zaphod@192.168.1.208 'sudoedit /etc/default/kismet-pi'
ssh zaphod@192.168.1.208 'sudo bash /tmp/wigle-pi/bootstrap.sh'
```

It ends with checks (radio names, monitor and AP mode, hostapd, gpsd) and exits 1
if any fail. The in-kernel `rtl8xxxu` driver can't put the RTL8188EUS in AP mode,
so `pi/setup-rtl8188eus.sh` (run by bootstrap) builds
[aircrack-ng/rtl8188eus](https://github.com/aircrack-ng/rtl8188eus) at a pinned
commit through DKMS, patched for current kernels by
`pi/rtl8188eus-kernel-compat.py`, and blacklists `rtl8xxxu`. The build takes a few
minutes on a Pi 3B+. If a kernel upgrade breaks the DKMS rebuild, the compile
errors are in `/var/lib/dkms/realtek-rtl8188eus/*/build/make.log`; add the fix to
the compat script.

When replacing the Pi, copy `/etc/ssh/ssh_host_*` from the old one to keep the
host key pinned in `known_hosts` below, and `~/.kismet/kismet_httpd.conf` to keep
the Kismet UI login.

The app connects as a dedicated `wigle-sync` user that can only use SFTP, and
whose only access is an ACL on that log dir. `pi/setup-pi-user.sh` sets this up
(bootstrap runs it when `WIGLE_SYNC_PUBKEY` is set) and is safe to re-run:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/wigle_sync_ed25519 -N "" -C wigle-sync
ssh zaphod@192.168.1.208 "sudo bash -s -- '$(cat ~/.ssh/wigle_sync_ed25519.pub)'" < pi/setup-pi-user.sh
ssh-keyscan -t ed25519 192.168.1.208 > known_hosts   # pins the Pi's host key
```

WiGLE rejects uploads over 180 MiB, and Kismet can't rotate `.kismet` logs by
size (only pcapng, via `pcapng_log_max_mb`). `kismet-size-watch.timer` checks the
log Kismet has open every 5 minutes and restarts Kismet once it passes 150 MiB, which
starts a new log. Change the cap with `KISMET_MAX_MB` in
`pi/kismet-size-watch.service`. To install, or reinstall after a change:

```bash
scp pi/kismet-size-watch.* pi/setup-kismet-size-watch.sh zaphod@192.168.1.208:/tmp/
ssh zaphod@192.168.1.208 'sudo bash /tmp/setup-kismet-size-watch.sh'
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

## Console (`https://wigle.brick.nozdormu.cloud`)

A read-only status page in the same style as chucks-wisdom's console
(`console.py`, `stats.py`, `templates/console.html`, image built from
`Dockerfile.web`). Four tiles across the top: next sync, the last sync with the Pi
(OK, failed, or waiting for the internet) and what it uploaded (files and MB), the
all-time total (files and MB). Below them, a 96h strip of hourly runs on the left and your WiGLE rank
plus the latest uploads' processing state on the right (one column on a
phone). Faults from the sync job appear only when there are any, each distinct
one once with a count. `/api/status` serves the same data as JSON.

The all-time totals (`wigle_sync_files_uploaded_since_launch`,
`wigle_sync_bytes_uploaded_since_launch`) live in the Pushgateway: each run that
uploads reads them back and pushes them increased. A run never restarts a total
that's missing, since an empty gateway may just have lost its data; it logs a
warning instead. To set them (first time, or after the gateway lost them), recount
from WiGLE's upload history, which includes any uploads made outside wigle-sync:

```bash
poetry run python sync.py --seed-totals --dry-run   # print the counts
PUSHGATEWAY_URL=http://localhost:9091 poetry run python sync.py --seed-totals
```

It reads from Prometheus (Pushgateway + kube-state-metrics), Loki and the WiGLE
API (cached 10 min). Each source is independent, so one being down only blanks
its own panel. Answers come from a stale-while-revalidate cache: past its
15s TTL the last status is served at once while a background thread rebuilds
it, because a cold build takes ~10s (mostly Loki's 96h queries). The console
builds the first status at start-up, so only a request in its first seconds
waits.

```bash
kubectl apply -f k8s_config/console/
```

`wigle.brick.nozdormu.cloud` is served through brick9000's proxy, over HTTPS
(see `brick-k8s-config`'s README, "LAN names for ingresses"), so it opens from
any device on the home WiFi, phones included, with no setup.

Locally: port-forward Prometheus (9090) and Loki (3100), then
`docker compose up wigle-console` and open http://localhost:5000.

## Radar (`https://radar.brick.nozdormu.cloud`)

The console's second page (`radar.py`, `templates/radar.html`; also at
`/radar` on the console's own name): a full-screen radar of where recent drives
found networks. The data comes from WiGLE, not the Pi: each finished upload
has a KML of the networks it located (`/file/kml/<transid>`; it answers 406 to
`Accept: application/json`). An upload never changes once processed, so each
KML is fetched once and kept.

- **Drives**: the newest `RADAR_UPLOADS` (12) uploads, each in its own colour
  (newest brightest), with a legend of their dates, how many networks each
  found, and how many of those were new to you.
- **New to you**: a network not seen on any earlier drive the radar holds is
  drawn larger, with a bright core when the sweep passes. WiGLE's KMLs don't
  say what was new to WiGLE itself, so this is about your own drives.
- **Ghost streets**: up to `GHOST_UPLOADS` (60) older uploads, kept only as
  positions rounded to about 11 m and drawn very faintly under everything.
  Networks line the roads they were found on, so the old drives trace the
  street grid without a map. They're fetched at most
  `RADAR_FETCHES_PER_REFRESH` (5) per 30-minute refresh, to go easy on WiGLE.

Network names never leave `radar.parse_kml`. Network IDs are only used, as an
in-memory hash, to tell a first sighting from a repeat; they never reach the
page. The scope holds 90% of the recent contacts (snapped to 500 m, 1, 2, 3,
5, 10, 20, 50 or 100 km), and a sweep re-lights what it passes. The centre is
the median of the recent contacts (drives start and end at home), or
`RADAR_CENTER` (`lat,lon`), an optional key in the `wigle-sync-secrets`
Secret, so home's coordinates never sit in git.

The console's readiness probe is `/ready`, which answers 503 until the
radar's first WiGLE load has finished (a minute or so), so a new pod takes no
traffic with an empty radar and a rollout keeps an old pod serving until then.
A failed load counts as finished, so a WiGLE outage doesn't take the console
down. Liveness stays on `/health`.
