"""Tes sender E-04. Receiver yang dipakai adalah ReceiverStub (B asli belum terlibat)."""
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import httpx

from loglab.config import Settings
from loglab.db import connect
from loglab.events import build_receipt, validate_source_status
from loglab.security import hash_password
from loglab.sender import (
    BACKOFF_MAX,
    HttpTransport,
    Sender,
    SenderAlreadyRunning,
    backoff_delay,
)
from loglab.source_service import SourceService
from loglab.testing import ReceiverStub

PASSWORD = "a-long-fictitious-password"
WRONG = "wrong-password-123"
EVENTS = "/api/v1/events"
STATUS = "/api/v1/source-status"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Crash(BaseException):
    """Mensimulasikan proses mati mendadak (tidak ditangkap sender)."""


class SenderTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings.for_test("source", Path(tmp.name))
        self.service = SourceService(self.settings)
        self.stub = ReceiverStub(self.settings.source_token)
        self.clock = Clock()
        self._hashed = hash_password(PASSWORD)

    def add_events(self, n):
        return [
            self.service.process_login(
                "user-a", PASSWORD if i % 2 == 0 else WRONG, self._hashed
            )[1]
            for i in range(n)
        ]

    def sender(self, transport=None, rng=lambda: 0.0):
        return Sender(self.settings, transport or self.stub.post, now=self.clock, rng=rng)

    def jalankan(self, transport=None, rng=lambda: 0.0):
        with self.sender(transport, rng) as s:
            return s.run_once()

    def row(self, sequence):
        with connect(self.settings.db_path) as conn:
            return conn.execute(
                "SELECT * FROM outbox WHERE sequence = ?", (sequence,)
            ).fetchone()


