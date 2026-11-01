"""Wiring entrypoint Source: aplikasi login + satu sender di proses yang sama."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

import loglab.source_main as source_main
from loglab.config import Settings
from loglab.contract import ContractError
from loglab.db import connect
from loglab.sender import Sender
from loglab.seed_source_accounts import seed_accounts
from loglab.testing import ReceiverStub

PASSWORD = "a-long-fictitious-password"


class TestSourceMain(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings.for_test("source", Path(tmp.name))
        self.stub = ReceiverStub(self.settings.source_token)
        seed_accounts(self.settings.db_path, {"demo-user-01": PASSWORD})

    def state(self, sequence):
        with connect(self.settings.db_path) as conn:
            row = conn.execute(
                "SELECT state FROM outbox WHERE sequence = ?", (sequence,)
            ).fetchone()
        return row["state"] if row else None

    def wait_for(self, sequence, state, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state(sequence) == state:
                return True
            time.sleep(0.02)
        return False

    def login(self, client):
        return client.post("/login", data={"username": "demo-user-01", "password": PASSWORD})

    def test_import_tidak_membangun_app(self):
        self.assertIsNone(source_main._app)

    def test_app_tanpa_env_ditolak_saat_diminta(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ContractError):
                source_main.app  # noqa: B018 (akses memicu build_app)

    def test_sender_berjalan_bersama_aplikasi(self):
        app = source_main.build_app(self.settings, transport=self.stub.post, poll_interval=0.02)
        with TestClient(app) as client:
            self.assertEqual(self.login(client).status_code, 200)
            self.assertTrue(self.wait_for(1, "acked"))
        self.assertEqual(len(self.stub.events), 1)
        with Sender(self.settings, self.stub.post):  # kunci dilepas saat shutdown
            pass

    def test_event_menunggu_di_outbox_bila_monitor_mati(self):
        self.stub.available = False
        app = source_main.build_app(self.settings, transport=self.stub.post, poll_interval=0.02)
        with TestClient(app) as client:
            self.assertEqual(self.login(client).status_code, 200)  # login tetap menjawab
            self.assertTrue(self.wait_for(1, "pending"))
            time.sleep(0.2)
            self.assertEqual(self.state(1), "pending")
            self.stub.available = True
            with connect(self.settings.db_path) as conn:
                conn.execute("UPDATE outbox SET next_attempt = 0")  # lewati jeda backoff
            self.assertTrue(self.wait_for(1, "acked"))

    def test_sender_kedua_tidak_dijalankan(self):
        with Sender(self.settings, self.stub.post):  # kunci dipegang pihak lain
            app = source_main.build_app(
                self.settings, transport=self.stub.post, poll_interval=0.02
            )
            with TestClient(app) as client:
                self.assertEqual(self.login(client).status_code, 200)
                time.sleep(0.4)
                self.assertEqual(self.state(1), "pending")
        self.assertEqual(len(self.stub.events), 0)


if __name__ == "__main__":
    unittest.main()
