#!/usr/bin/env bash
# Backup SQLite Source memakai helper atomik (loglab.backup), bukan salin file aktif.
# Hasil: daily.sqlite3 (kontrak) + salinan bertanggal; hanya 7 salinan bertanggal terakhir disimpan.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
PY=${LOGLAB_PYTHON:-$REPO_DIR/.venv/bin/python}
SRC=${LOGLAB_BACKUP_SOURCE:-/var/lib/loglab/source.sqlite3}
DEST_DIR=${LOGLAB_BACKUP_DIR:-/var/backups/loglab}
KEEP=7

umask 077
"$PY" -m loglab.backup --source "$SRC" --destination "$DEST_DIR/daily.sqlite3"

stamp=$(date -u +%F)
cp --preserve=mode,timestamps "$DEST_DIR/daily.sqlite3" "$DEST_DIR/daily-$stamp.sqlite3.tmp"
mv -f "$DEST_DIR/daily-$stamp.sqlite3.tmp" "$DEST_DIR/daily-$stamp.sqlite3"

mapfile -t old < <(ls -1 "$DEST_DIR"/daily-????-??-??.sqlite3 2>/dev/null | sort -r | tail -n +$((KEEP + 1)))
for f in "${old[@]}"; do
  rm -f -- "$f"
done
echo "backup selesai: $DEST_DIR/daily-$stamp.sqlite3"
