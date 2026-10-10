"""A stand-in for the wardriving Pi and for WiGLE, for the functional tests.

FakePi is a real SSH server (paramiko's server side) on 127.0.0.1 that only
accepts one key and serves a temp directory over SFTP, so PiClient connects,
checks the host key, lists, downloads and renames exactly as it does against
the Pi. FakeWigle is an HTTP server that records uploads and answers like
WiGLE's /file/upload.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paramiko


class _Server(paramiko.ServerInterface):
    def __init__(self, allowed: paramiko.PKey) -> None:
        self.allowed = allowed

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        ok = key.get_base64() == self.allowed.get_base64()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def _sftp_root(root: Path):
    class Handle(paramiko.SFTPHandle):
        pass

    class SFTP(paramiko.SFTPServerInterface):
        def _real(self, path: str) -> str:
            # Everything resolves inside root, absolute or relative to "home".
            return str(root / path.lstrip("/"))

        def _err(self, e: OSError) -> int:
            return paramiko.SFTPServer.convert_errno(e.errno)

        def list_folder(self, path):
            try:
                out = []
                for name in os.listdir(self._real(path)):
                    attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                    attr.filename = name
                    out.append(attr)
                return out
            except OSError as e:
                return self._err(e)

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as e:
                return self._err(e)

        lstat = stat

        def open(self, path, flags, attr):
            real = self._real(path)
            try:
                fd = os.open(real, flags, 0o644)
                mode = "rb" if flags & (os.O_WRONLY | os.O_RDWR) == 0 else ("ab" if flags & os.O_APPEND else "wb")
                if flags & os.O_RDWR:
                    mode = "r+b"
                f = os.fdopen(fd, mode)
            except OSError as e:
                return self._err(e)
            h = Handle(flags)
            h.filename = real
            h.readfile = f if "r" in mode or "+" in mode else None
            h.writefile = f if "r" not in mode or "+" in mode else None
            return h

        def remove(self, path):
            try:
                os.remove(self._real(path))
            except OSError as e:
                return self._err(e)
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            try:
                os.rename(self._real(oldpath), self._real(newpath))
            except OSError as e:
                return self._err(e)
            return paramiko.SFTP_OK

        posix_rename = rename

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._real(path))
            except OSError as e:
                return self._err(e)
            return paramiko.SFTP_OK

    return SFTP


class FakePi:
    def __init__(self, root: Path, client_key: paramiko.PKey) -> None:
        self.root = root
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._client_key = client_key
        self._transports: list[paramiko.Transport] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, _sftp_root(self.root))
            try:
                t.start_server(server=_Server(self._client_key))
            except (paramiko.SSHException, EOFError, OSError):
                continue
            self._transports.append(t)

    def known_hosts_line(self) -> str:
        return f"[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n"

    def close(self) -> None:
        for t in self._transports:
            t.close()
        self.sock.close()


class FakeWigle:
    """/file/upload: records each file; answers success unless told otherwise."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode() if b'filename="' in body else "?"
                fake.uploads.append((name, body))
                ok = fake.status == 200
                reply = json.dumps({"success": ok, "results": {"transid": "20261010-00001"} if ok else {},
                                    "message": "" if ok else "rejected"}).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v2"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


