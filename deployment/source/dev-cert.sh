#!/usr/bin/env bash
# KHUSUS UJI SENDIRI: membuat CA lab sementara + sertifikat Source.
# Untuk lab bersama, pakai sertifikat yang diterbitkan CA lab yang SAMA untuk A dan B;
# kunci privat CA tidak boleh berada di VM.
#
#   deployment/source/dev-cert.sh [--ip 172.16.202.21] [--workdir DIR] [--install]
set -euo pipefail

IP=172.16.202.21
if [ -n "${SUDO_USER:-}" ]; then
  USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)  # bukan folder root bila lewat sudo
else
  USER_HOME=$HOME
fi
WORKDIR=$USER_HOME/loglab-dev-ca
INSTALL=0
ETC_DIR=/etc/loglab

while [ $# -gt 0 ]; do
  case "$1" in
    --ip)      [ $# -ge 2 ] || { echo "--ip butuh nilai" >&2; exit 1; }; IP=$2; shift 2 ;;
    --workdir) [ $# -ge 2 ] || { echo "--workdir butuh nilai" >&2; exit 1; }; WORKDIR=$2; shift 2 ;;
    --install) INSTALL=1; shift ;;
    *)         echo "opsi tidak dikenal: $1" >&2; exit 1 ;;
  esac
done

[[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "--ip harus berupa alamat IPv4" >&2; exit 1; }

umask 077
mkdir -p "$WORKDIR"
cd "$WORKDIR"

if [ ! -f ca.key ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days 825 -subj "/CN=LogLab Dev CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.crt
  echo "CA dev dibuat: $WORKDIR/ca.crt"
fi

openssl ecparam -name prime256v1 -genkey -noout -out tls.key
openssl req -new -key tls.key -subj "/CN=loglab-source" -out tls.csr
printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature\n' "$IP" > ext.cnf
openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -sha256 -days 397 \
  -extfile ext.cnf -out tls.crt 2>/dev/null
rm -f tls.csr ext.cnf
echo "sertifikat Source dibuat untuk IP $IP: $WORKDIR/tls.crt"
if [ -n "${SUDO_USER:-}" ]; then
  chown -R "$SUDO_USER" "$WORKDIR"  # agar ca.crt bisa disalin dengan scp oleh pemilik akun
fi

if [ "$INSTALL" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || { echo "--install butuh sudo" >&2; exit 1; }
  install -d -o root -g loglab-svc -m 0750 "$ETC_DIR"
  install -o root -g root -m 0644 ca.crt "$ETC_DIR/ca.crt"
  install -o root -g root -m 0644 tls.crt "$ETC_DIR/tls.crt"
  install -o root -g loglab-svc -m 0640 tls.key "$ETC_DIR/tls.key"
  echo "terpasang di $ETC_DIR (ca.crt, tls.crt, tls.key)"
fi
echo "Pasang $WORKDIR/ca.crt pada browser host (CA dev ini hanya untuk uji sendiri)."
