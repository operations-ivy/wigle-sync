from __future__ import annotations

import argparse
import csv
import io
import math
import os
import socket
import sqlite3
import statistics
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import brick_observe
from opentelemetry import trace
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

import detect
import follows
import ledger as ledgers
import report
from config import Settings
from observability import BYTES_TOTAL
from observability import push_metrics
from observability import push_totals
from observability import RunStats
from observability import SERVICE_NAME
from observability import UPLOADED_TOTAL
from pi_client import PiClient
from pi_client import RemoteFile
from wigle_client import WigleClient
from wigle_client import WigleUnreachable

log = brick_observe.get_logger(__name__)
tracer = trace.get_tracer(__name__)
# The log line the console finds the newest report by (stats.after_action).
REPORT_EVENT = "After-action report"


def compress(path: Path) -> Path:
    archive_path = path.with_name(f"{path.name}.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(path, arcname=path.name)
    return archive_path


def recover_kismet_db(path: Path) -> None:
    """Replay a hot rollback journal into the .kismet sqlite db.

    Kismet killed mid-write (power pulled at the end of a drive) leaves a
    `-journal` beside the db; without it the db holds a half-applied transaction.
    Opening it with the journal alongside makes SQLite roll that back.
    """
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        log.warning("Kismet db failed integrity check, uploading anyway", file=path.name, result=result)


def has_observations(path: Path) -> bool:
    """A .wiglecsv is two header lines before any data; Kismet leaves header-only files for empty sessions."""
    if path.stat().st_size == 0:
        return False
    if path.suffix != ".wiglecsv":
        return True
    with path.open("rb") as fh:
        return sum(1 for _ in zip(range(3), fh)) > 2


# A session whose GPS fixes all lie within this of their median never went
# anywhere: booted at home and switched off before kismet-home-stop could
# remove it (#26), or parked. GPS indoors wanders tens of metres.
STATIONARY_METRES = 150


def stationary(path: Path) -> bool:
    """True when every GPS fix in a .wiglecsv is within STATIONARY_METRES of
    their median: the Pi never moved. False with no fixes to judge by."""
    if path.suffix != ".wiglecsv":
        return False
    lines = path.read_text(errors="replace").replace("\0", "").splitlines()
    fixes = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[1:]))):
        try:
            lat, lon = float(row.get("CurrentLatitude") or ""), float(row.get("CurrentLongitude") or "")
        except ValueError:
            continue
        if lat or lon:
            fixes.append((lat, lon))
    if not fixes:
        return False
    mid_lat = statistics.median(f[0] for f in fixes)
    mid_lon = statistics.median(f[1] for f in fixes)
    # Equirectangular: exact enough over a few hundred metres.
    scale = math.cos(math.radians(mid_lat))
    return all(
        6_371_000 * math.radians(math.hypot(lat - mid_lat, (lon - mid_lon) * scale)) <= STATIONARY_METRES
        for lat, lon in fixes
    )


def drop_own_radios(path: Path, own: frozenset[str]) -> int:
    """Remove the Pi's own radios from a .wiglecsv in place (its phone AP, which
    travels with it: not a network to put on WiGLE's map). Returns how many
    rows went. `own` holds lower-case MACs."""
    if not own or path.suffix != ".wiglecsv":
        return 0
    # surrogateescape both ways: an SSID that isn't UTF-8 is uploaded byte for byte.
    lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
    header, rows = lines[:2], lines[2:]
    keep = [row for row in rows if row.split(",", 1)[0].strip().lower() not in own]
    dropped = len(rows) - len(keep)
    if dropped:
        path.write_text("".join(header + keep), encoding="utf-8", errors="surrogateescape")
    return dropped


def publish_sightings(watch: follows.NullFollows, path: Path, key: str, stats: RunStats) -> None:
    """A drive's followable devices onto the sightings topic (#83). Never holds
    up the upload: a failure is logged and the drive is sent again next time
    it's synced (or backfilled)."""
    try:
        found = follows.sightings(path.read_text(encoding="utf-8", errors="surrogateescape"), key)
        watch.publish(found)
    except Exception:
        log.warning("Sightings not published", file=path.name, exc_info=True)
        return
    stats.sightings += len(found)


