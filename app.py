"""Agent-to-Agent API. Local demo; billing is simulated, never a payment."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
import csv_cleanup as csv_core

VERSION = '1.0.0'
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 64
MAX_CONCURRENT = 2
PRICE_MICRO_USD = 1000  # Exactly USD 0.001, not an actual charge.
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS charges (key_hash TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, at_utc TEXT NOT NULL, micro_usd INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, at_utc TEXT NOT NULL, reused INTEGER NOT NULL, micro_usd INTEGER NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def success(self, key_hash, fingerprint):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM charges WHERE key_hash=?', (key_hash,)).fetchone()
            if old and old[0] != fingerprint:
                raise HTTPException(409, 'Idempotency-Key was used with different input or options.')
            reused = old is not None
            now = datetime.now(timezone.utc).isoformat()
            amount = 0 if reused else PRICE_MICRO_USD
            if not reused:
                db.execute('INSERT INTO charges VALUES (?, ?, ?, ?)', (key_hash, fingerprint, now, amount))
            db.execute('INSERT INTO requests(at_utc,reused,micro_usd) VALUES (?,?,?)', (now, int(reused), amount))
            return reused, amount

    def usage(self):
        with self.connect() as db:
            requests = db.execute('SELECT COUNT(*), COALESCE(SUM(reused),0), COALESCE(SUM(micro_usd),0) FROM requests').fetchone()
            billed = db.execute('SELECT COUNT(*) FROM charges').fetchone()[0]
        return {'successful_responses': requests[0], 'idempotent_replays': requests[1], 'billable_requests': billed,
                'simulated_total_micro_usd': requests[2], 'simulated_total_usd': f'{requests[2] // 1_000_000}.{requests[2] % 1_000_000:06d}',
                'unit_price_micro_usd': PRICE_MICRO_USD, 'currency': 'USD', 'billing_mode': 'simulation',
                'failed_requests_billed': 0}


def process(data, options):
    # The file exists only in an automatically cleaned temporary directory.
    with tempfile.TemporaryDirectory(prefix='agent-csv-') as folder:
        path = Path(folder) / 'input.csv'
        path.write_bytes(data)
        try:
            headers, rows, skipped = csv_core.read_csv(path, options['encoding'], options['delimiter'])
            if len(headers) > MAX_COLUMNS:
                raise HTTPException(413, f'Maximum {MAX_COLUMNS} columns.')
            if len(rows) > MAX_ROWS:
                raise HTTPException(413, f'Maximum {MAX_ROWS} data rows.')
            args = argparse.Namespace(**options, required=[], numeric=[], malformed='error', output_encoding='utf-8-sig', output_delimiter=',', dry_run=True, spreadsheet_safe=False)
            headers, cleaned, report = csv_core.clean_data(headers, rows, skipped, args)
        except csv_core.CleanupError as exc:
            # No file bytes or submitted filenames are included in errors/logs.
            raise HTTPException(422, str(exc)) from exc
    return {'columns': headers, 'rows': [dict(zip(headers, row)) for row in cleaned], 'audit': report}


class BoundedUploadMiddleware:
    """Bound complete multipart bodies before parsing, including chunked uploads."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/clean-csv' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        if not SLOTS.acquire(blocking=False):
            return await JSONResponse({'detail': 'Busy: retry with the same Idempotency-Key.'}, status_code=429)(scope, receive, send)
        try:
            chunks, total = [], 0
            async def collect():
                nonlocal total
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return False
                    chunk = message.get('body', b'')
                    total += len(chunk)
                    if total > MAX_BYTES + 65536:
                        raise OverflowError
                    chunks.append(chunk)
                    if not message.get('more_body', False):
                        return True
            try:
                connected = await asyncio.wait_for(collect(), timeout=30)
            except OverflowError:
                return await JSONResponse({'detail': 'Multipart request exceeds size limit.'}, status_code=413)(scope, receive, send)
            except asyncio.TimeoutError:
                return await JSONResponse({'detail': 'Upload timed out.'}, status_code=408)(scope, receive, send)
            if not connected:
                return
            body = b''.join(chunks)
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await receive()
            return await self.app(scope, replay, send)
        finally:
            SLOTS.release()


def create_app(db_path=None):
    api = FastAPI(title='Agent-to-Agent API: CSV Cleaner', version=VERSION,
                  description='Local demo. USD 0.001 simulated billing per successful unique request. No payment is collected.')
    ledger = Ledger(db_path or os.environ.get('CSV_USAGE_DB', '/tmp/agent-csv-usage.sqlite3'))
    api.state.ledger = ledger
    api.add_middleware(BoundedUploadMiddleware)

    @api.get('/health')
    def health():
        return {'status': 'ok', 'version': VERSION, 'billing_mode': 'simulation'}

    @api.get('/usage')
    def usage():
        try:
            return ledger.usage()
        except sqlite3.Error as exc:
            raise HTTPException(503, 'Usage ledger unavailable.') from exc

    @api.post('/clean-csv')
    async def clean_csv(file: UploadFile = File(...), encoding: str = Form('utf-8-sig'),
                        delimiter: str = Form('comma'), trim: bool = Form(False),
                        normalize_headers: bool = Form(True), dedupe: str = Form('exact'),
                        keys: str = Form(''), idempotency_key: str | None = Header(None, alias='Idempotency-Key')):
        if encoding not in {'utf-8', 'utf-8-sig', 'cp949', 'utf-16'}:
            raise HTTPException(422, 'encoding must be utf-8, utf-8-sig, cp949, or utf-16.')
        if dedupe not in {'none', 'exact', 'keys'}:
            raise HTTPException(422, 'dedupe must be none, exact, or keys.')
        if len(keys) > 1024:
            raise HTTPException(422, 'keys is too long.')
        if idempotency_key is not None and (not 1 <= len(idempotency_key) <= 128 or not idempotency_key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)):
            raise HTTPException(422, 'Idempotency-Key must contain 1–128 printable ASCII characters without spaces.')
        try:
            sep = csv_core.delimiter(delimiter)
        except argparse.ArgumentTypeError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = await file.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, 'CSV file exceeds 2 MiB.')
            options = {'encoding': encoding, 'delimiter': sep, 'trim': trim, 'normalize_headers': normalize_headers,
                       'dedupe': None if dedupe == 'none' else dedupe, 'keys': [k.strip() for k in keys.split(',') if k.strip()]}
            fingerprint = hashlib.sha256(data + b'\x00' + json.dumps(options, sort_keys=True).encode()).hexdigest()
            key = idempotency_key or str(uuid.uuid4())
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            result = await run_in_threadpool(process, data, options)
            try:
                reused, amount = await run_in_threadpool(ledger.success, key_hash, fingerprint)
            except sqlite3.Error as exc:
                raise HTTPException(503, 'Usage ledger unavailable; request not charged. Retry with the same Idempotency-Key.') from exc
            result['billing'] = {'mode': 'simulation', 'currency': 'USD', 'unit_price_micro_usd': PRICE_MICRO_USD,
                                 'charged_micro_usd': amount, 'charged_usd': '0.000000' if reused else '0.001000', 'idempotent_replay': reused}
            return result
        finally:
            await file.close()

    return api


app = create_app()
