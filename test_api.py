import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient
import app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = app.create_app(Path(self.tmp.name) / 'usage.sqlite3')
        self.client = TestClient(self.api)
        self.addCleanup(self.client.close)

    def post(self, content=b'id,name\n001,Demo\n001,Demo\n', key=None, **options):
        headers = {'Idempotency-Key': key} if key is not None else {}
        return self.client.post('/clean-csv', files={'file': ('input.csv', content, 'text/csv')}, data=options, headers=headers)

    def usage(self):
        return self.client.get('/usage').json()

    def test_health(self):
        self.assertEqual(self.client.get('/health').json()['status'], 'ok')

    def test_clean_json_string_ids_and_charge(self):
        r=self.post(); self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['rows'], [{'id':'001','name':'Demo'}])
        self.assertEqual(r.json()['billing']['charged_micro_usd'],1000)
        self.assertEqual(self.usage()['simulated_total_usd'],'0.001000')

    def test_replay_no_double_billing(self):
        self.post(key='retry-1'); r=self.post(key='retry-1')
        self.assertEqual(r.json()['billing']['charged_micro_usd'],0)
        self.assertEqual(self.usage()['successful_responses'],2)
        self.assertEqual(self.usage()['billable_requests'],1)

    def test_idempotency_survives_restart(self):
        self.post(key='persist')
        with TestClient(app.create_app(Path(self.tmp.name)/'usage.sqlite3')) as client:
            r=client.post('/clean-csv',files={'file':('x.csv',b'id,name\n001,Demo\n001,Demo\n')},headers={'Idempotency-Key':'persist'})
        self.assertTrue(r.json()['billing']['idempotent_replay'])

    def test_key_conflict_request_409(self):
        self.post(key='retry-1'); r=self.post(b'id\n002\n',key='retry-1')
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.usage()['simulated_total_micro_usd'],1000)

    def test_changed_options_conflict(self):
        self.post(key='k'); self.assertEqual(self.post(key='k',trim='true').status_code,409)

    def test_missing_file_free(self):
        self.assertEqual(self.client.post('/clean-csv').status_code,422)
        self.assertEqual(self.usage()['billable_requests'],0)

    def test_malformed_free(self):
        for data in [b'',b'a,b\n1\n',b'a,b\n1,"oops',b'a,b\n1,"hi"junk\n',b'a\na\x00b\n']:
            with self.subTest(data=data): self.assertEqual(self.post(data).status_code,422)
        self.assertEqual(self.usage()['billable_requests'],0)

    def test_encoding_error_422(self):
        self.assertEqual(self.post('상품\n커피\n'.encode('cp949')).status_code,422)

    def test_cp949(self):
        r=self.post('상품\n커피\n'.encode('cp949'),encoding='cp949')
        self.assertEqual(r.json()['rows'],[{'상품':'커피'}])

    def test_bom_quotes_multiline(self):
        r=self.post('\ufeffid,note\n001,"a,b\nc"\n'.encode())
        self.assertEqual(r.json()['rows'][0]['note'],'a,b\nc')

    def test_key_conflicting_rows_retained(self):
        r=self.post(b'id,value\n001,a\n001,b\n001,a\n',dedupe='keys',keys='id')
        self.assertEqual(len(r.json()['rows']),2)
        self.assertEqual(r.json()['audit']['key_conflict_groups'],1)

    def test_duplicate_headers_normalized(self):
        r=self.post(b'a,a\n1,2\n')
        self.assertEqual(r.json()['columns'],['a','a_2'])

    def test_duplicate_headers_without_normalization(self):
        self.assertEqual(self.post(b'a,a\n1,2\n',normalize_headers='false').status_code,422)

    def test_invalid_options(self):
        for opts in [{'encoding':'invalid'}, {'delimiter':'long'}, {'dedupe':'bad'}, {'dedupe':'keys'}, {'keys':'id'}, {'trim':'notbool'}]:
            with self.subTest(opts=opts): self.assertEqual(self.post(**opts).status_code,422)
        self.assertEqual(self.usage()['billable_requests'],0)

    def test_unknown_key(self):
        self.assertEqual(self.post(dedupe='keys',keys='missing').status_code,422)

    def test_key_limit(self):
        self.assertEqual(self.post(key='a'*129).status_code,422)

    def test_file_limit(self):
        self.assertEqual(self.post(b'a\n'+b'x'*(app.MAX_BYTES)).status_code,413)
        self.assertEqual(self.usage()['billable_requests'],0)

    def test_request_limit(self):
        self.assertEqual(self.post(b'a'*(app.MAX_BYTES+100000)).status_code,413)

    def test_rows_limit(self):
        self.assertEqual(self.post(b'a\n'+b'1\n'*(app.MAX_ROWS+1)).status_code,413)

    def test_columns_limit(self):
        self.assertEqual(self.post((','.join('c'+str(i) for i in range(65))+'\n').encode()).status_code,413)

    def test_busy_429_free(self):
        with mock.patch.object(app.SLOTS,'acquire',return_value=False):
            self.assertEqual(self.post().status_code,429)
        self.assertEqual(self.usage()['billable_requests'],0)

    def test_ledger_concurrent_retry(self):
        ledger=self.api.state.ledger
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _: ledger.success('keyhash','fingerprint'),range(16)))
        self.assertEqual(sum(x[1] for x in results),1000)
        self.assertEqual(ledger.usage()['billable_requests'],1)

    def test_ledger_failure_503(self):
        with mock.patch.object(self.api.state.ledger,'success',side_effect=app.sqlite3.OperationalError('fail')):
            self.assertEqual(self.post().status_code,503)
        self.assertEqual(self.usage()['billable_requests'],0)

    def test_logs_exclude_data(self):
        self.post(b'id,name\n001,PRIVATE_SYNTHETIC_TEXT\n',key='PRIVATE_KEY')
        raw=(Path(self.tmp.name)/'usage.sqlite3').read_bytes()
        self.assertNotIn(b'PRIVATE_SYNTHETIC_TEXT',raw)
        self.assertNotIn(b'PRIVATE_KEY',raw)

    def test_large_ledger_total_exact_decimal(self):
        ledger = self.api.state.ledger
        with ledger.connect() as db:
            db.execute("INSERT INTO requests(at_utc,reused,micro_usd) VALUES ('test',0,?)", (9_007_199_254_740_996_000,))
        self.assertEqual(ledger.usage()['simulated_total_usd'], '9007199254740.996000')

    def test_no_dedupe(self):
        self.assertEqual(len(self.post(dedupe='none').json()['rows']),2)

    def test_formula_string_never_evaluated(self):
        self.assertEqual(self.post(b'id,note\n001,=1+1\n').json()['rows'][0]['note'],'=1+1')


if __name__=='__main__': unittest.main()
