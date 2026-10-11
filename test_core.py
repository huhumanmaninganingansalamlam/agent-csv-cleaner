"""Regression tests; generated data is entirely synthetic."""
import contextlib
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import csv_cleanup as kit


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.csv'
        self.output = self.root / 'clean.csv'
        self.report = self.root / 'report.json'

    def source_text(self, text, encoding='utf-8'):
        self.source.write_bytes(text.encode(encoding))

    def run_cli(self, *args, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = kit.main(list(map(str, args)))
        self.assertEqual(code, expected, stderr.getvalue())
        return json.loads(stdout.getvalue()) if code == 0 else stderr.getvalue()

    def clean(self, *args, expected=0):
        return self.run_cli('clean', self.source, '--output', self.output, *args, expected=expected)

    def output_rows(self):
        with self.output.open(encoding='utf-8-sig', newline='') as f:
            return list(csv.reader(f))

    def test_unicode_bom(self):
        self.source_text('상품,수량\n커피,2\n', 'utf-8-sig')
        self.clean()
        self.assertEqual(self.output_rows(), [['상품', '수량'], ['커피', '2']])
        self.assertTrue(self.output.read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_cp949(self):
        self.source_text('상품,수량\n커피,2\n', 'cp949')
        self.clean('--encoding', 'cp949')
        self.assertEqual(self.output_rows()[1][0], '커피')

    def test_utf16(self):
        self.source_text('id,name\n001,커피\n', 'utf-16')
        self.clean('--encoding', 'utf-16')
        self.assertEqual(self.output_rows()[1][0], '001')

    def test_quotes_commas_multiline(self):
        self.source_text('id,note\n001,"a,b"\n002,"line1\nline2"\n003,"say ""hi"""\n')
        report = self.clean()
        self.assertEqual(report['output_records'], 3)
        self.assertEqual(self.output_rows()[2][1], 'line1\nline2')
        self.assertEqual(self.output_rows()[3][1], 'say "hi"')

    def test_normalize_collisions(self):
        self.source_text('A,A,a_2,,상품 이름\n1,2,3,4,5\n')
        self.clean('--normalize-headers')
        self.assertEqual(self.output_rows()[0], ['a', 'a_2', 'a_2_2', 'column_4', '상품_이름'])

    def test_duplicate_headers_refused(self):
        self.source_text('a,a\n1,2\n')
        self.clean(expected=2)
        self.assertFalse(self.output.exists())

    def test_blank_header_refused(self):
        self.source_text('a, \n1,2\n')
        self.clean(expected=2)

    def test_trim_is_opt_in(self):
        self.source_text('id,name\n001, x \n')
        self.clean()
        self.assertEqual(self.output_rows()[1], ['001', ' x '])

    def test_trim_before_dedupe(self):
        self.source_text('id,name\n001, x \n001,x\n')
        r = self.clean('--trim', '--dedupe', 'exact')
        self.assertEqual(r['duplicate_records_removed'], 1)
        self.assertEqual(r['cells_trimmed'], 1)

    def test_exact_dedupe_first(self):
        self.source_text('id,name\n001,x\n001,x\n002,y\n')
        r = self.clean('--dedupe', 'exact')
        self.assertEqual(r['removed_record_numbers'], [3])
        self.assertEqual(self.output_rows()[1:], [['001', 'x'], ['002', 'y']])

    def test_key_conflicts_retained(self):
        self.source_text('id,name\n001,x\n001,y\n001,x\n')
        r = self.clean('--dedupe', 'keys', '--keys', 'id')
        self.assertEqual(r['key_conflict_groups'], 1)
        self.assertEqual(r['key_conflict_records'], [[2, 3, 4]])
        self.assertEqual(self.output_rows()[1:], [['001', 'x'], ['001', 'y']])

    def test_composite_keys_case_sensitive(self):
        self.source_text('id,sku,value\n001,A,x\n001,a,y\n001,A,z\n')
        r = self.clean('--dedupe', 'keys', '--keys', 'id', 'sku')
        self.assertEqual(r['key_conflict_records'], [[2, 4]])
        self.assertEqual(r['output_records'], 3)

    def test_keys_required(self):
        self.source_text('id\n1\n')
        self.clean('--dedupe', 'keys', expected=2)

    def test_keys_without_mode_rejected(self):
        self.source_text('id\n1\n')
        self.clean('--keys', 'id', expected=2)

    def test_unknown_column(self):
        self.source_text('id\n1\n')
        self.clean('--required', 'missing', expected=2)

    def test_validation_retains_values(self):
        self.source_text('id,amount\n,NaN\n001,Infinity\n002,hello\n003,1.25\n004,-2\n')
        r = self.clean('--required', 'id', '--numeric', 'amount', '--report', self.report)
        self.assertEqual(r['validation_issue_count'], 4)
        self.assertEqual(r['output_records'], 5)
        self.assertEqual(json.loads(self.report.read_text())['validation_issue_count'], 4)
        self.assertNotIn('hello', self.report.read_text())

    def test_malformed_default_aborts(self):
        self.source_text('a,b\n1,2\n3\n4,5,6\n')
        self.clean(expected=2)
        self.assertFalse(self.output.exists())

    def test_malformed_skip(self):
        self.source_text('a,b\n1,2\n3\n4,5,6\n')
        r = self.clean('--malformed', 'skip')
        self.assertEqual(r['malformed_records_skipped'], 2)
        self.assertEqual(r['output_records'], 1)

    def test_broken_quote_always_aborts(self):
        self.source_text('a,b\n1,"unterminated\n')
        self.clean('--malformed', 'skip', expected=2)
        self.assertFalse(self.output.exists())

    def test_trailing_junk_after_quote(self):
        self.source_text('a,b\n1,"hi"junk\n')
        self.clean(expected=2)

    def test_dry_run_no_writes(self):
        self.source_text('id\n001\n')
        before = set(self.root.iterdir())
        r = self.clean('--report', self.report, '--dry-run')
        self.assertTrue(r['dry_run'])
        self.assertEqual(before, set(self.root.iterdir()))

    def test_never_overwrite_input(self):
        self.source_text('id\n001\n')
        before = self.source.read_bytes()
        self.run_cli('clean', self.source, '--output', self.source, expected=2)
        self.assertEqual(before, self.source.read_bytes())

    def test_existing_output(self):
        self.source_text('id\n001\n')
        self.output.write_text('KEEP')
        self.clean(expected=2)
        self.assertEqual(self.output.read_text(), 'KEEP')

    def test_existing_report_blocks_all(self):
        self.source_text('id\n001\n')
        self.report.write_text('KEEP')
        self.clean('--report', self.report, expected=2)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.report.read_text(), 'KEEP')

    def test_output_report_same_path(self):
        self.source_text('id\n001\n')
        self.clean('--report', self.output, expected=2)

    def test_symlink_input_alias(self):
        self.source_text('id\n001\n')
        self.output.symlink_to(self.source)
        self.clean(expected=2)
        self.assertEqual(self.source.read_text(), 'id\n001\n')

    def test_broken_symlink_refused(self):
        self.source_text('id\n001\n')
        self.output.symlink_to(self.root / 'missing')
        self.clean(expected=2)

    def test_delimiter_conversion(self):
        self.source_text('id;name\n001;테스트\n')
        self.clean('--delimiter', 'semicolon', '--output-delimiter', 'tab')
        self.assertIn('001\t테스트', self.output.read_text(encoding='utf-8-sig'))

    def test_output_encoding_failure_no_files(self):
        self.source_text('id,name\n001,테스트\n')
        self.clean('--output-encoding', 'ascii', '--report', self.report, expected=2)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_wrong_encoding(self):
        self.source_text('이름\n테스트\n', 'cp949')
        self.clean(expected=2)

    def test_empty_input(self):
        self.source_text('')
        self.clean(expected=2)

    def test_header_only(self):
        self.source_text('id,name\n')
        self.assertEqual(self.clean()['output_records'], 0)

    def test_formula_default_preserved(self):
        self.source_text('id,note\n001,=1+1\n002,@SUM(A1)\n')
        r = self.clean()
        self.assertEqual(r['formula_like_cells'], 2)
        self.assertEqual(self.output_rows()[1][1], '=1+1')

    def test_formula_safe_opt_in(self):
        self.source_text('id,note\n001,=1+1\n002,  +CMD\n003,-2\n004,\tvalue\n')
        r = self.clean('--spreadsheet-safe')
        self.assertEqual(r['spreadsheet_cells_prefixed'], 4)
        self.assertEqual(self.output_rows()[1][1], "'=1+1")

    def test_inspect_no_row_values(self):
        self.source_text('id,name\n001,SECRET_SAMPLE\n001,SECRET_SAMPLE\n')
        r = self.run_cli('inspect', self.source)
        self.assertNotIn('SECRET_SAMPLE', json.dumps(r))
        self.assertEqual(r['exact_duplicate_records'], 1)
        self.assertFalse(self.output.exists())

    def test_size_limit(self):
        self.source_text('id\n001\n')
        with mock.patch.object(kit, 'MAX_BYTES', 2):
            self.clean(expected=2)

    def test_invalid_encoding_name(self):
        self.source_text('id\n001\n')
        self.clean('--encoding', 'not-an-encoding', expected=2)

    def test_null_character(self):
        self.source_text('id\na\x00b\n')
        self.clean(expected=2)

    def test_missing_parent(self):
        self.source_text('id\n001\n')
        self.run_cli('clean', self.source, '--output', self.root / 'absent' / 'x.csv', expected=2)

    def test_atomic_publish_conflict(self):
        self.source_text('id\n001\n')
        original_link = kit.os.link
        calls = []
        def fail_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError('simulated report write failure')
            return original_link(src, dst)
        with mock.patch.object(kit.os, 'link', side_effect=fail_second):
            self.clean('--report', self.report, expected=2)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_detail_limit(self):
        self.source_text('id\n' + '\n'.join(['001'] * 150) + '\n')
        r = self.clean('--dedupe', 'exact')
        self.assertEqual(r['duplicate_records_removed'], 149)
        self.assertEqual(len(r['removed_record_numbers']), 100)


if __name__ == '__main__':
    unittest.main()
