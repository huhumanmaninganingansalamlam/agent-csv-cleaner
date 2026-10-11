#!/usr/bin/env python3
"""Offline, conservative CSV inspection and cleanup. Version 1.0.0."""
import argparse
import codecs
import csv
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from decimal import Decimal, InvalidOperation

__version__ = '1.0.0'
MAX_BYTES = 50 * 1024 * 1024
MAX_DETAILS = 100


class CleanupError(Exception):
    """An actionable input or output error."""


def delimiter(value):
    value = {'comma': ',', 'tab': '\t', 'semicolon': ';', 'pipe': '|'}.get(value, value)
    if len(value) != 1 or value in '\r\n\x00"':
        raise argparse.ArgumentTypeError('Delimiter must be one character (not quote/newline/NUL), or comma/tab/semicolon/pipe.')
    return value


def normalized_headers(headers):
    used = set()
    result = []
    for index, header in enumerate(headers, 1):
        base = re.sub(r'[^\w]+', '_', unicodedata.normalize('NFKC', header).strip().lower()).strip('_') or f'column_{index}'
        name, suffix = base, 2
        while name in used:
            name = f'{base}_{suffix}'
            suffix += 1
        used.add(name)
        result.append(name)
    return result


def read_csv(path, encoding, sep, malformed='error'):
    try:
        codecs.lookup(encoding)
        if path.stat().st_size > MAX_BYTES:
            raise CleanupError('Input exceeds the 50 MiB limit. Split it into smaller files first.')
        with path.open('r', encoding=encoding, newline='') as stream:
            # The capped read also handles a file that grows after stat().
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CleanupError('Decoded input exceeds the supported limit.')
        if '\x00' in raw:
            raise CleanupError('NUL characters found. Check encoding; UTF-16 input needs --encoding utf-16.')
        reader = csv.reader(io.StringIO(raw, newline=''), delimiter=sep, strict=True)
        headers = next(reader, None)
        if headers is None or not headers:
            raise CleanupError('CSV is empty or has an empty header record.')
        headers[0] = headers[0].lstrip('\ufeff')
        rows, skipped = [], []
        for record, row in enumerate(reader, 2):
            if len(row) != len(headers):
                issue = {'record': record, 'end_line': reader.line_num, 'fields': len(row), 'expected': len(headers)}
                if malformed == 'error':
                    raise CleanupError(f'Record {record} ending at line {reader.line_num} has {len(row)} fields; expected {len(headers)}. No output written. Use --malformed skip only after review.')
                skipped.append(issue)
            else:
                rows.append((record, row))
        return headers, rows, skipped
    except (OSError, UnicodeError, LookupError, csv.Error) as exc:
        raise CleanupError(f'Cannot read CSV: {exc}') from exc


def formula_like(value):
    return bool(value) and (value[0] in '\t\r\n' or value.lstrip().startswith(('=', '+', '-', '@')))


def summarize(headers, rows, skipped):
    seen = set()
    duplicates = 0
    for _, row in rows:
        key = tuple(row)
        duplicates += key in seen
        seen.add(key)
    return {
        'version': __version__, 'headers': headers, 'columns': len(headers),
        'valid_records': len(rows), 'exact_duplicate_records': duplicates,
        'duplicate_headers': sorted({h for h in headers if headers.count(h) > 1}),
        'blank_header_positions': [i + 1 for i, h in enumerate(headers) if not h.strip()],
        'malformed_records_skipped': len(skipped), 'malformed_details': skipped[:MAX_DETAILS],
        'formula_like_cells': sum(formula_like(v) for _, row in rows for v in row),
        'detail_limit': MAX_DETAILS,
        'record_numbering': 'Logical CSV records; header is record 1. Quoted multiline fields count as one record.'
    }


def indexes(names, headers, label):
    missing = [name for name in names if name not in headers]
    if missing:
        raise CleanupError(f'Unknown {label} column(s): {", ".join(missing)}. Use names shown by inspect, or normalized names with --normalize-headers.')
    return [headers.index(name) for name in names]


