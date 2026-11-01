# Deployment Source (VM A)

Berkas di folder ini memasang peran Source di guest Debian: user layanan `loglab-svc`,
unit `loglab-source.service` (aplikasi login + satu sender di proses yang sama),
backup harian, dan firewall default-deny. Semua nilai mengikuti `config/contract.json`.

## Urutan

1. Guest Debian terpasang, repo di-clone, `make dev-setup` lolos (virtualenv ada di `.venv`).
2. Adapter host-only guest memakai alamat kontrak **172.16.202.21** (nilai `LOGLAB_BIND_HOST`
   dikunci kontrak; service menolak alamat lain). Adapter NAT dipakai sementara untuk `apt`.
3. `sudo deployment/source/setup.sh` — aman diulang; tidak mengganti token, kunci, akun, atau database.
4. Sertifikat: dari CA lab yang sama untuk A dan B, atau untuk uji sendiri
   `sudo deployment/source/dev-cert.sh --install` lalu pasang `~/loglab-dev-ca/ca.crt` di browser host.
5. `sudo deployment/source/setup.sh` lagi, lalu buka `https://172.16.202.21:8443/login`.
6. Firewall (setelah service sehat): `sudo deployment/source/setup.sh --apply-firewall --host-addr <IP host>`.

## Rahasia

- `/etc/loglab/source.env` (root, 0600): `LOGLAB_SOURCE_TOKEN` harus sama dengan Monitor.
  Ubah dengan `sudoedit`, lalu `sudo systemctl restart loglab-source`.
- `/etc/loglab/test-accounts.json` (root, 0600): password `demo-user-01/02`; baca dengan `sudo cat`.
- Jangan masukkan file-file ini ke Git atau screenshot.

## Backup dan restore

Backup harian 02:30 (`loglab-source-backup.timer`) memakai `loglab.backup` (atomik, dicek integritas):
`/var/backups/loglab/daily.sqlite3` plus 7 salinan bertanggal terakhir. Jalankan manual:
`sudo systemctl start loglab-source-backup.service`.

Restore (uji di lokasi terpisah dulu):

    sudo systemctl stop loglab-source
    sudo -u loglab-svc python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('pragma integrity_check').fetchone()[0])" /var/backups/loglab/daily.sqlite3
    sudo cp --preserve=mode /var/lib/loglab/source.sqlite3 /var/backups/loglab/sebelum-restore.sqlite3
    sudo install -o loglab-svc -g loglab-svc -m 0600 /var/backups/loglab/daily.sqlite3 /var/lib/loglab/source.sqlite3
    sudo systemctl start loglab-source

Bila sequence dapat terulang terhadap watermark Monitor, gunakan stream baru (lihat SRS event-pipeline).
Backup di dalam guest tidak melindungi dari hilangnya disk guest: salin ke lokasi privat di host.

## Firewall

`source.nft` hanya menerima koneksi baru dari alamat host (SSH 22, web 8443). Setup menolak
menerapkannya bila sesi SSH aktif berasal dari alamat lain. Pemulihan dari console VM:
`sudo nft delete table inet loglab_source`.