class TestSender(SenderTestCase):
    def test_e4_01_receipt_cocok_disimpan_sebagai_ack(self):
        [event] = self.add_events(1)
        summary = self.jalankan()
        self.assertEqual(summary, {"acked": 1, "held": 0, "retry": 0})
        row = self.row(1)
        self.assertEqual(row["state"], "acked")
        self.assertEqual(row["receipt_status"], "stored")
        self.assertIsNotNone(row["acked_at"])
        self.assertEqual(len(self.stub.events), 1)
        self.assertEqual(json.loads(row["payload_json"]), event)

    def test_e4_02_receipt_tidak_cocok_bukan_ack(self):
        self.add_events(1)
        salah = {
            "source_id": "sumber-lain",
            "event_id": str(uuid.uuid4()),
            "event_digest": "0" * 64,
        }
        for field, nilai in salah.items():
            with self.subTest(field=field):
                def transport(path, payload, token, field=field, nilai=nilai):
                    status, body = self.stub.post(path, payload, token)
                    return status, {**body, field: nilai}

                self.jalankan(transport)
                row = self.row(1)
                self.assertEqual(row["state"], "pending")
                self.assertEqual(row["last_error"], "receipt_mismatch")
                self.clock.advance(60)

    def test_e4_03_backoff_1_2_4_sampai_30_payload_tetap(self):
        self.add_events(1)
        payloads = []

        def mati(path, payload, token):
            payloads.append(json.dumps(payload, sort_keys=True))
            raise TimeoutError("receiver tidak menjawab")

        sebelum = self.row(1)["payload_json"]
        for expected in (1, 2, 4, 8, 16, 30, 30):
            start = self.clock()
            self.assertEqual(self.jalankan(mati)["retry"], 1)
            self.assertAlmostEqual(self.row(1)["next_attempt"] - start, expected)
            terkirim = len(payloads)
            self.assertEqual(self.jalankan(mati)["retry"], 0)  # belum jatuh tempo
            self.assertEqual(len(payloads), terkirim)
            self.clock.advance(expected)
        self.assertEqual(len(set(payloads)), 1)
        self.assertEqual(self.row(1)["payload_json"], sebelum)
        self.assertEqual(self.row(1)["state"], "pending")
        self.assertEqual(self.row(1)["attempts"], 7)
        self.assertEqual(self.row(1)["last_error"], "timeout")

    def test_e4_03_jitter_0_sampai_20_persen_dan_batas_30(self):
        self.assertAlmostEqual(backoff_delay(1, lambda: 0.0), 1.0)
        self.assertAlmostEqual(backoff_delay(1, lambda: 0.5), 1.1)
        self.assertAlmostEqual(backoff_delay(1, lambda: 1.0), 1.2)
        self.assertAlmostEqual(backoff_delay(3, lambda: 1.0), 4.8)
        self.assertEqual(backoff_delay(20, lambda: 1.0), BACKOFF_MAX)
        self.add_events(1)

        def mati(path, payload, token):
            raise TimeoutError("x")

        start = self.clock()
        self.jalankan(mati, rng=lambda: 1.0)
        self.assertAlmostEqual(self.row(1)["next_attempt"] - start, 1.2)

    def test_e4_03_429_dan_5xx_dicoba_ulang(self):
        self.add_events(1)
        for code in (429, 500, 503):
            with self.subTest(code=code):
                self.assertEqual(self.jalankan(lambda p, b, t, c=code: (c, {}))["retry"], 1)
                row = self.row(1)
                self.assertEqual(row["state"], "pending")
                self.assertEqual(row["last_error"], f"http_{code}")
                self.clock.advance(60)

    def test_respons_permanen_ditahan_dengan_alasan_aman(self):
        self.add_events(5)
        kode = [400, 401, 409, 413, 422]
        urutan = iter(kode)
        summary = self.jalankan(lambda p, b, t: (next(urutan), {"error": "contoh_alasan"}))
        self.assertEqual(summary["held"], 5)
        for seq, code in enumerate(kode, start=1):
            row = self.row(seq)
            self.assertEqual(row["state"], "held")  # ditahan, tidak dihapus
            self.assertEqual(row["last_error"], f"http_{code}:contoh_alasan")

    def test_alasan_tidak_aman_tidak_disimpan(self):
        self.add_events(1)
        self.jalankan(lambda p, b, t: (422, {"error": "password=rahasia!"}))
        row = self.row(1)
        self.assertEqual(row["last_error"], "http_422")
        self.assertNotIn("rahasia", row["last_error"])

    def test_e4_04_receipt_hilang_lalu_duplicate_sah(self):
        self.add_events(1)

        def hilang(path, payload, token):
            self.stub.post(path, payload, token)
            raise TimeoutError("receipt hilang di jalan")

        self.jalankan(hilang)
        self.assertEqual(self.row(1)["state"], "pending")
        self.clock.advance(60)
        self.jalankan()
        row = self.row(1)
        self.assertEqual(row["state"], "acked")
        self.assertEqual(row["receipt_status"], "duplicate")
        self.assertEqual(len(self.stub.events), 1)

    def test_e4_05_berhenti_sebelum_ack_dipulihkan(self):
        self.add_events(1)

        def mati_setelah_kirim(path, payload, token):
            self.stub.post(path, payload, token)
            raise Crash()

        with self.assertRaises(Crash):
            self.jalankan(mati_setelah_kirim)
        self.assertEqual(self.row(1)["state"], "pending")
        self.jalankan()  # proses baru; kunci sudah lepas
        row = self.row(1)
        self.assertEqual(row["state"], "acked")
        self.assertEqual(row["receipt_status"], "duplicate")
        self.assertEqual(len(self.stub.events), 1)

    def test_e4_05_berhenti_sesudah_ack_tidak_kirim_ulang(self):
        self.add_events(1)
        self.jalankan()
        panggilan = []

        def hitung(path, payload, token):
            panggilan.append(path)
            return self.stub.post(path, payload, token)

        self.assertEqual(self.jalankan(hitung), {"acked": 0, "held": 0, "retry": 0})
        self.assertEqual(panggilan, [])
        self.assertEqual(self.row(1)["state"], "acked")

    def test_hanya_satu_sender_aktif(self):
        with self.sender():
            with self.assertRaises(SenderAlreadyRunning):
                with self.sender():
                    pass
        with self.sender():  # setelah dilepas, sender baru boleh
            pass

    def test_e4_06_watermark_kontigu_tidak_meloncat(self):
        self.add_events(3)

        def tolak_kedua(path, payload, token):
            if payload["sequence"] == 2:
                return 409, {"error": "event_conflict"}
            return self.stub.post(path, payload, token)

        self.assertEqual(self.jalankan(tolak_kedua), {"acked": 2, "held": 1, "retry": 0})
        status = self.sender().build_status()
        validate_source_status(status)
        self.assertEqual(status["committed_sequence"], 3)
        self.assertEqual(status["acknowledged_sequence"], 1)  # 3 sudah ACK, tapi belum meloncat
        self.assertEqual(status["pending_count"], 0)
        self.assertEqual(status["held_count"], 1)
        self.assertEqual(status["offered_count"], 3)
        self.assertEqual(status["accepted_local_count"], 3)
        self.assertEqual(status["rejected_count"], 0)

        # penyebab diperbaiki, operator mengembalikan event ke antrean
        with connect(self.settings.db_path) as conn:
            conn.execute("UPDATE outbox SET state='pending', last_error=NULL WHERE sequence=2")
        self.jalankan()
        status = self.sender().build_status()
        self.assertEqual(status["acknowledged_sequence"], 3)
        self.assertEqual(status["held_count"], 0)

    def test_watermark_tidak_mundur_setelah_cleanup(self):
        self.add_events(3)
        self.jalankan()
        self.assertEqual(self.sender().build_status()["acknowledged_sequence"], 3)
        with connect(self.settings.db_path) as conn:
            conn.execute("DELETE FROM outbox")
        status = self.sender().build_status()
        self.assertEqual(status["acknowledged_sequence"], 3)
        self.assertEqual(status["committed_sequence"], 3)

    def test_status_kosong_watermark_nol_sah(self):
        status = self.sender().build_status()
        validate_source_status(status)
        self.assertEqual(status["committed_sequence"], 0)
        self.assertEqual(status["acknowledged_sequence"], 0)

    def test_status_terkirim_ke_receiver(self):
        self.add_events(2)
        self.jalankan()
        with self.sender() as s:
            self.assertTrue(s.send_status())
            self.assertIsInstance(s.last_clock_offset_ms, int)
        tersimpan = self.stub.statuses[(self.settings.source_id, self.row(1)["stream_id"])]
        self.assertEqual(tersimpan["acknowledged_sequence"], 2)
        self.assertEqual(tersimpan["committed_sequence"], 2)

    def test_status_gagal_bila_receiver_mati(self):
        self.stub.available = False
        with self.sender() as s:
            self.assertFalse(s.send_status())

    def test_run_forever_mengirim_event_dan_status(self):
        self.add_events(2)
        stop = threading.Event()

        def transport(path, payload, token):
            hasil = self.stub.post(path, payload, token)
            if path == STATUS:
                stop.set()
            return hasil

        with self.sender(transport) as s:
            s.run_forever(stop, poll_interval=0.01)
        self.assertEqual(self.row(1)["state"], "acked")
        self.assertEqual(self.row(2)["state"], "acked")
        self.assertEqual(len(self.stub.statuses), 1)