def clean_data(headers, rows, skipped, args):
    source_headers = list(headers)
    headers = normalized_headers(headers) if args.normalize_headers else list(headers)
    if len(set(headers)) != len(headers) or any(not h.strip() for h in headers):
        raise CleanupError('Duplicate or blank headers require --normalize-headers. Inspect the header mapping before using the export.')
    key_indexes = indexes(args.keys, headers, 'key')
    required = indexes(args.required, headers, 'required')
    numeric = indexes(args.numeric, headers, 'numeric')
    if args.dedupe == 'keys' and not key_indexes:
        raise CleanupError('--dedupe keys requires --keys COLUMN [COLUMN ...].')
    if args.keys and args.dedupe != 'keys':
        raise CleanupError('--keys requires --dedupe keys.')
    report = summarize(source_headers, rows, skipped)
    report['header_mapping'] = [{'position': i + 1, 'from': old, 'to': new} for i, (old, new) in enumerate(zip(source_headers, headers))]
    kept, seen, groups, invalid, removed = [], set(), {}, [], []
    trimmed = 0
    for record, original in rows:
        row = [v.strip() for v in original] if args.trim else list(original)
        trimmed += sum(a != b for a, b in zip(original, row))
        for i in required:
            if not row[i].strip():
                invalid.append({'record': record, 'column': headers[i], 'issue': 'required_value_missing'})
        for i in numeric:
            try:
                if not row[i].strip() or not Decimal(row[i].strip()).is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                invalid.append({'record': record, 'column': headers[i], 'issue': 'not_finite_number'})
        full_key = tuple(row)
        if args.dedupe == 'keys':
            key = tuple(row[i] for i in key_indexes)
            groups.setdefault(key, {}).setdefault(full_key, []).append(record)
        if args.dedupe and full_key in seen:
            removed.append(record)
            continue
        seen.add(full_key)
        kept.append(row)
    conflicts = [sorted(n for records in variants.values() for n in records) for variants in groups.values() if len(variants) > 1]
    changed = 0
    if args.spreadsheet_safe:
        for row in kept:
            for i, value in enumerate(row):
                if formula_like(value):
                    row[i] = "'" + value
                    changed += 1
        for i, value in enumerate(headers):
            if formula_like(value):
                headers[i] = "'" + value
                changed += 1
    report.update({
        'output_headers': headers, 'output_records': len(kept),
        'duplicate_records_removed': len(removed), 'removed_record_numbers': removed[:MAX_DETAILS],
        'key_conflict_groups': len(conflicts), 'key_conflict_records': [r[:MAX_DETAILS] for r in conflicts[:MAX_DETAILS]],
        'key_conflict_policy': 'Keep differing rows; only identical full rows are removed.',
        'validation_issue_count': len(invalid), 'validation_issues': invalid[:MAX_DETAILS],
        'validation_policy': 'Report only; invalid values are retained unchanged.',
        'cells_trimmed': trimmed, 'spreadsheet_cells_prefixed': changed,
        'dry_run': args.dry_run,
        'settings': {k: getattr(args, k) for k in ('encoding', 'delimiter', 'output_encoding', 'output_delimiter', 'normalize_headers', 'trim', 'dedupe', 'keys', 'required', 'numeric', 'malformed', 'spreadsheet_safe')}
    })
    return headers, kept, report


def ensure_destinations(input_path, destinations):
    resolved = [p.resolve() for p in destinations]
    if input_path.resolve() in resolved or len(set(resolved)) != len(resolved):
        raise CleanupError('Input, output, and report paths must all be different.')
    for path in destinations:
        if path.exists() or path.is_symlink():
            raise CleanupError(f'Refusing to overwrite existing path: {path}')
        if not path.parent.is_dir():
            raise CleanupError(f'Output folder does not exist: {path.parent}')


def write_new_files(contents):
    """Prepare first; exclusively create final files. Roll back this invocation on error."""
    staged, created = [], []
    try:
        for target, data in contents:
            fd, name = tempfile.mkstemp(prefix='.csv-cleanup-', dir=str(target.parent))
            staged.append((Path(name), target))
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for temp, target in staged:
            # Hard-link publication is atomic and refuses existing names, including symlinks.
            os.link(temp, target)
            created.append(target)
    except OSError as exc:
        for target in created:
            target.unlink(missing_ok=True)
        raise CleanupError(f'Could not create outputs safely: {exc}. Use a local folder supporting hard links.') from exc
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description='CSV Cleanup Kit: offline inspection and conservative cleanup. Existing files are never overwritten.')
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('inspect', 'clean'):
        q = sub.add_parser(command)
        q.add_argument('input', type=Path)
        q.add_argument('--encoding', default='utf-8-sig')
        q.add_argument('--delimiter', type=delimiter, default=',')
        q.add_argument('--malformed', choices=['error', 'skip'], default='error', help='Skip applies only to field-count mismatches; broken quoting always fails.')
        if command == 'clean':
            q.add_argument('--output', type=Path, required=True)
            q.add_argument('--report', type=Path)
            q.add_argument('--normalize-headers', action='store_true')
            q.add_argument('--trim', action='store_true')
            q.add_argument('--dedupe', choices=['exact', 'keys'])
            q.add_argument('--keys', nargs='+', default=[])
            q.add_argument('--required', nargs='+', default=[])
            q.add_argument('--numeric', nargs='+', default=[])
            q.add_argument('--output-encoding', default='utf-8-sig')
            q.add_argument('--output-delimiter', type=delimiter, default=',')
            q.add_argument('--dry-run', action='store_true')
            q.add_argument('--spreadsheet-safe', action='store_true')
    return p


def main(argv=None):
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(errors='backslashreplace')
    args = parser().parse_args(argv)
    try:
        headers, rows, skipped = read_csv(args.input, args.encoding, args.delimiter, args.malformed)
        if args.command == 'inspect':
            report = summarize(headers, rows, skipped)
        else:
            destinations = [args.output] + ([args.report] if args.report else [])
            ensure_destinations(args.input, destinations)
            headers, clean_rows, report = clean_data(headers, rows, skipped, args)
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer, delimiter=args.output_delimiter, lineterminator='\r\n')
            writer.writerow(headers)
            writer.writerows(clean_rows)
            try:
                encoded = buffer.getvalue().encode(args.output_encoding)
            except (UnicodeError, LookupError) as exc:
                raise CleanupError(f'Cannot encode output: {exc}. Try --output-encoding utf-8-sig.') from exc
            if not args.dry_run:
                contents = [(args.output, encoded)]
                if args.report:
                    contents.append((args.report, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8')))
                write_new_files(contents)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except CleanupError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
