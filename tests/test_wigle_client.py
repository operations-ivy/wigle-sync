from __future__ import annotations

from unittest import mock

import pytest

from wigle_client import WigleClient
from wigle_client import WigleUploadError


def _response(status: int, body: dict) -> mock.Mock:
    resp = mock.Mock(status_code=status, ok=200 <= status < 300)
    resp.json.return_value = body
    return resp


def test_upload_posts_file_with_basic_auth(tmp_path):
    path = tmp_path / "Kismet-1.wiglecsv"
    path.write_text("WigleWifi-1.4\n")
    client = WigleClient("AIDname", "token", donate=True)

    with mock.patch.object(client.session, "post", return_value=_response(200, {"success": True})) as post:
        client.upload(path)

    assert client.session.auth == ("AIDname", "token")
    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "https://api.wigle.net/api/v2/file/upload"
    assert kwargs["files"]["file"][0] == "Kismet-1.wiglecsv"
    assert kwargs["data"] == {"donate": "on"}


def test_upload_raises_when_wigle_reports_failure(tmp_path):
    path = tmp_path / "Kismet-1.kismet"
    path.write_bytes(b"sqlite")
    client = WigleClient("AIDname", "token")

    failed = _response(200, {"success": False, "message": "bad file"})
    with mock.patch.object(client.session, "post", return_value=failed):
        with pytest.raises(WigleUploadError, match="bad file"):
            client.upload(path)
