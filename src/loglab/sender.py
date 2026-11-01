"""Sender Source: kirim outbox ke Monitor, cek receipt, retry, status (E-04).

Transport adalah callable post(path, payload, source_token) -> (status, body).
Tes memakai ReceiverStub.post; layanan nyata memakai HttpTransport (HTTPS).
"""
import fcntl
import json
import os
import random
import re
import signal
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from loglab.config import Settings
from loglab.db import connect
from loglab.events import validate_receipt, validate_source_status

EVENTS_PATH = "/api/v1/events"
STATUS_PATH = "/api/v1/source-status"

REQUEST_TIMEOUT = 3.0
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
JITTER_MAX = 0.2
BATCH_SIZE = 50

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

Transport = Callable[[str, dict, str], Tuple[int, Any]]


class SenderAlreadyRunning(RuntimeError):
    """Sudah ada sender aktif untuk database yang sama."""


def backoff_delay(attempts: int, rng: Callable[[], float] = random.random) -> float:
    """1, 2, 4, ... detik, maksimal 30, ditambah jitter 0-20%."""
    base = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** min(max(attempts, 1) - 1, 10)))
    return min(BACKOFF_MAX, base * (1 + JITTER_MAX * rng()))


def _reason(status: int, body: Any) -> str:
    """Alasan aman untuk inspeksi: tanpa isi body mentah."""
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and _SAFE_CODE.match(code):
        return f"http_{status}:{code}"
    return f"http_{status}"


