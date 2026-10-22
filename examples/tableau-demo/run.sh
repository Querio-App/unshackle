#!/usr/bin/env bash
# Tableau workbook -> Git repo -> numbers checked, end to end, offline.
#
# Uses the fixture export in tests/fixtures/tableau (what `unshackle extract tableau`
# writes) and a small DuckDB warehouse, so it runs without a Tableau site.
#   ./examples/tableau-demo/run.sh [output-dir]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d)/acme-analytics}"
if [ -x "$ROOT/.venv/bin/unshackle" ]; then
  UNSHACKLE="${UNSHACKLE:-$ROOT/.venv/bin/unshackle}"
  PY="${PYTHON:-$ROOT/.venv/bin/python}"
else
  UNSHACKLE="${UNSHACKLE:-unshackle}"
  PY="${PYTHON:-python}"
fi

mkdir -p "$OUT/migration/raw"
cp -R "$ROOT/tests/fixtures/tableau" "$OUT/migration/raw/tableau"
cd "$OUT"
git init -q 2>/dev/null || true

echo "== 1. scaffold the unshackle layout"
"$UNSHACKLE" init --name acme-analytics >/dev/null

echo "== 2. build: parse the workbook and write metrics, catalog, notebooks, policies"
"$UNSHACKLE" build tableau

echo "== 3. what's left for the agent"
"$UNSHACKLE" todo | tail -n 1

echo "== 4. a warehouse to check against"
"$PY" "$HERE/make_warehouse.py" "$OUT/warehouse.duckdb"
export UNSHACKLE_WAREHOUSE_URL="duckdb:///$OUT/warehouse.duckdb"

echo "== 5. render a migrated dashboard"
"$PY" -m unshackle.run notebooks/executive-overview.ipynb

echo "== 6. parity: Tableau's own numbers vs the migrated SQL"
mkdir -p migration/parity
CSV=$(ls migration/raw/tableau/views/*.csv | head -n 1)
cat > migration/parity/sales-trend.yaml <<YAML
name: Sales trend by category and month
question: What were monthly sales by category in East and West?
source:
  csv: $CSV
target:
  notebook: notebooks/executive-overview.ipynb
  tile: sales_trend
keys: [category, month_of_order_date]
tolerance: 0.0001
YAML
"$UNSHACKLE" verify

echo
echo "Done: $OUT"
echo "  migration/report.md   what was migrated and what's left"
echo "  evals/                parity checks that keep running"
