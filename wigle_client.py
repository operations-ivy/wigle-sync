from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

WIGLE_API_URL = "https://api.wigle.net/api/v2"
# WiGLE rejects uploads over 180 MiB. Compressing first keeps most .kismet
# (sqlite) logs well under that.
MAX_UPLOAD_BYTES = 180 * 1024 * 1024


class WigleUploadError(Exception):
    pass


class WigleClient:
    def __init__(self, api_name: str, api_token: str, donate: bool = False) -> None:
        self.donate = donate
        self.session = requests.Session()
        self.session.auth = (api_name, api_token)
        self.session.headers["Accept"] = "application/json"

    def check_auth(self) -> dict[str, Any]:
        resp = self.session.get(f"{WIGLE_API_URL}/profile/user", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def upload(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise WigleUploadError(f"{path.name} is {size} bytes, over WiGLE's 180 MiB upload limit")

        data = {"donate": "on"} if self.donate else {}
        with path.open("rb") as fh:
            # Generous read timeout: WiGLE doesn't respond until it has the whole file.
            resp = self.session.post(
                f"{WIGLE_API_URL}/file/upload",
                files={"file": (path.name, fh, "application/octet-stream")},
                data=data,
                timeout=(10, 600),
            )

        try:
            body = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise WigleUploadError(f"Non-JSON response from WiGLE: {resp.text[:200]}")

        if not resp.ok or not body.get("success"):
            raise WigleUploadError(f"WiGLE rejected {path.name} ({resp.status_code}): {body.get('message', body)}")
        return body
