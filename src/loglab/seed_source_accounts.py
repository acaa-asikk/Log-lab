"""Menyiapkan akun fiktif Source. Aman dijalankan berulang (E3-05).

Password dibaca dari environment (LOGLAB_SEED_PASSWORD), tidak ditulis di kode
atau Git. Akun yang sudah ada tidak ditimpa.
"""
import os
import sys
from pathlib import Path
from typing import Dict

from loglab.db import connect, initialize_source
from loglab.security import hash_password

DEFAULT_USERNAMES = ("akun-uji-1", "akun-uji-2", "akun-uji-3")
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


def main() -> int:
    db_path = os.environ.get("LOGLAB_DB_PATH", "")
    password = os.environ.get("LOGLAB_SEED_PASSWORD", "")
    if not db_path:
        print("LOGLAB_DB_PATH belum diatur", file=sys.stderr)
        return 2
    try:
        added = seed_accounts(
            Path(db_path), {name: password for name in DEFAULT_USERNAMES}
        )
    except ValueError as exc:
        print(f"Seed gagal: {exc}", file=sys.stderr)
        return 2
    print(f"Akun baru ditambahkan: {added} dari {len(DEFAULT_USERNAMES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