class TestHttpTransport(SenderTestCase):
    @staticmethod
    def client(handler):
        return httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://monitor.test"
        )

    def test_transport_mengirim_bearer_dan_membaca_receipt(self):
        [event] = self.add_events(1)
        seen = {}

        def handler(request):
            seen["auth"] = request.headers["authorization"]
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=build_receipt(seen["body"]))

        transport = HttpTransport(client=self.client(handler))
        status, body = transport(EVENTS, event, "token-abc")
        self.assertEqual(status, 200)
        self.assertEqual(seen["auth"], "Bearer token-abc")
        self.assertEqual(seen["path"], EVENTS)
        self.assertEqual(seen["body"], event)
        self.assertEqual(body, build_receipt(event))

    def test_transport_timeout_menjadi_retry(self):
        self.add_events(1)

        def handler(request):
            raise httpx.ConnectTimeout("timeout", request=request)

        self.assertEqual(self.jalankan(HttpTransport(client=self.client(handler)))["retry"], 1)
        row = self.row(1)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["last_error"], "timeout")

    def test_transport_respons_bukan_json(self):
        self.add_events(1)

        def handler(request):
            return httpx.Response(502, text="<html>bad gateway</html>")

        transport = HttpTransport(client=self.client(handler))
        self.assertEqual(self.jalankan(transport)["retry"], 1)
        self.assertEqual(self.row(1)["last_error"], "http_502")


if __name__ == "__main__":
    unittest.main()
