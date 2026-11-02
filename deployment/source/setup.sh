#!/usr/bin/env bash
# Setup peran Source (VM A) di guest Debian. Aman dijalankan ulang.
#
#   sudo deployment/source/setup.sh [opsi]
#
# Opsi:
#   --host-addr IP      alamat host yang boleh SSH/web (default 172.16.202.1)
#   --apply-firewall    terapkan firewall sekarang (default: hanya dipasang + diperiksa)
#   --force-firewall    terapkan walau alamat SSH aktif tidak sama dengan --host-addr
#   --skip BAGIAN       lewati: apt | acl | systemd | firewall  (boleh diulang)
#   -h, --help
#
# Setup TIDAK membuat ulang token/kunci/akun/database yang sudah ada.
set -euo pipefail

SVC_USER=loglab-svc
ETC_DIR=/etc/loglab
DATA_DIR=/var/lib/loglab
BACKUP_DIR=/var/backups/loglab
ENV_FILE=$ETC_DIR/source.env
ACCOUNTS_FILE=$ETC_DIR/test-accounts.json
DB_FILE=$DATA_DIR/source.sqlite3
UNIT_DIR=/etc/systemd/system
NFT_DIR=/etc/nftables.d
NFT_INCLUDE='include "/etc/nftables.d/*.nft"'
OS_RELEASE=${LOGLAB_OS_RELEASE:-/etc/os-release}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
VENV_PY=$REPO_DIR/.venv/bin/python

HOST_ADDR=172.16.202.1
APPLY_FIREWALL=0
FORCE_FIREWALL=0
SKIP=()
START_OK=1

