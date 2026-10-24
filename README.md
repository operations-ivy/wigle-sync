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
- **Surveillance and tracking devices** (`detect.py`, #35): before upload, each `.wiglecsv` is checked for Flock ALPR cameras (SSID `Flock-` + serial, medium), Axon devices (Axon's IEEE OUI in a globally administered address, high), Flipper Zeros (Bluetooth name, high) and Bluetooth serial modules as used in card skimmers (low: logged, never alerted). Only counts per kind leave it: a "Surveillance devices on this drive" log line, `wigle_sync_last_sync_detections{kind,confidence}` on the Pushgateway, and wigle-console's `/api/status` `detections`. Trackers (AirTags, SmartTags, Tile) need the advert data only the `.kismet` has, which isn't downloaded; that's a later step.
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
`/var/log/kismet`. It has two Wi-Fi radios, named by udev `.link` files that match
the USB chip rather than a MAC, so swapping a dongle keeps its name:

| Name | Radio | Job |
| --- | --- | --- |
| `wlan_host` | onboard (`brcmfmac`) | Joins home wifi; wigle-sync reaches the Pi through it |
| `wlan_mon0` | RTL8821CU (`0bda:c811`) | Kismet's capture source |

Kismet also captures from a long-range USB Bluetooth dongle (`hci0`), so BT and
BLE devices go into the `.wiglecsv` alongside Wi-Fi. The onboard Bluetooth is off
(`dtoverlay=disable-bt`, added by `bootstrap.sh`): it shared a chip and antenna
with `wlan_host`, and with it off the dongle is always `hci0`. `bootstrap.sh`
unblocks the adapter if it comes up soft-blocked, and checks that `hci0` is the
USB one (`btusb`); `pi_bt_dongle` says the same on the brick69 dashboard.

### Status LEDs

Three DIYables RGB LED modules sit on the header, stacked LED1 (top) to LED3;
`pi/leds/setup-leds.sh` (run by `bootstrap.sh`) sets them up.

| LED | Shows | R/G/B pins (BCM) |
| --- | --- | --- |
| LED1 | The onboard LEDs, which the case hides: red while powered, green on SD activity (red off for that moment). Kernel `gpio-led` overlays with the `actpwr` and `mmc0` triggers. | 16/20/21 |
| LED2 | Red and white, a second each, while Kismet heard a Flock camera or an Axon device in the last minute (`detect.py`'s rules); off otherwise, including at home with Kismet stopped. | 5/6/13 |
| LED3 | How hot the Pi is: **green** cool (as after a boot), **yellow** warm (5-minute average 55 °C or more), **orange** running hot (average 65 °C or more; a brief dip doesn't clear it), **flashing red** at 75 °C or more right now, close to the firmware's hard throttle at 80 °C. A Pi 3B+ slows its CPU from 60 °C. A level clears 2 °C under its threshold. Red and green mix on the kernel's software PWM (`pwm-gpio` overlays); blue is unused. | 17/27/22 |

LED2 and LED3 are `status-leds.service`, asking Kismet's REST API and reading the temperature every 3 seconds. To check the colours on the LEDs themselves, `sudo python3 /usr/local/lib/wigle-leds/status-leds.py --test` shows LED3 green, yellow, orange and red, then LED2's alert, 5 seconds each (it stops the service while it runs and starts it again). Kismet's web login is
set by the setup script to a random password, kept only on the Pi in
`~/.kismet/kismet_httpd.conf` (user `wigle`); before that, whoever reached
port 2501 first could choose it.

There's no phone AP any more (it was an RTL8188EUS running hostapd, retired
2026-10-15 for the dongle's USB slot), so Kismet's UI isn't reachable on the
road; at home it's on the home Wi-Fi at port 2501. `bootstrap.sh` removes what
the AP left behind on a Pi that had it (its units and config, hostapd, and the
patched `realtek-rtl8188eus` DKMS driver).

GPS is a u-blox 7 on USB, read through gpsd. Once home wifi has been up for 15
minutes, a NetworkManager dispatcher script stops Kismet, and deletes the session
if the Pi never left home (a boot in the driveway shouldn't upload the home
network).

`pi/bootstrap.sh` builds all of this on a fresh Raspberry Pi OS (or Kali) install
and is safe to re-run. Kismet comes from Kismet's own apt repo on Raspberry Pi OS.
The home SSID lives only on the Pi, in `/etc/default/kismet-pi`
(start from `pi/kismet-pi.env.example`):

```bash
scp -r pi zaphod@brick69:/tmp/wigle-pi
ssh zaphod@brick69 'sudo install -m 600 /tmp/wigle-pi/kismet-pi.env.example /etc/default/kismet-pi'
ssh -t zaphod@brick69 'sudoedit /etc/default/kismet-pi'
ssh zaphod@brick69 'sudo bash /tmp/wigle-pi/bootstrap.sh'
```

It ends with checks (radio names, monitor mode, the Bluetooth dongle as `hci0`,
gpsd) and exits 1 if any fail.

When replacing the Pi, copy `/etc/ssh/ssh_host_*` from the old one to keep the
host key pinned in `known_hosts` below, and `~/.kismet/kismet_httpd.conf` to keep
the Kismet UI login.

The app connects as a dedicated `wigle-sync` user that can only use SFTP, and
whose only access is an ACL on that log dir. `pi/setup-pi-user.sh` sets this up
(bootstrap runs it when `WIGLE_SYNC_PUBKEY` is set) and is safe to re-run:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/wigle_sync_ed25519 -N "" -C wigle-sync
ssh zaphod@brick69 "sudo bash -s -- '$(cat ~/.ssh/wigle_sync_ed25519.pub)'" < pi/setup-pi-user.sh
ssh-keyscan -t ed25519 brick69 | sed "s/^brick69 /brick69,192.168.1.208 /" > known_hosts   # pins the Pi's host key, by name and address
```

WiGLE rejects uploads over 180 MiB, and Kismet can't rotate `.kismet` logs by
size (only pcapng, via `pcapng_log_max_mb`). `kismet-size-watch.timer` checks the
log Kismet has open every 5 minutes and restarts Kismet once it passes 150 MiB, which
starts a new log. Change the cap with `KISMET_MAX_MB` in
`pi/kismet-size-watch.service`. To install, or reinstall after a change:

```bash
scp pi/kismet-size-watch.* pi/setup-kismet-size-watch.sh zaphod@brick69:/tmp/
ssh zaphod@brick69 'sudo bash /tmp/setup-kismet-size-watch.sh'
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

Besides the unit tests, two files run the real thing end to end. In
`tests/test_functional_sync.py`, `sync.run()` talks to `tests/fake_pi.py`: a
real SSH/SFTP server (paramiko's server side) that only accepts one key and
has a pinned host key, serving a temp directory, plus a stub WiGLE that
records uploads. It covers a drive (only the `.wiglecsv` uploaded, the
`.kismet` and empty sessions archived, a still-open file left), a rejected
upload, WiGLE unreachable, a changed host key, and the Pi away. In
`tests/test_functional_console.py`, the console app with its background
threads runs against one stub playing Prometheus, Loki and WiGLE (which, like
WiGLE, answers 406 to a KML request that asks for JSON).

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

Argo CD deploys all of `k8s_config/` from `main` (brick-k8s-config's
`argocd/apps/wigle-sync.yaml`), so a merge is the deploy, within about 3
minutes; a change made by hand is put back. To try a branch on the cluster
first: `scripts/trial-branch wigle-sync <branch>` in brick-k8s-config (and `scripts/trial-branch wigle-sync main` after the merge).

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

## Metrics

- **The sync job** pushes `wigle_sync_*` to the Pushgateway at the end of
  each run ("WiGLE Sync" dashboard, `k8s_config/metrics/`).
- **wigle-console** serves `/metrics` (`console_metrics.py`, scraped through
  `k8s_config/metrics/console-podmonitor.yaml`): requests by route, the
  radar's refreshes, drives and contacts, failed status sections, and the
  account's standing on WiGLE (rank, networks and Bluetooth devices found,
  uploads still processing), read from the console's own cached fetch. The
  "WiGLE: console, radar and account" dashboard draws them.
- **The Pi** (`pi/metrics/`, run by `bootstrap.sh`): node-exporter on
  localhost, plus `pi-metrics.py` every 30 s: Kismet's open log size and
  the logs waiting, the GPS fix and satellites, whether the Bluetooth dongle is `hci0`, power and
  throttling flags, home or away. Prometheus can't reach the Pi while it's
  out, so a **Prometheus agent** on it scrapes those and remote-writes to
  `prometheus.brick.nozdormu.cloud`; away, the samples wait in its WAL (up
  to 24 hours) and arrive once it's home (the cluster's Prometheus accepts
  them up to 24 hours late). The Pi has no RTC, so **chrony** takes GPS
  time from gpsd (run with `-n`) when there's no internet, and those
  samples carry the right time. The "brick69" dashboard draws it all.