class HttpTransport:
    """Transport HTTPS nyata. Verifikasi sertifikat selalu aktif."""

    def __init__(
        self,
        base_url: str = "",
        ca_bundle: Optional[Path] = None,
        timeout: float = REQUEST_TIMEOUT,
        client: Optional[httpx.Client] = None,
    ):
        if client is None:
            context = ssl.create_default_context(cafile=str(ca_bundle))
            client = httpx.Client(base_url=base_url, verify=context, timeout=timeout)
        self._client = client

    def __call__(self, path: str, payload: dict, source_token: str) -> Tuple[int, Any]:
        response = self._client.post(
            path, json=payload, headers={"Authorization": f"Bearer {source_token}"}
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body


class Sender:
    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        *,
        now: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ):
        if not settings.source_token:
            raise ValueError("source_token belum diatur")
        self.settings = settings
        self.transport = transport
        self.now = now
        self.rng = rng
        self.last_clock_offset_ms: Optional[int] = None
        self._lock_fd: Optional[int] = None

    # ---- satu sender aktif per database ----
    def __enter__(self) -> "Sender":
        lock = self.settings.sender_lock or Path(str(self.settings.db_path) + ".sender.lock")
        lock = Path(lock)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SenderAlreadyRunning("sender lain sudah aktif untuk database ini") from None
        self._lock_fd = fd
        return self

    def __exit__(self, *exc_info) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # menutup fd melepas flock
            self._lock_fd = None

    # ---- pengiriman event ----
    def run_once(self) -> Dict[str, int]:
        summary = {"acked": 0, "held": 0, "retry": 0}
        for row in self._pending():
            if row["next_attempt"] > self.now():
                break  # event terdepan menunggu backoff; urutan dijaga
            outcome = self._deliver(row)
            summary[outcome] += 1
            if outcome == "retry":
                break  # Monitor bermasalah; jangan menghujani
        return summary

    def _pending(self):
        with connect(self.settings.db_path) as conn:
            return conn.execute(
                "SELECT event_id, sequence, payload_json, attempts, next_attempt "
                "FROM outbox WHERE state='pending' ORDER BY sequence LIMIT ?",
                (BATCH_SIZE,),
            ).fetchall()

    def _deliver(self, row) -> str:
        event = json.loads(row["payload_json"])  # payload dari DB: sama tiap retry
        try:
            status, body = self.transport(EVENTS_PATH, event, self.settings.source_token)
        except (OSError, httpx.TransportError) as exc:
            timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException))
            return self._retry(row, "timeout" if timed_out else "transport_error")

        if status == 200:
            try:
                validate_receipt(body, event)
            except ValueError:
                # receipt tidak cocok = kegagalan, bukan ACK
                return self._retry(row, "receipt_mismatch")
            return self._ack(row, body["status"])
        if status == 429 or 500 <= status <= 599:
            return self._retry(row, _reason(status, body))
        if 400 <= status <= 499:
            return self._hold(row, _reason(status, body))
        return self._retry(row, f"unexpected_status_{status}")

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ack(self, row, receipt_status: str) -> str:
        with connect(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE outbox SET state='acked', attempts=attempts+1, receipt_status=?, "
                "acked_at=?, last_error=NULL WHERE event_id=? AND state='pending'",
                (receipt_status, self._stamp(), row["event_id"]),
            )
        return "acked"

    def _retry(self, row, reason: str) -> str:
        attempts = row["attempts"] + 1
        next_attempt = self.now() + backoff_delay(attempts, self.rng)
        with connect(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE outbox SET attempts=?, next_attempt=?, last_error=? "
                "WHERE event_id=? AND state='pending'",
                (attempts, next_attempt, reason, row["event_id"]),
            )
        return "retry"

    def _hold(self, row, reason: str) -> str:
        with connect(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE outbox SET state='held', attempts=attempts+1, last_error=? "
                "WHERE event_id=? AND state='pending'",
                (reason, row["event_id"]),
            )
        return "held"

    # ---- status sumber ----
    def build_status(self) -> Dict[str, Any]:
        """Status sumber. ACK watermark = sequence tertinggi yang semua
        pendahulunya sudah ACK; disimpan di meta agar tidak mundur."""
        with connect(self.settings.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                committed = int(meta["next_sequence"]) - 1
                watermark = int(meta.get("ack_watermark", "0"))
                for r in conn.execute(
                    "SELECT sequence FROM outbox WHERE state='acked' AND sequence > ? "
                    "ORDER BY sequence",
                    (watermark,),
                ):
                    if r["sequence"] != watermark + 1:
                        break
                    watermark += 1
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('ack_watermark', ?)",
                    (str(watermark),),
                )
                states = {
                    r[0]: r[1]
                    for r in conn.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state")
                }
                counters = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM counters")}
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "source_id": self.settings.source_id,
            "stream_id": meta["stream_id"],
            "committed_sequence": committed,
            "acknowledged_sequence": watermark,
            "pending_count": states.get("pending", 0),
            "held_count": states.get("held", 0),
            "rejected_count": counters.get("enqueue_rejected", 0),
            "offered_count": counters.get("offered", 0),
            "accepted_local_count": counters.get("accepted_local", 0),
            "source_time": self._stamp(),
        }

    def send_status(self) -> bool:
        """Kirim status. Status bukan event: gagal = coba lagi pada tick berikutnya."""
        status = self.build_status()
        validate_source_status(status)
        try:
            code, body = self.transport(STATUS_PATH, status, self.settings.source_token)
        except (OSError, httpx.TransportError):
            return False
        if code == 200 and isinstance(body, dict) and body.get("status") == "accepted":
            offset = body.get("clock_offset_ms")
            if isinstance(offset, int) and not isinstance(offset, bool):
                self.last_clock_offset_ms = offset
            return True
        return False

    def run_forever(
        self,
        stop: threading.Event,
        poll_interval: float = 0.5,
        status_interval: float = 5.0,
    ) -> None:
        last_status: Optional[float] = None
        while not stop.is_set():
            self.run_once()
            current = self.now()
            if last_status is None or current - last_status >= status_interval:
                self.send_status()
                last_status = current
            stop.wait(poll_interval)


def main() -> None:
    settings = Settings.from_env("source")
    transport = HttpTransport(base_url=settings.monitor_url, ca_bundle=settings.ca_bundle)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with Sender(settings, transport) as sender:
        sender.run_forever(stop)


if __name__ == "__main__":
    main()
