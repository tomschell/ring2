"""The Ring bus server: retained messages per channel over HTTP, in one SQLite file.

  python3 -m ring2.busd --port 8770 --db FILE

  POST /channels/<name>  {"message": "one line"}   -> 201 {"message": {sequence, channel, message, created_at}}
  GET  /events?channel=<name>&after=<n>&wait=<s>   -> {"messages": [...]}  long poll, at most 30 s
  GET  /messages?channel=<name>                    -> {"messages": [...]}  everything retained
  GET  /listeners                                  -> {"listeners": {label: {read, age}}}
  GET  /capabilities                               -> protocol and size limits

A message is one non-empty line of at most 4096 UTF-8 bytes. Each channel
keeps its newest 64 messages; sequences are global and only grow.

A fetch on /events may add `as=<label>&read=<n>`: the bus remembers, in memory
only, the latest position each label reported and when it last heard from it.
`age` is in seconds on the bus's own clock.

Extracted from the Ring repository (ring/ring.py at commit aa06bacd): the
store, the app, the HTTP handler and the server entry point, unchanged apart
from dropping the `snapshot=1` query form, a new default database path, and
leaving out the client and CLI code that shared the file.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import stat
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence


RING_SIZE = 64
DEFAULT_DB = Path("~/.local/share/ring2/bus.db").expanduser()
MAX_REQUEST_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 4096
RING_PROTOCOL_VERSION = 1


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def validate_channel(channel: str) -> str:
    if (
        not channel
        or channel.strip() != channel
        or channel.splitlines() != [channel]
    ):
        raise ValueError("channel must be a non-empty one-line value without surrounding whitespace")
    return channel


def validate_message_line(message: str) -> str:
    if not message or message.splitlines() != [message]:
        raise ValueError("message must be one non-empty line")
    return message


def validate_message(message: str) -> str:
    validate_message_line(message)
    size = len(message.encode("utf-8"))
    if size > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"message is {size} UTF-8 bytes, {size - MAX_MESSAGE_BYTES} over the "
            f"{MAX_MESSAGE_BYTES}-byte ceiling, measured as posted including the sender "
            "prefix; put detail in Beads, Git, or a document and post its locator"
        )
    return message


class RingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = None
        self._initialize()
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise sqlite3.OperationalError('Ring database identity unavailable')
        self.identity = (info.st_dev, info.st_ino)

    def _owned(self):
        if self.identity is None:
            return  # Initial creation has no earlier source binding.
        try:
            info = self.path.stat()
        except OSError as exc:
            raise sqlite3.OperationalError('Ring database identity changed') from exc
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
            raise sqlite3.OperationalError('Ring database identity changed')

    @contextlib.contextmanager
    def _connect(self):
        # sqlite3's own context manager only commits or rolls back; the
        # connection itself must still be closed explicitly.
        self._owned()
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                self._owned()
                yield connection
                self._owned()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS messages_channel_sequence "
                "ON messages(channel, sequence)"
            )

    @staticmethod
    def _rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def post(self, channel: str, message: str) -> dict[str, Any]:
        channel = validate_channel(channel)
        message = validate_message(message)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(channel, message, created_at) VALUES (?, ?, ?)",
                (channel, message, now_utc()),
            )
            sequence = int(cursor.lastrowid)
            connection.execute(
                """
                DELETE FROM messages
                WHERE channel = ? AND sequence NOT IN (
                    SELECT sequence FROM messages
                    WHERE channel = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                )
                """,
                (channel, channel, RING_SIZE),
            )
            row = connection.execute(
                "SELECT sequence, channel, message, created_at "
                "FROM messages WHERE sequence = ?",
                (sequence,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def retained(self, channels: Sequence[str]) -> list[dict[str, Any]]:
        normalized = [validate_channel(channel) for channel in channels]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, channel, message, created_at FROM messages "
                f"WHERE channel IN ({placeholders}) ORDER BY sequence",
                normalized,
            ).fetchall()
        return self._rows(rows)

    def after(self, channels: Sequence[str], sequence: int) -> list[dict[str, Any]]:
        normalized = [validate_channel(channel) for channel in channels]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, channel, message, created_at FROM messages "
                f"WHERE sequence > ? AND channel IN ({placeholders}) "
                "ORDER BY sequence",
                [sequence, *normalized],
            ).fetchall()
        return self._rows(rows)


class RingApp:
    def __init__(self, path: Path) -> None:
        self.store = RingStore(path)
        self.changed = threading.Condition()
        self.clock = time.monotonic
        self.heard: dict[str, tuple[int, float]] = {}

    def hear(self, label: str, read: int) -> None:
        with self.changed:
            self.heard[label] = (read, self.clock())

    def listeners(self) -> dict[str, dict[str, Any]]:
        with self.changed:
            now = self.clock()
            return {label: {"read": read, "age": max(0.0, now - at)} for label, (read, at) in self.heard.items()}

    def post(self, channel: str, message: str) -> dict[str, Any]:
        with self.changed:
            result = self.store.post(channel, message)
            self.changed.notify_all()
            return result

    def wait(self, channels: Sequence[str], after: int, timeout: float):
        deadline = time.monotonic() + max(0.0, min(timeout, 30.0))
        with self.changed:
            while True:
                messages = self.store.after(channels, after)
                if messages:
                    return messages
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self.changed.wait(remaining)


LOOPBACK_AUTHORITY = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?", re.IGNORECASE)


class RingHandler(BaseHTTPRequestHandler):
    server_version = "Ring/0.1"
    # A peer that opens a connection and never finishes its request, or
    # announces a longer body than it sends, would otherwise hold a handler
    # thread forever. Long-poll waiting happens on the app condition, not on
    # the socket, so this bounds only the request read and the response write.
    timeout = 60

    @property
    def app(self) -> RingApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, {"error": message})

    def _local_only(self) -> bool:
        """Refuse a request whose Host is not loopback when the bus binds only to loopback."""
        if not self.server.local_only:  # type: ignore[attr-defined]
            return True
        if LOOPBACK_AUTHORITY.fullmatch(self.headers.get("Host", "").strip()):
            return True
        self._error(HTTPStatus.FORBIDDEN, "host not allowed")
        return False

    def do_POST(self) -> None:
        if not self._local_only():
            return
        if self.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content type must be application/json")
            return
        parsed = urllib.parse.urlparse(self.path)
        prefix = "/channels/"
        if not parsed.path.startswith(prefix) or parsed.path == prefix:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
                raise ValueError("message must be a string")
            channel = urllib.parse.unquote(parsed.path[len(prefix) :])
            result = self.app.post(channel, payload["message"])
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except sqlite3.Error:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Ring storage unavailable")
            return
        self._send(HTTPStatus.CREATED, {"message": result})

    def do_GET(self) -> None:
        if not self._local_only():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/capabilities' and not parsed.query:
            self._send(HTTPStatus.OK, {
                'protocol_version': RING_PROTOCOL_VERSION,
                'server_version': self.server_version,
                'max_message_bytes': MAX_MESSAGE_BYTES,
            })
            return
        query = urllib.parse.parse_qs(parsed.query)
        channels = query.get("channel", [])
        try:
            if 'snapshot' in urllib.parse.parse_qs(parsed.query, keep_blank_values=True):
                raise ValueError('snapshot queries are not supported by this server')
            if parsed.path == "/messages":
                result = self.app.store.retained(channels)
            elif parsed.path == "/listeners":
                self._send(HTTPStatus.OK, {"listeners": self.app.listeners()})
                return
            elif parsed.path == "/events":
                after = int(query.get("after", ["0"])[0])
                timeout = float(query.get("wait", ["30"])[0])
                if after < 0:
                    raise ValueError("after must be nonnegative")
                if query.get("as"):
                    read = int(query.get("read", [after])[0])
                    if read < 0:
                        raise ValueError("read must be nonnegative")
                    self.app.hear(query["as"][0], read)
                result = self.app.wait(channels, after, timeout)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except sqlite3.Error:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Ring storage unavailable")
            return
        self._send(HTTPStatus.OK, {"messages": result})


class RingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: RingApp) -> None:
        super().__init__(address, RingHandler)
        self.local_only = address[0] in ("127.0.0.1", "localhost", "::1")
        self.app = app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Ring bus server")
    parser.add_argument("--host", default=os.environ.get("RING_HOST", "127.0.0.1"),
                        help="interface to bind; defaults to RING_HOST, then loopback only")
    # argparse converts a string default with `type`, so a bad RING_PORT is an
    # argument error rather than a traceback before --help can even print.
    parser.add_argument("--port", type=int, default=os.environ.get("RING_PORT", "8770"),
                        help="TCP port to listen on; defaults to RING_PORT, then 8770")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("RING_DB", DEFAULT_DB)),
                        help="SQLite file for retained messages; defaults to RING_DB, then the\n"
                             "standard data path; created if absent")

    args = parser.parse_args(argv)
    server = RingServer((args.host, args.port), RingApp(args.db.expanduser()))
    print(f"ring bus listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
