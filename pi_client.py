from __future__ import annotations

import posixpath
import stat
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import paramiko
import structlog

from config import Settings

log = structlog.get_logger()


@dataclass(frozen=True)
class RemoteFile:
    name: str
    path: str
    size: int
    mtime: int
    # SQLite rollback journal left beside a .kismet when Kismet stopped mid-write.
    journal_name: str | None = None


def _session(filename: str) -> str:
    # "Kismet-20260924-15-25-36-1.kismet-journal" -> "Kismet-20260924-15-25-36-1"
    return filename.split(".", 1)[0]


def select_ready_files(
    entries: list[paramiko.SFTPAttributes],
    remote_dir: str,
    extensions: tuple[str, ...],
    min_age_seconds: int,
    now: float,
) -> list[RemoteFile]:
    """Pick the capture files that are safe to upload.

    Kismet holds every file of a session (`Kismet-<ts>-1.kismet`, its
    `-journal`, `.wiglecsv`) open until it stops, so readiness is judged per
    session: a file is only ready once *all* of its session's files have been
    quiet for `min_age_seconds`. Judging files individually isn't enough — with
    no GPS fix (e.g. parked indoors) the live .wiglecsv sits untouched at just
    its header while the .kismet db is still written every few seconds.
    """
    journals = {e.filename.removesuffix("-journal"): e for e in entries if e.filename.endswith("-journal")}
    session_mtime: dict[str, float] = defaultdict(float)
    for entry in entries:
        stem = _session(entry.filename)
        session_mtime[stem] = max(session_mtime[stem], entry.st_mtime or 0)

    ready = []
    for entry in entries:
        if entry.st_mode is None or not stat.S_ISREG(entry.st_mode):
            continue
        if not entry.filename.endswith(extensions):
            continue
        journal = journals.get(entry.filename)
        mtime = session_mtime[_session(entry.filename)]
        if now - mtime < min_age_seconds:
            log.info("Skipping file still being written", file=entry.filename)
            continue
        ready.append(
            RemoteFile(
                name=entry.filename,
                path=posixpath.join(remote_dir, entry.filename),
                size=entry.st_size,
                mtime=mtime,
                journal_name=journal.filename if journal else None,
            )
        )
    return sorted(ready, key=lambda f: f.mtime)


class PiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._ssh = paramiko.SSHClient()
        self._sftp: paramiko.SFTPClient | None = None

    def connect(self) -> None:
        if self.settings.pi_known_hosts_path:
            self._ssh.load_host_keys(self.settings.pi_known_hosts_path)
            self._ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            log.warning("PI_KNOWN_HOSTS_PATH not set, trusting the Pi's host key blindly")
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        self._ssh.connect(
            hostname=self.settings.pi_host,
            port=self.settings.pi_port,
            username=self.settings.pi_user,
            key_filename=self.settings.pi_ssh_key_path,
            timeout=self.settings.pi_connect_timeout,
            banner_timeout=self.settings.pi_connect_timeout,
            auth_timeout=self.settings.pi_connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        self._sftp = self._ssh.open_sftp()

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        self._ssh.close()

    @property
    def sftp(self) -> paramiko.SFTPClient:
        assert self._sftp is not None, "call connect() first"
        return self._sftp

    def remote_now(self) -> float:
        """The Pi's own idea of the current time.

        The Pi has no RTC: it boots with a stale clock and only corrects it once
        NTP syncs on the home network (never during a drive), so it can be hours
        off. File mtimes are stamped by that clock, so a file's age has to be
        measured against it too, not the cluster's clock, or Kismet's live
        session file can look hours old and get uploaded mid-write.
        """
        probe = ".wigle-sync-clock"  # relative to the sync user's home dir
        with self.sftp.open(probe, "w"):
            pass
        return self.sftp.stat(probe).st_mtime

    def list_ready_files(self) -> list[RemoteFile]:
        entries = self.sftp.listdir_attr(self.settings.pi_remote_dir)
        return select_ready_files(
            entries,
            self.settings.pi_remote_dir,
            self.settings.file_extensions,
            self.settings.min_file_age_seconds,
            now=self.remote_now(),
        )

    def download(self, remote_file: RemoteFile, local_dir: Path) -> Path:
        local_path = local_dir / remote_file.name
        self.sftp.get(remote_file.path, str(local_path))
        if remote_file.journal_name:
            self.sftp.get(
                posixpath.join(self.settings.pi_remote_dir, remote_file.journal_name),
                str(local_dir / remote_file.journal_name),
            )
        return local_path

    def archive(self, remote_file: RemoteFile) -> None:
        """Move an uploaded file out of the capture dir so it's never uploaded twice."""
        archive_dir = self.settings.pi_archive_dir
        try:
            self.sftp.stat(archive_dir)
        except FileNotFoundError:
            self.sftp.mkdir(archive_dir)
        for name in filter(None, (remote_file.name, remote_file.journal_name)):
            self.sftp.posix_rename(posixpath.join(self.settings.pi_remote_dir, name), posixpath.join(archive_dir, name))
