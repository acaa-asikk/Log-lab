import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from loglab.config import Settings
from loglab.db import connect, initialize_source
from loglab.events import build_event, validate_event
from loglab.security import hash_password, pseudonym, verify_password


class SourceService:
    """Keputusan auth + pencatatan event ke outbox (EP-01, EP-02, EP-05)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._dummy_hash: Optional[str] = None
        initialize_source(self.settings.db_path)

    def _get_dummy_hash(self) -> str:
        if self._dummy_hash is None:
            self._dummy_hash = hash_password(secrets.token_hex(16))
        return self._dummy_hash

    def _lookup_hash(self, username: str) -> Optional[str]:
        with connect(self.settings.db_path) as conn:
            row = conn.execute(
                "SELECT password_hash, active FROM auth_accounts WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None or not row["active"]:
            return None
        return row["password_hash"]

    def login(
        self, username: str, password: str, client_id: str = "client-uji-1"
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Cari akun di database, putuskan hasil auth, lalu catat event."""
        stored = self._lookup_hash(username)
        if stored is None:
            # akun tidak ada / nonaktif: tetap hitung hash agar waktunya mirip
            verify_password(password, self._get_dummy_hash())
            is_success = False
        else:
            is_success = verify_password(password, stored)
        return self._record(is_success, username, client_id)

    def process_login(
        self,
        username: str,
        password_attempt: str,
        stored_hash: str,
        client_id: str = "client-uji-1",
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Varian tanpa lookup DB: hash disuplai pemanggil (dipakai tes)."""
        is_success = verify_password(password_attempt, stored_hash)
        return self._record(is_success, username, client_id)

    def _record(
        self, is_success: bool, username: str, client_id: str
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Simpan event + sequence + counter dalam satu transaksi.

        Hasil auth tidak berubah walau penyimpanan gagal (EP-02).
        Jika enqueue gagal: event = None, sequence tidak terpakai,
        dan counter enqueue_rejected naik (EP-05).
        """
        key = self.settings.pseudonym_key
        if key is None:
            raise RuntimeError("pseudonym_key belum diatur untuk source")

        try:
            with connect(self.settings.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    stream_id = conn.execute(
                        "SELECT value FROM meta WHERE key='stream_id'"
                    ).fetchone()[0]
                    sequence = int(
                        conn.execute(
                            "SELECT value FROM meta WHERE key='next_sequence'"
                        ).fetchone()[0]
                    )
                    event = build_event(
                        source_id=self.settings.source_id,
                        stream_id=stream_id,
                        sequence=sequence,
                        occurred_at=datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        event_type="login.success" if is_success else "login.failure",
                        account_token=pseudonym(key, "account", username),
                        client_token=pseudonym(key, "client", client_id),
                        reason_code="SUCCESS" if is_success else "INVALID_CREDENTIALS",
                    )
                    validate_event(event)
                    conn.execute(
                        "INSERT INTO outbox "
                        "(event_id, stream_id, sequence, payload_json, state, created_at) "
                        "VALUES (?, ?, ?, ?, 'pending', ?)",
                        (
                            event["event_id"],
                            stream_id,
                            sequence,
                            json.dumps(event, sort_keys=True, separators=(",", ":")),
                            event["occurred_at"],
                        ),
                    )
                    conn.execute(
                        "UPDATE meta SET value=? WHERE key='next_sequence'",
                        (str(sequence + 1),),
                    )
                    conn.execute(
                        "UPDATE counters SET value = value + 1 WHERE key='offered'"
                    )
                    conn.execute(
                        "UPDATE counters SET value = value + 1 WHERE key='accepted_local'"
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except sqlite3.Error:
            self._count_enqueue_rejected()
            return is_success, None

        return is_success, event

    def _count_enqueue_rejected(self) -> None:
        try:
            with connect(self.settings.db_path) as conn:
                conn.execute("UPDATE counters SET value = value + 1 WHERE key='offered'")
                conn.execute(
                    "UPDATE counters SET value = value + 1 WHERE key='enqueue_rejected'"
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def count_request_rejected(self) -> None:
        """Request ditolak sebelum auth dijalankan: tidak masuk 'offered'."""
        try:
            with connect(self.settings.db_path) as conn:
                conn.execute(
                    "UPDATE counters SET value = value + 1 WHERE key='request_rejected'"
                )
                conn.commit()
        except sqlite3.Error:
            pass
