"""Menyiapkan akun fiktif Source dari daftar privat. Aman dijalankan berulang.

Daftar akun (default /etc/loglab/test-accounts.json, mode 0600, tidak masuk Git):
  [{"username": "demo-user-01", "password": "..."}, ...]
Akun yang sudah ada tidak ditimpa.
"""
import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence

from loglab.db import connect, initialize_source
from loglab.security import hash_password

DEFAULT_DB = Path("/var/lib/loglab/source.sqlite3")
DEFAULT_ACCOUNTS = Path("/etc/loglab/test-accounts.json")
MIN_PASSWORD_LENGTH = 12


def seed_accounts(db_path, accounts: Dict[str, str]) -> int:
    """Tambah akun yang belum ada. Mengembalikan jumlah akun baru."""
    for username, password in accounts.items():
        if not username:
            raise ValueError("username tidak boleh kosong")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"password minimal {MIN_PASSWORD_LENGTH} karakter untuk '{username}'"
            )
    initialize_source(db_path)
    added = 0
    with connect(db_path) as conn:
        for username, password in accounts.items():
            exists = conn.execute(
                "SELECT 1 FROM auth_accounts WHERE username = ?", (username,)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO auth_accounts (username, password_hash, active) "
                "VALUES (?, ?, 1)",
                (username, hash_password(password)),
            )
            added += 1
        conn.commit()
    return added


def load_accounts_file(path) -> Dict[str, str]:
    """Baca daftar akun privat. Pesan galat tidak memuat password."""
    path = Path(path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"tidak bisa membaca daftar akun ({type(exc).__name__})") from None
    if mode & 0o077:
        raise ValueError("daftar akun harus berizin 0600 (tanpa akses group/other)")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("daftar akun bukan JSON yang valid") from None
    if not isinstance(data, list) or not data:
        raise ValueError("daftar akun harus berupa array yang tidak kosong")
    accounts: Dict[str, str] = {}
    seen = set()
    for item in data:
        if (
            not isinstance(item, dict)
            or set(item) != {"username", "password"}
            or not all(isinstance(value, str) for value in item.values())
        ):
            raise ValueError("setiap akun harus berisi tepat username dan password (teks)")
        folded = item["username"].casefold()
        if folded in seen:
            raise ValueError("username ganda pada daftar akun")
        seen.add(folded)
        accounts[item["username"]] = item["password"]
    return accounts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Seed akun fiktif Source.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    args = parser.parse_args(argv)
    try:
        accounts = load_accounts_file(args.accounts)
        added = seed_accounts(args.db, accounts)
    except ValueError as exc:
        print(f"Seed gagal: {exc}", file=sys.stderr)
        return 2
    print(f"Akun baru ditambahkan: {added} dari {len(accounts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