def check_follows(pi: PiClient, watch: follows.NullFollows, own_radios: frozenset[str], stats: RunStats) -> None:
    """The follows-me candidates from the whole history: onto wigle-sync.follows
    for the console, and their addresses into follows.txt on the Pi for LED2.
    Logs counts only (follows.py)."""
    if not watch.available:
        return
    try:
        rules = follows.Rules.from_env()
        candidates = watch.history.candidates(rules, frozenset(m.upper() for m in own_radios))
        watch.publish_candidates(candidates, rules)
        pi.write_follows(c["mac"] for c in candidates)
    except Exception:
        log.warning("Follows-me check failed", exc_info=True)
        return
    stats.follows_candidates = len(candidates)
    log.info(
        "Follows-me check",
        candidates=len(candidates),
        devices=len(watch.history.devices),
        sightings=len(watch.history),
    )


def pi_is_online(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def sync_file(
    pi: PiClient,
    wigle: WigleClient,
    remote_file: RemoteFile,
    settings: Settings,
    stats: RunStats,
    ledger: ledgers.NullLedger | None = None,
    own_radios: frozenset[str] = frozenset(),
    watch: follows.NullFollows | None = None,
) -> None:
    ledger = ledger or ledgers.NullLedger()
    watch = watch or follows.NullFollows()
    with tracer.start_as_current_span("sync_file") as span, tempfile.TemporaryDirectory() as tmp:
        span.set_attribute("file.name", remote_file.name)
        span.set_attribute("file.size", remote_file.size)
        span.set_attribute("file.has_journal", remote_file.journal_name is not None)

        # Not downloaded at all: a .kismet can be well over 100 MB.
        if not remote_file.name.endswith(settings.upload_extensions):
            span.set_attribute("sync.outcome", "archived_kept")
            log.info("Not an upload format, archiving on the Pi without upload", file=remote_file.name)
            with tracer.start_as_current_span("pi.archive"):
                pi.archive(remote_file)
            stats.archived_kept += 1
            return

        with tracer.start_as_current_span("pi.download"):
            local_path = pi.download(remote_file, Path(tmp))
        if remote_file.journal_name:
            with tracer.start_as_current_span("kismet.recover"):
                recover_kismet_db(local_path)

        if not has_observations(local_path):
            span.set_attribute("sync.outcome", "archived_empty")
            log.info("No observations in file, archiving without upload", file=remote_file.name)
            with tracer.start_as_current_span("pi.archive"):
                pi.archive(remote_file)
            stats.archived_empty += 1
            return

        if stationary(local_path):
            span.set_attribute("sync.outcome", "archived_stationary")
            log.info("The Pi never moved in this session, archiving without upload", file=remote_file.name)
            with tracer.start_as_current_span("pi.archive"):
                pi.archive(remote_file)
            stats.archived_stationary += 1
            return

        # Already sent, by the ledger (#21): copied back onto the Pi, or uploaded/ was wiped.
        key = ledgers.file_key(local_path)
        prior = ledger.uploaded(key)
        if prior:
            span.set_attribute("sync.outcome", "already_uploaded")
            log.info(
                "Already uploaded, by the ledger; archiving without upload",
                file=remote_file.name,
                uploaded_as=prior.get("file"),
                uploaded_at=prior.get("at"),
            )
            with tracer.start_as_current_span("pi.archive"):
                pi.archive(remote_file)
            stats.already_uploaded += 1
            return

        with tracer.start_as_current_span("detect"):
            found = detect.scan(local_path)
        if found:
            # Counts only: no addresses, names or places (detect.py).
            stats.detections.update(found)
            log.info("Surveillance devices on this drive", file=remote_file.name, **found)
        span.set_attribute("detect.alerting", sum(detect.alerting(found).values()))

        # The console's after-action report: only the sync ever has the file.
        # A report that fails to build never holds up the upload.
        with tracer.start_as_current_span("report"):
            try:
                after_action = report.build(local_path)
            except Exception:
                log.warning("After-action report failed", file=remote_file.name, exc_info=True)
            else:
                if after_action:
                    log.info(REPORT_EVENT, file=remote_file.name, report=after_action)

        dropped = drop_own_radios(local_path, own_radios)
        if dropped:
            log.info("Left the Pi's own radios out of the upload", file=remote_file.name, rows=dropped)

        if watch.available:
            with tracer.start_as_current_span("follows.publish"):
                publish_sightings(watch, local_path, key, stats)

        if settings.compress:
            with tracer.start_as_current_span("compress"):
                upload_path = compress(local_path)
        else:
            upload_path = local_path
        upload_size = upload_path.stat().st_size
        span.set_attribute("upload.size", upload_size)

        with tracer.start_as_current_span("wigle.upload"):
            result = wigle.upload(upload_path)
        # Recorded before archiving: a crash between the two must not upload it twice.
        ledger.record(key, "uploaded", file=remote_file.name, bytes=upload_size, wigle_results=result.get("results"))
        with tracer.start_as_current_span("pi.archive"):
            pi.archive(remote_file)

        span.set_attribute("sync.outcome", "uploaded")
        stats.uploaded += 1
        stats.bytes_uploaded += upload_size
        log.info(
            "Uploaded to WiGLE",
            file=remote_file.name,
            bytes=upload_size,
            # WiGLE returns the transaction id(s) in `results`; log it whole rather than
            # guess at the field shape.
            wigle_results=result.get("results"),
        )


def run(settings: Settings, stats: RunStats, dry_run: bool = False) -> None:
    """Sync every Pi in turn (#88), into one run's stats. One Pi failing
    (its SSH, its log dir) is counted and logged, and the next is still
    synced."""
    for host in settings.hosts:
        with tracer.start_as_current_span("pi.sync", attributes={"pi.host": host}):
            try:
                sync_pi(settings.for_host(host), stats, dry_run)
            except Exception:
                stats.failed += 1
                log.exception("Failed to sync the Pi, trying the next", host=host)


def sync_pi(settings: Settings, stats: RunStats, dry_run: bool = False) -> None:
    """One Pi: settings.pi_host."""
    host = settings.pi_host
    stats.pis[host] = False
    # The Pi is out wardriving (or powered off) most of the time — that's the
    # normal case, not a failure. A bare TCP probe answers that in a few seconds
    # before any SSH handshake or WiGLE setup, and the job exits cleanly.
    with tracer.start_as_current_span("pi.probe"):
        online = pi_is_online(host, settings.pi_port, settings.pi_probe_timeout)
    if not online:
        log.info("Pi offline, nothing to do", host=host)
        return

    wigle = WigleClient(settings.wigle_api_name, settings.wigle_api_token, donate=settings.wigle_donate)

    pi = PiClient(settings)
    try:
        with tracer.start_as_current_span("pi.connect"):
            pi.connect()
    except OSError as e:
        # Answered the probe but dropped off before SSH finished (e.g. pulling out of the driveway).
        log.info("Pi went away while connecting, nothing to do", host=host, error=str(e))
        return
    stats.pis[host] = True
    stats.pi_online = True

    watch: follows.NullFollows = follows.NullFollows()
    with tracer.start_as_current_span("ledger.load"):
        ledger = ledgers.open_ledger(os.environ.get("KAFKA_BOOTSTRAP"), os.environ.get("LEDGER_TOPIC", ledgers.TOPIC))
    try:
        with tracer.start_as_current_span("pi.list"):
            ready = pi.list_ready_files()
        stats.files_found = len(ready)
        log.info("Found files ready to upload", host=host, count=len(ready), files=[f.name for f in ready])
        own_radios = pi.own_radios() if ready else frozenset()
        if ready and not dry_run:
            with tracer.start_as_current_span("follows.load"):
                watch = follows.open_follows(os.environ.get("KAFKA_BOOTSTRAP"))

        for i, remote_file in enumerate(ready):
            if dry_run:
                log.info("Dry run, would upload", file=remote_file.name, size=remote_file.size)
                continue
            try:
                sync_file(pi, wigle, remote_file, settings, stats, ledger, own_radios, watch)
            except WigleUnreachable as e:
                # The home internet is down, not something wigle-sync got wrong. Every
                # file is still on the Pi, so the next run picks them up; stop trying now.
                stats.deferred += len(ready) - i
                log.info(
                    "WiGLE unreachable, leaving files on the Pi for next run", deferred=stats.deferred, error=str(e)
                )
                break
            except Exception:
                stats.failed += 1
                log.exception(
                    "Failed to sync file, leaving it on the Pi for next run", host=host, file=remote_file.name
                )
        with tracer.start_as_current_span("follows.check"):
            check_follows(pi, watch, own_radios, stats)
    finally:
        watch.close()
        ledger.close()
        pi.close()


def backfill_sightings(settings: Settings, stats: RunStats) -> int:
    """Publish the sightings of every drive already archived on each Pi (the
    history before #83), then run the follows-me check. Safe to repeat: a
    drive's records replace themselves."""
    watch = follows.open_follows(os.environ.get("KAFKA_BOOTSTRAP"))
    if not watch.available:
        log.error("Kafka unset or unreadable: nothing to backfill into")
        return 1
    try:
        for host in settings.hosts:
            try:
                backfill_pi(settings.for_host(host), watch, stats)
            except OSError as e:
                log.warning("Pi unreachable, not backfilled", host=host, error=str(e))
    finally:
        watch.close()
    return 0


def backfill_pi(settings: Settings, watch: follows.NullFollows, stats: RunStats) -> None:
    """backfill_sightings for one Pi: settings.pi_host."""
    pi = PiClient(settings)
    try:
        pi.connect()
        stats.pis[settings.pi_host] = True
        stats.pi_online = True
        archived = pi.list_archived(".wiglecsv")
        log.info("Backfilling sightings", host=settings.pi_host, files=len(archived))
        own_radios = pi.own_radios()
        for remote_file in archived:
            with tempfile.TemporaryDirectory() as tmp:
                local_path = pi.download(remote_file, Path(tmp))
                if has_observations(local_path) and not stationary(local_path):
                    publish_sightings(watch, local_path, ledgers.file_key(local_path), stats)
        check_follows(pi, watch, own_radios, stats)
    finally:
        pi.close()
    log.info("Backfill complete", host=settings.pi_host, files=len(archived), sightings=stats.sightings)


def wigle_totals(settings: Settings) -> dict[str, float]:
    """The lifetime totals from WiGLE's record of every upload on the account,
    including any made outside wigle-sync."""
    uploads = WigleClient(settings.wigle_api_name, settings.wigle_api_token).transactions()
    return {
        UPLOADED_TOTAL: float(len(uploads)),
        BYTES_TOTAL: float(sum(t.get("fileSize") or 0 for t in uploads)),
    }


def seed_totals(settings: Settings, dry_run: bool = False) -> int:
    """Set the lifetime totals from WiGLE's record of every upload on the account.

    Each run already puts back a total the Pushgateway lost (push_metrics);
    this forces a recount now, e.g. to check one.
    """
    totals = wigle_totals(settings)
    log.info("Counted WiGLE's upload history", **totals, dry_run=dry_run)
    if dry_run:
        return 0
    gateway = os.environ.get("PUSHGATEWAY_URL")
    if not gateway:
        log.error("PUSHGATEWAY_URL is not set")
        return 1
    push_totals(gateway, totals)
    log.info("Seeded the upload totals", gateway=gateway)
    return 0


# RunStats fields a span can't take as they are (span_attributes).
NON_SCALAR_STATS = frozenset({"detections", "pis"})


def span_attributes(stats: RunStats) -> dict:
    """The run's stats as span attributes: a span takes only plain values,
    so detections become one attribute per kind found."""
    out = {f"sync.{k}": v for k, v in vars(stats).items() if k not in NON_SCALAR_STATS and v is not None}
    out.update({f"sync.detections.{k}": n for k, n in stats.detections.items()})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync wardriving logs from the Pi to WiGLE")
    parser.add_argument("--dry-run", action="store_true", help="list what would be uploaded without uploading")
    parser.add_argument("--check-auth", action="store_true", help="verify WiGLE credentials and exit")
    parser.add_argument(
        "--seed-totals",
        action="store_true",
        help="recount the lifetime upload totals from WiGLE's upload history and push them (--dry-run: print only)",
    )
    parser.add_argument(
        "--backfill-sightings",
        action="store_true",
        help="publish the follows-me sightings of every drive archived on the Pi, then check (#83)",
    )
    args = parser.parse_args()

    brick_observe.init(SERVICE_NAME)
    settings = Settings.from_env()

    if args.check_auth:
        wigle = WigleClient(settings.wigle_api_name, settings.wigle_api_token)
        profile = wigle.check_auth()
        log.info("WiGLE credentials OK", user=profile.get("userid"))
        return 0

    if args.seed_totals:
        return seed_totals(settings, dry_run=args.dry_run)

    if args.backfill_sightings:
        return backfill_sightings(settings, RunStats())

    if args.dry_run:
        os.environ.pop("PUSHGATEWAY_URL", None)  # a rehearsal isn't a run
    stats = RunStats()
    started = time.monotonic()
    with brick_observe.job(SERVICE_NAME) as job, tracer.start_as_current_span("wigle_sync.run") as span:
        try:
            run(settings, stats, dry_run=args.dry_run)
        except Exception as e:
            # Anything escaping run() (auth failure, missing log dir...) is a failed run.
            stats.failed += 1
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR))
            log.exception("Sync run failed")
        span.set_attributes(span_attributes(stats))
        if not stats.succeeded:
            span.set_status(Status(StatusCode.ERROR))
            job.fail(f"{stats.failed} failed")
        stats.duration_seconds = time.monotonic() - started
        log.info("Sync complete", **{**vars(stats), "detections": dict(stats.detections), "pis": dict(stats.pis)})
        if not args.dry_run:
            push_metrics(stats, finished_at=time.time(), recount=lambda: wigle_totals(settings))

    return 0 if stats.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