log()  { printf '[setup-source] %s\n' "$*"; }
warn() { printf '[setup-source] PERINGATAN: %s\n' "$*" >&2; }
die()  { printf '[setup-source] GAGAL: %s\n' "$*" >&2; exit 1; }
skipped() { [[ " ${SKIP[*]-} " == *" $1 "* ]]; }

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host-addr)      [ $# -ge 2 ] || die "--host-addr butuh nilai"; HOST_ADDR=$2; shift 2 ;;
    --apply-firewall) APPLY_FIREWALL=1; shift ;;
    --force-firewall) FORCE_FIREWALL=1; shift ;;
    --skip)           [ $# -ge 2 ] || die "--skip butuh nilai"; SKIP+=("$2"); shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "opsi tidak dikenal: $1" ;;
  esac
done

[[ "$HOST_ADDR" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--host-addr harus berupa alamat IPv4"
[ "$(id -u)" -eq 0 ] || die "jalankan dengan sudo"
[[ "$REPO_DIR" != *[[:space:]]* ]] || die "path repo tidak boleh mengandung spasi: $REPO_DIR"

check_os() {
  local id version
  # shellcheck source=/dev/null
  id=$(. "$OS_RELEASE" && printf '%s' "${ID:-}")
  # shellcheck source=/dev/null
  version=$(. "$OS_RELEASE" && printf '%s' "${VERSION_ID:-}")
  [ "$id" = debian ] || die "OS bukan Debian (ID=$id)"
  [ "$version" = 13 ] || warn "Debian $version terdeteksi; kontrak memakai Debian 13"
}

check_repo() {
  [ -x "$VENV_PY" ] || die "virtualenv belum ada di $REPO_DIR/.venv; jalankan 'make dev-setup' dulu"
  [ -f "$REPO_DIR/config/source.example.env" ] || die "config/source.example.env tidak ditemukan"
  [ -f "$REPO_DIR/scripts/check_contract.py" ] || die "scripts/check_contract.py tidak ditemukan"
}

install_packages() {
  skipped apt && { log "lewati: apt"; return; }
  local missing=() pkg
  for pkg in nftables acl openssl; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ ${#missing[@]} -eq 0 ]; then log "paket sistem sudah ada"; return; fi
  log "memasang paket: ${missing[*]} (butuh akses paket sementara/NAT)"
  apt-get update -qq || die "apt-get update gagal; aktifkan adapter NAT sementara"
  apt-get install -y -qq --no-install-recommends "${missing[@]}" || die "pemasangan paket gagal"
}

ensure_user() {
  getent group "$SVC_USER" >/dev/null || groupadd --system "$SVC_USER"
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SVC_USER" --home-dir "$DATA_DIR" --no-create-home \
      --shell /usr/sbin/nologin "$SVC_USER"
    log "user layanan dibuat: $SVC_USER"
  fi
}

ensure_dirs() {
  install -d -o root -g "$SVC_USER" -m 0750 "$ETC_DIR"
  install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$DATA_DIR"
  install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$BACKUP_DIR"
}

ensure_env() {
  if [ -e "$ENV_FILE" ]; then
    log "env sudah ada, tidak diubah: $ENV_FILE"
  else
    local tmp
    tmp=$(mktemp "$ETC_DIR/.source.env.XXXXXX")
    chmod 0600 "$tmp"
    NEW_TOKEN=$(openssl rand -hex 32) NEW_KEY=$(openssl rand -hex 32) \
      python3 - "$REPO_DIR/config/source.example.env" "$tmp" <<'PY'
import os
import sys

src, dst = sys.argv[1:3]
text = open(src, encoding="utf-8").read()
text = text.replace("__SET_ONCE_PER_LAB__", os.environ["NEW_TOKEN"])
text = text.replace("__GENERATE_SOURCE_ONLY__", os.environ["NEW_KEY"])
with open(dst, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
    chown root:root "$tmp"
    mv "$tmp" "$ENV_FILE"
    log "env dibuat dengan token dan kunci acak: $ENV_FILE"
    log "SOURCE_TOKEN harus SAMA dengan Monitor; sinkronkan secara privat (jangan di screenshot)"
  fi
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  if ! python3 "$REPO_DIR/scripts/check_contract.py" --role source --env "$ENV_FILE"; then
    warn "env belum lolos pemeriksaan kontrak; service tidak dinyalakan"
    START_OK=0
  fi
}

ensure_accounts() {
  if [ -e "$ACCOUNTS_FILE" ]; then
    log "daftar akun uji sudah ada, tidak diubah"
  else
    python3 - "$ACCOUNTS_FILE" <<'PY'
import json
import os
import secrets
import sys

accounts = [
    {"username": name, "password": secrets.token_urlsafe(18)}
    for name in ("demo-user-01", "demo-user-02")
]
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(accounts, handle, indent=2)
    handle.write("\n")
PY
    log "daftar akun uji dibuat dengan password acak: $ACCOUNTS_FILE (baca dengan sudo)"
  fi
  chown root:root "$ACCOUNTS_FILE"
  chmod 0600 "$ACCOUNTS_FILE"

  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_DIR/src" \
    "$VENV_PY" -m loglab.seed_source_accounts --db "$DB_FILE" --accounts "$ACCOUNTS_FILE"
  local f
  for f in "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm" "$DB_FILE-journal"; do
    if [ -e "$f" ]; then
      chown "$SVC_USER:$SVC_USER" "$f"
      chmod 0600 "$f"
    fi
  done
}

grant_repo_access() {
  skipped acl && { log "lewati: acl"; return; }
  command -v setfacl >/dev/null || die "setfacl tidak ada; pasang paket 'acl'"
  local path=$REPO_DIR
  while [ "$path" != / ]; do
    # beri hak lintas (x) saja pada folder induk yang belum bisa dilintasi pihak lain
    if [ "$(stat -c %A "$path" | cut -c10)" != x ]; then
      setfacl -m "u:$SVC_USER:x" "$path"
    fi
    path=$(dirname "$path")
  done
  setfacl -R -m "u:$SVC_USER:rX" "$REPO_DIR"
  log "akses baca repo untuk $SVC_USER diberikan (tanpa membuka .ssh/history)"
}

ensure_tls() {
  local missing=() f
  for f in ca.crt tls.crt tls.key; do
    [ -f "$ETC_DIR/$f" ] || missing+=("$f")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    warn "belum ada di $ETC_DIR: ${missing[*]}"
    warn "pasang dari CA lab, atau untuk uji sendiri: sudo deployment/source/dev-cert.sh --install"
    START_OK=0
    return
  fi
  chown root:root "$ETC_DIR/ca.crt" "$ETC_DIR/tls.crt"
  chmod 0644 "$ETC_DIR/ca.crt" "$ETC_DIR/tls.crt"
  chown root:"$SVC_USER" "$ETC_DIR/tls.key"
  chmod 0640 "$ETC_DIR/tls.key"
}

install_units() {
  local unit
  for unit in loglab-source.service loglab-source-backup.service loglab-source-backup.timer; do
    sed "s|@REPO_DIR@|$REPO_DIR|g" "$SCRIPT_DIR/$unit" > "$UNIT_DIR/$unit.new"
    chmod 0644 "$UNIT_DIR/$unit.new"
    mv "$UNIT_DIR/$unit.new" "$UNIT_DIR/$unit"
  done
  chmod 0755 "$SCRIPT_DIR/backup.sh"
  log "unit systemd terpasang"
}

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1; }

health_check() {
  "$VENV_PY" - "$1" "$2" <<'PY'
import json
import ssl
import sys
import urllib.request

host, port = sys.argv[1:3]
context = ssl.create_default_context(cafile="/etc/loglab/ca.crt")
with urllib.request.urlopen(f"https://{host}:{port}/health", context=context, timeout=3) as response:
    data = json.load(response)
sys.exit(0 if data.get("service") == "loglab-source" and data.get("state") == "ready" else 1)
PY
}

start_services() {
  skipped systemd && { log "lewati: systemd"; return; }
  systemctl daemon-reload
  systemctl enable loglab-source.service loglab-source-backup.timer >/dev/null
  if [ "$START_OK" -ne 1 ]; then
    warn "service dipasang dan di-enable tetapi belum dinyalakan (lihat peringatan di atas)"
    return
  fi
  local bind port
  bind=$(env_value LOGLAB_BIND_HOST)
  port=$(env_value LOGLAB_PORT)
  if ! ip -o -4 addr show | grep -qF " $bind/"; then
    warn "alamat $bind belum terpasang pada guest ini; service tidak dinyalakan"
    warn "atur alamat host-only guest ke $bind (lihat deployment/source/README.md)"
    return
  fi
  systemctl restart loglab-source.service
  systemctl start loglab-source-backup.timer
  local _
  for _ in $(seq 1 20); do
    if health_check "$bind" "$port" 2>/dev/null; then
      log "service sehat: https://$bind:$port/health"
      return
    fi
    sleep 1
  done
  warn "service belum menjawab /health; lihat: journalctl -u loglab-source -n 50 --no-pager"
}

# Alamat klien SSH yang sedang tersambung (satu per baris).
ssh_peers() {
  ss -Htn state established '( sport = :22 )' 2>/dev/null \
    | awk '{print $4}' \
    | sed -e 's/:[0-9]*$//' -e 's/^\[//' -e 's/\]$//' -e 's/^::ffff://' \
    | sort -u
}

install_firewall() {
  skipped firewall && { log "lewati: firewall"; return; }
  command -v nft >/dev/null || die "nft tidak ada; pasang paket 'nftables'"
  install -d -m 0755 "$NFT_DIR"
  sed "s|@HOST_ADDR@|$HOST_ADDR|g" "$SCRIPT_DIR/source.nft" > "$NFT_DIR/loglab-source.nft"
  chmod 0644 "$NFT_DIR/loglab-source.nft"
  nft -c -f "$NFT_DIR/loglab-source.nft" || die "aturan firewall tidak valid"
  if [ ! -f /etc/nftables.conf ]; then
    printf '#!/usr/sbin/nft -f\nflush ruleset\n' > /etc/nftables.conf
  fi
  grep -qF "$NFT_INCLUDE" /etc/nftables.conf || printf '\n%s\n' "$NFT_INCLUDE" >> /etc/nftables.conf
  systemctl enable nftables >/dev/null 2>&1 || true

  if [ "$APPLY_FIREWALL" -ne 1 ]; then
    log "firewall terpasang dan valid, BELUM diterapkan (tambahkan --apply-firewall)"
    return
  fi
  # Baca koneksi SSH yang benar-benar aktif (sudo membuang variabel lingkungan SSH, jadi env tidak bisa diandalkan).
  local peer others=()
  while IFS= read -r peer; do
    if [ -n "$peer" ] && [ "$peer" != "$HOST_ADDR" ]; then
      others+=("$peer")
    fi
  done < <(ssh_peers)
  if [ ${#others[@]} -gt 0 ] && [ "$FORCE_FIREWALL" -ne 1 ]; then
    die "ada sesi SSH aktif dari ${others[*]}, bukan $HOST_ADDR: firewall akan memutusnya. Ulangi dengan --host-addr yang benar atau --force-firewall (pemulihan: console VM, 'sudo nft delete table inet loglab_source')"
  fi
  nft -f "$NFT_DIR/loglab-source.nft"
  log "firewall diterapkan (SSH/web hanya dari $HOST_ADDR)"
}

check_os
check_repo
install_packages
ensure_user
ensure_dirs
ensure_env
ensure_accounts
grant_repo_access
ensure_tls
install_units
start_services
install_firewall
log "selesai"
