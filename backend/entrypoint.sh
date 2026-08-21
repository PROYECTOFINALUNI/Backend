#!/bin/sh
set -eu

python - <<'PY'
import os
import sys
import time

import psycopg

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    sys.exit("DATABASE_URL is required")

attempts = int(os.environ.get("DATABASE_WAIT_ATTEMPTS", "30"))
delay = float(os.environ.get("DATABASE_WAIT_DELAY", "2"))

for attempt in range(1, attempts + 1):
    try:
        with psycopg.connect(database_url, connect_timeout=3):
            print("PostgreSQL is available.")
            break
    except psycopg.OperationalError as exc:
        if attempt == attempts:
            sys.exit(f"PostgreSQL was not available after {attempts} attempts: {exc}")
        print(f"Waiting for PostgreSQL ({attempt}/{attempts})...")
        time.sleep(delay)
PY

python manage.py migrate --noinput
exec "$@"
