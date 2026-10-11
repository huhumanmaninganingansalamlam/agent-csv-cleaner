# Agent-to-Agent API

Version 1.0.0. A deliberately small FastAPI wrapper reusing `csv_cleanup.py` unchanged. It accepts a CSV upload and returns JSON for another agent to consume. It does not execute uploaded code, evaluate formulas, call external AI services or collect actual payments.

## Docker

From this directory:

```sh
docker build -t agent-csv-cleaner .
docker run --rm -p 8000:8000 agent-csv-cleaner
```

The requested `-p 8000:8000` syntax may expose the port beyond your computer. For a local-only demo, prefer:

```sh
docker run --rm -p 127.0.0.1:8000:8000 agent-csv-cleaner
```

To persist simulated usage and retry keys across container replacement, use the supplied Compose file. It binds to localhost and creates a named data volume:

```sh
docker compose up --build
# Stop without deleting the usage volume:
docker compose down
```

Without a mounted volume, deleting a container deletes its usage ledger. `docker compose down -v` also deletes the ledger and resets idempotency; do not do this if retry history matters.

No local Docker engine was available during preparation. See CI results for actual image-build/run verification. The image uses a non-root user. The base-image tag and top-level dependency versions are pinned by version, not immutable digest/transitive lock; revalidate before production release.

## Call from an agent or curl

```sh
curl --fail http://127.0.0.1:8000/health
curl --fail -H 'Idempotency-Key: demo-order-clean-1' \
  -F 'file=@examples/orders_messy.csv' \
  -F 'dedupe=keys' -F 'keys=order_id' -F 'trim=true' \
  http://127.0.0.1:8000/clean-csv
curl --fail http://127.0.0.1:8000/usage
```

`POST /clean-csv` returns `columns`, `rows` (objects containing string values), `audit`, and `billing`. Leading zeros remain strings, such as `"001"`. Normalized headers are unique. Exact duplicate rows are removed by default; rows sharing selected keys but differing elsewhere are preserved and reported. Blank values and formula-like text are not evaluated or automatically repaired.

### Multipart form options

- `file`: required CSV upload, at most 2 MiB
- `encoding`: `utf-8-sig` (default), `utf-8`, `cp949`, or `utf-16`
- `delimiter`: `comma` (default), `tab`, `semicolon`, `pipe`, or one supported character
- `normalize_headers`: `true` by default; false rejects blank/duplicate headers
- `trim`: `false` by default; true removes surrounding whitespace
- `dedupe`: `exact` (default), `none`, or `keys`
- `keys`: comma-separated column names, only with `dedupe=keys`; use normalized names when normalization is enabled

Malformed CSV is rejected rather than silently skipped. Key matching is case-sensitive. JSON output is not an Excel workbook. Formula-like strings remain text in JSON; use appropriate protection before sending them to a spreadsheet application. The audit's `dry_run: true` means the core did not export a CSV file; the API still returns transformed rows and meters the request.

### Exactly USD 0.001 simulated usage

A successful, previously unseen request adds **1,000 integer micro-USD**, exactly USD 0.001, to a local SQLite ledger. No payment provider, invoice, subscription, wallet, token or real charge exists.

Include a stable `Idempotency-Key` header when retrying the same bytes and options. The first successful request incurs 1,000 simulated micro-USD; subsequent identical requests using that key return zero additional simulated charge. Reusing a key with different file bytes/options returns HTTP 409. Keys must contain 1–128 printable ASCII characters without spaces. Without a key each successful call is metered separately.

The ledger stores only a hash of the key, a fingerprint of file bytes/options, a timestamp, integer amounts and replay counts. It does not store original filenames, CSV contents, customer values or raw keys. Uploads live briefly in a temporary file and are removed after processing. Cleanup results are recomputed on replay, so preserve the same application version if byte-identical responses matter. The ledger persists idempotency across restarts only while its database is retained.

HTTP 4xx/5xx validation, capacity and processing failures are not billed. A server-side completed operation may have been metered even if a client disconnects before seeing the response; retry with the same key. `GET /usage` is a read-only aggregate of successful responses, billable requests, idempotent replays and simulated totals. It is not a full failed-request access log. Runtime access logging is disabled in the Docker command.

## Limits and errors

- Entire multipart request: 2 MiB + 64 KiB; CSV part: 2 MiB
- At most 10,000 data rows, 64 columns; parser field limit 131,072 characters
- Two concurrent cleanup requests per process; excess requests get HTTP 429
- 30-second total body upload timeout (HTTP 408)
- Oversized data: HTTP 413; invalid CSV/options: HTTP 422; conflicting retry key: HTTP 409
- Ledger unavailable: HTTP 503
- Audit details are capped at 100; summary counts cover all records

Records mean logical CSV records; quoted multiline fields count as one record. Memory use exceeds input size because parsing and JSON generation retain copies. The concurrency limit is per process, so use one worker for this demo. A large multipart field may be rejected earlier by the framework.

## Local development and tests

Requires Python 3.10 or newer; verified with Linux Python 3.12.14. Installing dependencies requires access to their package registry; runtime cleanup itself is offline.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest -v test_api test_core
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-access-log
# In another terminal:
sh smoke_test.sh
```

Set `CSV_USAGE_DB` to an existing writable directory's database path if needed. Default direct-run database: `/tmp/agent-csv-usage.sqlite3`; Docker default: `/data/usage.sqlite3`.

CI installs dependencies, runs tests, builds the Docker image, starts a container, uses curl to verify JSON and billing/replay behavior, saves the image as an artifact after success, and cleans up the container. It does not publish to a container registry or deploy a public endpoint.

## Before any shared/public deployment

This is a local demonstration, not a production multi-tenant paid API. It has no authentication, TLS, tenant isolation, payment processing, quotas or account management. `/usage` exposes aggregate service activity to anyone who can reach it, and idempotency keys share one namespace. Do not expose it publicly without configuring authentication/TLS and a tenant-scoped billing design, gateway limits, monitoring, database backup/retention and an appropriate privacy policy. Disk usage grows with successful calls. Never place credentials or real customer uploads in a repository.

## 한국어 요약

CSV 파일을 보내면 정리된 JSON과 점검 결과를 받는 에이전트용 API입니다. 성공한 새 요청당 USD 0.001을 로컬 장부에 모의 기록하며 실제 결제는 없습니다. 같은 파일·옵션을 재시도할 때 동일한 `Idempotency-Key`를 사용하면 중복 과금 기록을 방지합니다. 오류 응답에는 모의 요금이 부과되지 않습니다. 현재 인증·TLS·사용자별 격리가 없으므로 로컬에서만 실행하고 공개 배포 전 보안 설정이 필요합니다.
