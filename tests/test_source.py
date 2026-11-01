import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from loglab.config import Settings
from loglab.db import connect
from loglab.security import hash_password
from loglab.seed_source_accounts import (
    load_accounts_file,
    main as seed_main,
    seed_accounts,
)
from loglab.source_app import create_app
from loglab.source_service import SourceService

PASSWORD = "a-long-fictitious-password"
WRONG = "wrong-password-123"


def make_service(tmp_dir):
    settings = Settings.for_test("source", Path(tmp_dir))
    return settings, SourceService(settings)


def read_counters(db_path):
    with connect(db_path) as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM counters")}


def outbox_rows(db_path):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM outbox ORDER BY sequence").fetchall()


class TestSourceService(unittest.TestCase):
    def test_e3_01_login_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, service = make_service(tmp_dir)
            success, event = service.process_login(
                "user_erna", PASSWORD, hash_password(PASSWORD)
            )
            self.assertTrue(success)
            self.assertEqual(event["event_type"], "login.success")
            self.assertEqual(event["reason_code"], "SUCCESS")
            self.assertEqual(event["sequence"], 1)
            rows = outbox_rows(settings.db_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "pending")
            self.assertEqual(json.loads(rows[0]["payload_json"]), event)

    def test_e3_02_login_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, service = make_service(tmp_dir)
            success, event = service.process_login(
                "user_erna", WRONG, hash_password(PASSWORD)
            )
            self.assertFalse(success)
            self.assertEqual(event["event_type"], "login.failure")
            self.assertEqual(event["reason_code"], "INVALID_CREDENTIALS")
            self.assertEqual(outbox_rows(settings.db_path)[0]["state"], "pending")

    def test_event_tanpa_secret_atau_nama_asli(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, service = make_service(tmp_dir)
            _, event = service.process_login(
                "user_erna", PASSWORD, hash_password(PASSWORD)
            )
            text = json.dumps(event)
            self.assertNotIn("user_erna", text)
            self.assertNotIn(PASSWORD, text)
            self.assertRegex(event["account_token"], r"^[0-9a-f]{64}$")
            self.assertRegex(event["client_token"], r"^[0-9a-f]{64}$")

    def test_sequence_dan_counter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, service = make_service(tmp_dir)
            hashed = hash_password(PASSWORD)
            _, e1 = service.process_login("user_erna", PASSWORD, hashed)
            _, e2 = service.process_login("user_erna", WRONG, hashed)
            self.assertEqual([e1["sequence"], e2["sequence"]], [1, 2])
            c = read_counters(settings.db_path)
            self.assertEqual(c["offered"], 2)
            self.assertEqual(c["accepted_local"], 2)
            self.assertEqual(c["enqueue_rejected"], 0)

    def test_login_dari_database(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, service = make_service(tmp_dir)
            seed_accounts(settings.db_path, {"akun-uji-1": PASSWORD, "akun-uji-2": PASSWORD})
            self.assertTrue(service.login("akun-uji-1", PASSWORD)[0])
            self.assertTrue(service.login("AKUN-UJI-1", PASSWORD)[0])  # NOCASE
            self.assertFalse(service.login("akun-uji-1", WRONG)[0])
            ok, event = service.login("tidak-ada", PASSWORD)
            self.assertFalse(ok)
            self.assertEqual(event["event_type"], "login.failure")
            with connect(settings.db_path) as conn:
                conn.execute("UPDATE auth_accounts SET active = 0 WHERE username = 'akun-uji-2'")
                conn.commit()
            self.assertFalse(service.login("akun-uji-2", PASSWORD)[0])

    def test_e3_03_enqueue_gagal_tidak_mengubah_hasil_auth(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, service = make_service(tmp_dir)
            with connect(settings.db_path) as conn:
                conn.execute("DROP TABLE outbox")
                conn.commit()
            success, event = service.process_login(
                "user_erna", PASSWORD, hash_password(PASSWORD)
            )
            self.assertTrue(success)      # hasil auth tetap benar
            self.assertIsNone(event)      # tidak ada event setengah jadi
            c = read_counters(settings.db_path)
            self.assertEqual(c["offered"], 1)
            self.assertEqual(c["accepted_local"], 0)
            self.assertEqual(c["enqueue_rejected"], 1)
            self.assertEqual(c["offered"], c["accepted_local"] + c["enqueue_rejected"])
            with connect(settings.db_path) as conn:
                seq = conn.execute(
                    "SELECT value FROM meta WHERE key='next_sequence'"
                ).fetchone()[0]
            self.assertEqual(seq, "1")    # sequence tidak terpakai

    def test_e3_04_event_bertahan_setelah_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, service = make_service(tmp_dir)
            _, first = service.process_login(
                "user_erna", PASSWORD, hash_password(PASSWORD)
            )
            restarted = SourceService(settings)  # simulasi proses mulai ulang
            rows = outbox_rows(settings.db_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_id"], first["event_id"])
            self.assertEqual(json.loads(rows[0]["payload_json"]), first)
            _, second = restarted.process_login("user_erna", WRONG, hash_password(PASSWORD))
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(second["stream_id"], first["stream_id"])

    def test_e3_05_seed_dua_kali_aman(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings, _ = make_service(tmp_dir)
            accounts = {"akun-uji-1": PASSWORD, "akun-uji-2": PASSWORD}
            self.assertEqual(seed_accounts(settings.db_path, accounts), 2)
            with connect(settings.db_path) as conn:
                before = conn.execute(
                    "SELECT username, password_hash FROM auth_accounts ORDER BY username"
                ).fetchall()
                before = [tuple(r) for r in before]
            # kedua kali: password beda tidak boleh menimpa yang sudah ada
            self.assertEqual(
                seed_accounts(settings.db_path, {u: "password-lain-12345" for u in accounts}), 0
            )
            with connect(settings.db_path) as conn:
                after = [
                    tuple(r)
                    for r in conn.execute(
                        "SELECT username, password_hash FROM auth_accounts ORDER BY username"
                    ).fetchall()
                ]
            self.assertEqual(before, after)
            with self.assertRaises(ValueError):
                seed_accounts(settings.db_path, {"akun-baru": "pendek"})


class TestSourceRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings.for_test("source", Path(self._tmp.name))
        self.app = create_app(self.settings)
        seed_accounts(
            self.settings.db_path,
            {"demo-user-01": PASSWORD, "<i>akun</i>": PASSWORD},
        )
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        self._tmp.cleanup()

    def last_event(self):
        return json.loads(outbox_rows(self.settings.db_path)[-1]["payload_json"])

    async def test_halaman_login(self):
        r = await self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<form", r.text)

    async def test_root_mengarah_ke_login(self):
        r = await self.client.get("/")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    async def test_health_sesuai_kontrak(self):
        r = await self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json(),
            {
                "service": "loglab-source",
                "state": "ready",
                "contract_version": self.settings.contract_version,
            },
        )

    async def test_form_berhasil_mencatat_event(self):
        r = await self.client.post("/login", data={"username": "demo-user-01", "password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Login berhasil", r.text)
        self.assertEqual(self.last_event()["event_type"], "login.success")

    async def test_form_salah_mencatat_failure(self):
        r = await self.client.post("/login", data={"username": "demo-user-01", "password": WRONG})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.last_event()["event_type"], "login.failure")

    async def test_akun_tidak_ada_pesannya_sama(self):
        wrong = await self.client.post("/login", data={"username": "demo-user-01", "password": WRONG})
        unknown = await self.client.post("/login", data={"username": "tidak-ada", "password": WRONG})
        self.assertEqual(unknown.status_code, 401)
        self.assertIn("Username atau password salah", unknown.text)
        self.assertIn("Username atau password salah", wrong.text)

    async def test_username_di_escape(self):
        r = await self.client.post("/login", data={"username": "<i>akun</i>", "password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertIn("&lt;i&gt;akun&lt;/i&gt;", r.text)
        self.assertNotIn("<i>akun</i>", r.text)

    async def test_form_kosong_ditolak_sebelum_auth(self):
        r = await self.client.post("/login", data={"username": "", "password": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(outbox_rows(self.settings.db_path), [])
        c = read_counters(self.settings.db_path)
        self.assertEqual(c["request_rejected"], 1)
        self.assertEqual(c["offered"], 0)

    async def test_event_tanpa_nama_password_atau_ip(self):
        await self.client.post("/login", data={"username": "demo-user-01", "password": PASSWORD})
        payload = outbox_rows(self.settings.db_path)[0]["payload_json"]
        for rahasia in ("demo-user-01", PASSWORD, "127.0.0.1"):
            self.assertNotIn(rahasia, payload)

    async def test_api_login_json(self):
        ok = await self.client.post(
            "/api/v1/login", json={"username": "demo-user-01", "password": PASSWORD}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"result": "success"})
        self.assertEqual(self.last_event()["event_type"], "login.success")
        salah = await self.client.post(
            "/api/v1/login", json={"username": "demo-user-01", "password": WRONG}
        )
        self.assertEqual(salah.status_code, 401)
        self.assertEqual(salah.json(), {"result": "failure"})
        self.assertEqual(self.last_event()["event_type"], "login.failure")

    async def test_api_login_permintaan_tidak_valid(self):
        kasus = [
            ("bukan json", b"{rusak"),
            ("bukan objek", b"[1, 2]"),
            ("field hilang", json.dumps({"username": "demo-user-01"}).encode()),
            ("tipe salah", json.dumps({"username": 7, "password": PASSWORD}).encode()),
            ("terlalu besar", json.dumps({"username": "x", "password": "y" * 5000}).encode()),
        ]
        for nama, body in kasus:
            with self.subTest(nama):
                r = await self.client.post(
                    "/api/v1/login", content=body, headers={"content-type": "application/json"}
                )
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json(), {"error": "invalid_request"})
        self.assertEqual(outbox_rows(self.settings.db_path), [])
        c = read_counters(self.settings.db_path)
        self.assertEqual(c["request_rejected"], len(kasus))
        self.assertEqual(c["offered"], 0)

    async def test_form_dan_api_menghasilkan_event_berformat_sama(self):
        await self.client.post("/login", data={"username": "demo-user-01", "password": PASSWORD})
        form_event = self.last_event()
        await self.client.post(
            "/api/v1/login", json={"username": "demo-user-01", "password": PASSWORD}
        )
        api_event = self.last_event()
        self.assertEqual(set(form_event), set(api_event))
        self.assertEqual(form_event["account_token"], api_event["account_token"])
        self.assertEqual(form_event["event_type"], api_event["event_type"])


class TestSeedFile(unittest.TestCase):
    def write(self, tmp_dir, data, mode=0o600):
        path = Path(tmp_dir) / "test-accounts.json"
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        os.chmod(path, mode)
        return path

    def test_membaca_daftar_akun(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self.write(tmp_dir, [
                {"username": "demo-user-01", "password": PASSWORD},
                {"username": "demo-user-02", "password": PASSWORD},
            ])
            self.assertEqual(
                load_accounts_file(path),
                {"demo-user-01": PASSWORD, "demo-user-02": PASSWORD},
            )

    def test_struktur_salah_ditolak_tanpa_membocorkan_password(self):
        kasus = {
            "bukan json": "{rusak",
            "bukan array": {"username": "a"},
            "array kosong": [],
            "field berlebih": [{"username": "a", "password": PASSWORD, "peran": "x"}],
            "tipe salah": [{"username": "a", "password": 123456789012}],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            for nama, data in kasus.items():
                with self.subTest(nama):
                    path = self.write(tmp_dir, data)
                    with self.assertRaises(ValueError) as ctx:
                        load_accounts_file(path)
                    self.assertNotIn(PASSWORD, str(ctx.exception))

    def test_username_ganda_ditolak(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self.write(tmp_dir, [
                {"username": "Demo-User-01", "password": PASSWORD},
                {"username": "demo-user-01", "password": PASSWORD},
            ])
            with self.assertRaises(ValueError):
                load_accounts_file(path)

    def test_izin_file_longgar_ditolak(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self.write(
                tmp_dir, [{"username": "demo-user-01", "password": PASSWORD}], mode=0o644
            )
            with self.assertRaises(ValueError):
                load_accounts_file(path)

    def test_cli_idempoten(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self.write(tmp_dir, [
                {"username": "demo-user-01", "password": PASSWORD},
                {"username": "demo-user-02", "password": PASSWORD},
            ])
            db = Path(tmp_dir) / "source.sqlite3"
            args = ["--db", str(db), "--accounts", str(path)]
            for expected in ("2 dari 2", "0 dari 2"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(seed_main(args), 0)
                self.assertIn(expected, out.getvalue())
            with connect(db) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM auth_accounts").fetchone()[0], 2
                )
            os.chmod(path, 0o644)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(seed_main(args), 2)
            self.assertNotIn(PASSWORD, err.getvalue())


if __name__ == "__main__":
    unittest.main()
