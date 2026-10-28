#!/usr/bin/env bash
# Align the local PostgreSQL with the app's DB settings (.env), create the database and schema.
# Safe to re-run. Needs sudo (to talk to the postgres superuser).
set -u
cd "$(dirname "$0")/.."
getenv() { grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r"' ; }
DB_USER="$(getenv DB_USER)";         DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="$(getenv DB_PASSWORD)"; DB_PASSWORD="${DB_PASSWORD:-postgres}"
DB_NAME="$(getenv DB_NAME)";         DB_NAME="${DB_NAME:-trading}"

pg_isready -q || sudo service postgresql start || { echo "Could not start PostgreSQL (install: sudo apt install postgresql)"; exit 1; }
sleep 1
PSQL=(sudo -u postgres psql -v ON_ERROR_STOP=1 -q)

echo "Setting password for role '$DB_USER' and creating database '$DB_NAME'..."
if [ "$DB_USER" = "postgres" ]; then
  "${PSQL[@]}" -c "ALTER USER postgres PASSWORD '$DB_PASSWORD';" || exit 1
else
  "${PSQL[@]}" -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DB_USER') THEN CREATE ROLE $DB_USER LOGIN SUPERUSER PASSWORD '$DB_PASSWORD'; ELSE ALTER ROLE $DB_USER PASSWORD '$DB_PASSWORD'; END IF; END \$\$;" || exit 1
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME" || exit 1

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'" | grep -q 1; then
  echo
  echo "NOTE: the TimescaleDB extension is not installed. The app still works with plain PostgreSQL tables,"
  echo "      but for hypertables (faster tick/candle queries) install it: https://docs.timescale.com/self-hosted/latest/install/installation-linux/"
  echo
fi

[ -d .venv ] && source .venv/bin/activate
python -c "from database.db import init_db; init_db()" && echo "Database ready."
