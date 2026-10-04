#!/bin/sh
# Creates the secrets DocNest needs in ./secrets (existing files are kept).
# The container runs as UID 10001, so the files are handed to that user.
set -eu
cd "$(dirname "$0")"
umask 077
mkdir -p secrets

gen() {
  if [ -s "secrets/$1" ]; then
    echo "secrets/$1 exists — keeping it"
  else
    openssl rand -base64 "$2" | tr -d '\n' > "secrets/$1"
    echo "created secrets/$1"
  fi
}

gen master_key 48
gen django_secret_key 64
gen db_password 32
gen db_admin_password 32

# The app (UID 10001) reads all secrets; the database init script (postgres, GID 70)
# additionally needs db_password once to create the application role.
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
[ -n "$SUDO" ] && echo "Setting ownership to UID 10001 (needs sudo)…"
$SUDO chown 10001:10001 secrets/master_key secrets/django_secret_key secrets/db_admin_password
$SUDO chown 10001:70 secrets/db_password
$SUDO chmod 400 secrets/master_key secrets/django_secret_key secrets/db_admin_password
$SUDO chmod 440 secrets/db_password

cat <<MSG

Done. IMPORTANT: back up secrets/master_key offline (e.g. in your password manager).
Without it, the encrypted data in the database and the Proton session cannot be read.
MSG
