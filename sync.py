from __future__ import annotations

import argparse
import socket
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

from config import Settings
from observability import init_logging
from observability import init_tracing
from observability import push_metrics
from observability import RunStats
from pi_client import PiClient
from pi_client import RemoteFile
from wigle_client import WigleClient

log = structlog.get_logger()
tracer = trace.get_tracer(__name__)


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


def pi_is_online(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def sync_file(pi: PiClient, wigle: WigleClient, remote_file: RemoteFile, settings: Settings, stats: RunStats) -> None:
    with tracer.start_as_current_span("sync_file") as span, tempfile.TemporaryDirectory() as tmp:
        span.set_attribute("file.name", remote_file.name)
        span.set_attribute("file.size", remote_file.size)
        span.set_attribute("file.has_journal", remote_file.journal_name is not None)

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

        if settings.compress:
            with tracer.start_as_current_span("compress"):
                upload_path = compress(local_path)
        else:
            upload_path = local_path
        upload_size = upload_path.stat().st_size
        span.set_attribute("upload.size", upload_size)

        with tracer.start_as_current_span("wigle.upload"):
            result = wigle.upload(upload_path)
        with tracer.start_as_current_span("pi.archive"):
            pi.archive(remote_file)

        span.set_attribute("sync.outcome", "uploaded")
        stats.uploaded += 1
        stats.bytes_uploaded += upload_size
        log.info(
            "Uploaded to WiGLE",
            file=remote_file.name,
            bytes=upload_size,
            transid=result.get("results", {}).get("transid"),
        )


def run(settings: Settings, stats: RunStats, dry_run: bool = False) -> None:
    # The Pi is out wardriving (or powered off) most of the time — that's the
    # normal case, not a failure. A bare TCP probe answers that in a few seconds
    # before any SSH handshake or WiGLE setup, and the job exits cleanly.
    with tracer.start_as_current_span("pi.probe"):
        stats.pi_online = pi_is_online(settings.pi_host, settings.pi_port, settings.pi_probe_timeout)
    if not stats.pi_online:
        log.info("Pi offline, nothing to do", host=settings.pi_host)
        return

    wigle = WigleClient(settings.wigle_api_name, settings.wigle_api_token, donate=settings.wigle_donate)

    pi = PiClient(settings)
    try:
        with tracer.start_as_current_span("pi.connect"):
            pi.connect()
    except OSError as e:
        # Answered the probe but dropped off before SSH finished (e.g. pulling out of the driveway).
        stats.pi_online = False
        log.info("Pi went away while connecting, nothing to do", host=settings.pi_host, error=str(e))
        return

    try:
        with tracer.start_as_current_span("pi.list"):
            ready = pi.list_ready_files()
        stats.files_found = len(ready)
        log.info("Found files ready to upload", count=len(ready), files=[f.name for f in ready])

        for remote_file in ready:
            if dry_run:
                log.info("Dry run, would upload", file=remote_file.name, size=remote_file.size)
                continue
            try:
                sync_file(pi, wigle, remote_file, settings, stats)
            except Exception:
                stats.failed += 1
                log.exception("Failed to sync file, leaving it on the Pi for next run", file=remote_file.name)
    finally:
        pi.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync wardriving logs from the Pi to WiGLE")
    parser.add_argument("--dry-run", action="store_true", help="list what would be uploaded without uploading")
    parser.add_argument("--check-auth", action="store_true", help="verify WiGLE credentials and exit")
    args = parser.parse_args()

    init_logging()
    settings = Settings.from_env()

    if args.check_auth:
        wigle = WigleClient(settings.wigle_api_name, settings.wigle_api_token)
        profile = wigle.check_auth()
        log.info("WiGLE credentials OK", user=profile.get("userid"))
        return 0

    provider = init_tracing()
    stats = RunStats()
    started = time.monotonic()
    try:
        with tracer.start_as_current_span("wigle_sync.run") as span:
            try:
                run(settings, stats, dry_run=args.dry_run)
            except Exception as e:
                # Anything escaping run() (auth failure, missing log dir...) is a failed run.
                stats.failed += 1
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR))
                log.exception("Sync run failed")
            for key, value in vars(stats).items():
                span.set_attribute(f"sync.{key}", value)
            if not stats.succeeded:
                span.set_status(Status(StatusCode.ERROR))
        stats.duration_seconds = time.monotonic() - started
        log.info("Sync complete", **vars(stats))
        if not args.dry_run:
            push_metrics(stats, finished_at=time.time())
    finally:
        # The pod exits right after this; flush buffered spans or they're lost.
        if provider is not None:
            provider.shutdown()

    return 0 if stats.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
