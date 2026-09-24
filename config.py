from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    wigle_api_name: str
    wigle_api_token: str
    wigle_donate: bool
    pi_host: str
    pi_port: int
    pi_user: str
    pi_ssh_key_path: str
    pi_known_hosts_path: str | None
    pi_remote_dir: str
    pi_archive_dir: str
    pi_connect_timeout: float
    pi_probe_timeout: float
    min_file_age_seconds: int
    file_extensions: tuple[str, ...]
    compress: bool

    @classmethod
    def from_env(cls) -> Settings:
        # Locally this picks up the repo's .env; in k8s the vars come from a Secret
        # and there's no .env, so this is a no-op.
        load_dotenv()

        remote_dir = os.environ.get("PI_REMOTE_DIR", "/home/pi/kismet")
        return cls(
            wigle_api_name=os.environ["WIGLE_API_NAME"],
            wigle_api_token=os.environ["WIGLE_API_TOKEN"],
            wigle_donate=_bool(os.environ.get("WIGLE_DONATE", "false")),
            pi_host=os.environ["PI_HOST"],
            pi_port=int(os.environ.get("PI_PORT", "22")),
            pi_user=os.environ.get("PI_USER", "pi"),
            pi_ssh_key_path=os.path.expanduser(os.environ.get("PI_SSH_KEY_PATH", "~/.ssh/id_ed25519")),
            pi_known_hosts_path=os.path.expanduser(os.environ.get("PI_KNOWN_HOSTS_PATH", "")) or None,
            pi_remote_dir=remote_dir,
            pi_archive_dir=os.environ.get("PI_ARCHIVE_DIR", f"{remote_dir.rstrip('/')}/uploaded"),
            pi_connect_timeout=float(os.environ.get("PI_CONNECT_TIMEOUT", "10")),
            pi_probe_timeout=float(os.environ.get("PI_PROBE_TIMEOUT", "3")),
            min_file_age_seconds=int(os.environ.get("MIN_FILE_AGE_SECONDS", "300")),
            file_extensions=tuple(
                ext.strip() for ext in os.environ.get("FILE_EXTENSIONS", ".kismet,.wiglecsv").split(",") if ext.strip()
            ),
            compress=_bool(os.environ.get("COMPRESS_UPLOADS", "true")),
        )
