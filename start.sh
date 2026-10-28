#!/usr/bin/env bash
# One-click launcher: database + Flask backend (auto-starts scanner, tick monitor,
# tick collector in market hours) + Next.js dashboard. Ctrl+C stops everything.
#   ./start.sh
set -u
cd "$(dirname "$0")"
mkdir -p logs
PIDS=()

cleanup() {
  echo; echo "Stopping AI Trader..."
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
  pkill -f "scripts/collect_ticks.py" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup INT TERM

# 1. Python venv
if [ ! -x .venv/bin/python ]; then
  echo "No .venv found. Create it with Python 3.13 first:"
  echo "  uv venv --python 3.13 .venv && source .venv/bin/activate && uv pip install -r requirements.txt"
  exit 1
fi
source .venv/bin/activate

# 1b. Python packages (requirements.txt had no Flask before — install if anything core is missing)
if ! python -c "import flask, flask_cors, sqlalchemy, pandas, xgboost, requests" 2>/dev/null; then
  echo "Installing Python packages (first run can take several minutes; torch is large)..."
  if command -v uv >/dev/null 2>&1; then uv pip install -r requirements.txt; else pip install -r requirements.txt; fi \
    || { echo "Package install failed. Make sure .venv uses Python 3.13 (not 3.14)."; exit 1; }
fi

# 2. Dashboard dependencies
[ -d dashboard/node_modules ] || (echo "Installing dashboard packages..." && cd dashboard && npm install --no-audit --no-fund)

# 3. PostgreSQL / TimescaleDB (WSL: needs `sudo` the first time)
if command -v pg_isready >/dev/null 2>&1 && ! pg_isready -q; then
  echo "Starting PostgreSQL..."
  sudo service postgresql start || echo "  (could not start PostgreSQL — the backend will run without a database)"
  sleep 2
fi

# 3b. Database login check — (re)configure role/password/database automatically if it fails
if command -v pg_isready >/dev/null 2>&1 && ! python - <<'PY' 2>/dev/null
from sqlalchemy import text
from database.db import engine
with engine.connect() as c: c.execute(text("select 1"))
PY
then
  echo "Database login failed — running scripts/setup_db.sh (needs sudo)..."
  ./scripts/setup_db.sh || echo "  (database setup failed — see messages above; the dashboard will run without data)"
fi

# 4. Backend
echo "Starting backend  → http://localhost:5050   (log: logs/backend.log)"
python backend/app.py >> logs/backend.log 2>&1 &
BACKEND_PID=$!
PIDS+=($BACKEND_PID)

echo -n "Waiting for backend"
for i in $(seq 1 60); do
  if curl -sf -o /dev/null http://localhost:5050/api/broker/config; then echo " — up."; break; fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo; echo "BACKEND CRASHED. Last log lines:"; echo "----------------------------------------"
    tail -n 30 logs/backend.log; echo "----------------------------------------"
    echo "Fix the error above, then run ./start.sh again."
    cleanup
  fi
  echo -n "."; sleep 1
  [ "$i" = 60 ] && echo " still starting (check logs/backend.log)"
done

# 5. Dashboard
echo "Starting dashboard → http://localhost:3000  (log: logs/dashboard.log)"
(cd dashboard && exec npm run dev) >> logs/dashboard.log 2>&1 &
PIDS+=($!)

sleep 5
echo
echo "AI Trader is running. Open http://localhost:3000 in your Windows browser."
echo "Press Ctrl+C to stop everything."
tail -n 0 -f logs/backend.log logs/dashboard.log &
PIDS+=($!)
wait
