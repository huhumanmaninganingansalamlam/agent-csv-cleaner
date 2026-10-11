#!/bin/sh
set -eu
BASE_URL=${BASE_URL:-http://127.0.0.1:8000}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
printf 'Order ID,Name\n001,Demo\n001,Demo\n' > "$TMP/input.csv"
KEY="smoke-$(date +%s)-$$"
curl --fail --silent --show-error "$BASE_URL/health" > "$TMP/health.json"
curl --fail --silent --show-error -H "Idempotency-Key: $KEY" -F "file=@$TMP/input.csv" "$BASE_URL/clean-csv" > "$TMP/first.json"
curl --fail --silent --show-error -H "Idempotency-Key: $KEY" -F "file=@$TMP/input.csv" "$BASE_URL/clean-csv" > "$TMP/replay.json"
curl --fail --silent --show-error "$BASE_URL/usage" > "$TMP/usage.json"
python3 - "$TMP" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
h,f,r,u=[json.loads((p/name).read_text()) for name in ['health.json','first.json','replay.json','usage.json']]
assert h['status']=='ok'
assert f['rows']==[{'order_id':'001','name':'Demo'}],f
assert f['billing']['charged_micro_usd']==1000,f
assert f['billing']['charged_usd']=='0.001000',f
assert r['billing']['charged_micro_usd']==0,r
assert r['billing']['idempotent_replay'] is True,r
assert u['billable_requests']>=1 and u['simulated_total_micro_usd']==u['billable_requests']*1000,u
assert u['idempotent_replays']>=1,u
print('PASS: live HTTP health, cleaned JSON, USD 0.001 simulation, free idempotent retry, usage aggregate')
print(json.dumps({'first_response':f,'replay_billing':r['billing'],'usage':u},indent=2))
PY
