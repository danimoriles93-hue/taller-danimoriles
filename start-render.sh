#!/usr/bin/env sh
set -eu

DB_PATH="${MOTORFIX_DB_PATH:-/var/data/motorfix.db}"
SEED_DB="/app/motorfix.db"

mkdir -p "$(dirname "$DB_PATH")"

if [ ! -f "$DB_PATH" ]; then
  echo "Inicializando base de datos MotorFix en $DB_PATH"
  cp "$SEED_DB" "$DB_PATH"
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}"
