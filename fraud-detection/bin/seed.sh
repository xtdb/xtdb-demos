#!/usr/bin/env bash
# Load a deterministic bitemporal dataset into a running stack, then train a model.
#
#   ./bin/seed.sh             # 50k transactions, comfortable on the default 4g heap
#   ./bin/seed.sh 300000      # the full demo set — needs XTDB_HEAP=10g in .env
#
# The backfill replays history in *learned-at* order, one XTDB transaction per day with
# an explicit SYSTEM_TIME, so a chargeback lands later in system-time than the
# transaction it corrects. This preserves the system-time history used to replay a
# score before its labels changed. Training uses fraud_status valid-time intervals
# to select the status available at each decision.
# The replay needs a fresh node: nothing already committed may be newer than its
# first simulated day, because system time only moves forward.
#
# Fixed seed, so every run reproduces the same accounts and transactions.
set -euo pipefail

cd "$(dirname "$0")/.."
N="${1:-50000}"

echo "== seeding $N transactions (learned-at order, one tx per day)"
docker compose exec -T api python backfill_adbc.py --n "$N"

echo "== training"
docker compose exec -T api python model.py
