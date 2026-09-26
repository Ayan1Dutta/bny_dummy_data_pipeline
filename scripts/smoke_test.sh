#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:?usage: smoke_test.sh BASE_URL API_KEY}"
API_KEY="${2:?usage: smoke_test.sh BASE_URL API_KEY}"
START="${3:-2026-09-01}"
END="${4:-2026-09-07}"

echo "smoke: health"
curl -fsS "$BASE_URL/health" | grep -q '"status":"ok"'

echo "smoke: readiness"
curl -fsS "$BASE_URL/ready" | grep -q '"warehouse_reachable":true'

echo "smoke: settlement summary"
BODY=$(curl -fsS -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/settlement-summary?start_date=$START&end_date=$END")
echo "$BODY" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert 0 <= d['settlement_rate'] <= 100, d
assert 0 <= d['sla_rate'] <= 100, d
assert d['transaction_amount'] >= 0, d
print('settlement_rate', d['settlement_rate'], 'sla_rate', d['sla_rate'])
"

echo "smoke: unauthenticated request is rejected"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/settlement-summary?start_date=$START&end_date=$END")
[ "$CODE" = "401" ] || { echo "expected 401, got $CODE"; exit 1; }

echo "smoke: all checks passed"
