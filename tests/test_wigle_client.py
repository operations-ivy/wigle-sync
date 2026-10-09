from __future__ import annotations

from unittest import mock

import pytest
import requests

from wigle_client import WigleClient
from wigle_client import WigleUnreachable
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


def test_upload_raises_unreachable_when_wigle_cannot_be_reached(tmp_path):
    path = tmp_path / "Kismet-1.kismet"
    path.write_bytes(b"sqlite")
    client = WigleClient("AIDname", "token")

    offline = requests.ConnectionError("Failed to resolve 'api.wigle.net'")
    with mock.patch.object(client.session, "post", side_effect=offline):
        with pytest.raises(WigleUnreachable):
            client.upload(path)


def test_transactions_walks_every_page():
    client = WigleClient("AIDname", "token")
    pages = [{"results": [{"transid": i} for i in range(start, min(start + 2, 5))]} for start in (0, 2, 4)]
    responses = [_response(200, p) for p in pages]
    for r in responses:
        r.raise_for_status.return_value = None

    with mock.patch.object(client.session, "get", side_effect=responses) as get:
        got = client.transactions(page_size=2)

    assert [t["transid"] for t in got] == [0, 1, 2, 3, 4]
    assert [c.kwargs["params"] for c in get.call_args_list] == [
        {"pagestart": 0, "pageend": 2},
        {"pagestart": 2, "pageend": 2},
        {"pagestart": 4, "pageend": 2},
    ]
