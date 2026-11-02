"""Pemeriksaan berkas deployment Source terhadap kontrak (tanpa systemd/nft sungguhan)."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deployment" / "source"


def read(name):
    return (SOURCE / name).read_text(encoding="utf-8")


class TestDeploymentSource(unittest.TestCase):
    def test_unit_mengikuti_kontrak(self):
        contract = (ROOT / "config" / "contract.json").read_text(encoding="utf-8")
        unit = read("loglab-source.service")
        for nilai in ("/etc/loglab/source.env", "/run/loglab/sender.lock"):
            self.assertIn(nilai, contract)
        self.assertIn("EnvironmentFile=/etc/loglab/source.env", unit)
        self.assertIn("loglab.source_main:app", unit)
        self.assertIn("User=loglab-svc", unit)
        self.assertIn("RuntimeDirectory=loglab", unit)  # lokasi /run/loglab/sender.lock
        for variabel in ("LOGLAB_BIND_HOST", "LOGLAB_PORT", "LOGLAB_TLS_CERT", "LOGLAB_TLS_KEY"):
            self.assertIn("${%s}" % variabel, unit)
        self.assertIn("--workers 1", unit)  # satu proses, satu sender
        self.assertNotIn("source /etc/loglab", unit)
        self.assertEqual(sorted(p.name for p in SOURCE.glob("*.service")),
                         ["loglab-source-backup.service", "loglab-source.service"])

    def test_firewall_default_deny(self):
        nft = read("source.nft")
        self.assertIn("policy drop", nft)
        self.assertIn("ct state established,related accept", nft)
        self.assertIn("@HOST_ADDR@", nft)
        self.assertNotIn("dport 5432", nft)
        self.assertNotIn("sqlite", nft.lower())

    def test_skrip_sintaks_bash_valid(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash tidak tersedia")
        for name in ("setup.sh", "backup.sh", "dev-cert.sh"):
            with self.subTest(name):
                result = subprocess.run([bash, "-n", str(SOURCE / name)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_penjaga_firewall_membaca_sesi_ssh_aktif(self):
        setup = read("setup.sh")
        self.assertNotIn("SSH_CONNECTION", setup)  # sudo membuang variabel ini
        self.assertIn("sport = :22", setup)
        self.assertIn("--force-firewall", setup)

    def test_setup_tidak_mencetak_secret(self):
        setup = read("setup.sh")
        for baris in setup.splitlines():
            if baris.lstrip().startswith(("log ", "warn ", "die ")):
                self.assertNotRegex(baris, r"\$\{?(NEW_TOKEN|NEW_KEY)")
                self.assertNotIn("openssl rand", baris)


if __name__ == "__main__":
    unittest.main()
