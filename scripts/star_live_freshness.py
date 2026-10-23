#!/usr/bin/env python3
"""Bounded post-collection Star API check; fails closed on count/first-page drift.

The public API is offset-paginated and has no snapshot token. A successful
check verifies partition counts and first-page evidence, NOT atomic parity of
all rows at a single instant.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import time
import sys
import collect_star as star


class FreshnessMismatch(RuntimeError):
    pass


def check(report, root, action, page_size=10, delay=0.15, page_reader=None):
    if report.get('status') != 'ALL_216_QUERIES_STAGED_VALIDATED' or report.get('complete') is not True:
        raise FreshnessMismatch('No complete source capture to compare')
    queries = report.get('queries')
    if not isinstance(queries, dict) or len(queries) != 216:
        raise FreshnessMismatch('Not all 216 partitions were captured')
    page_reader = page_reader or star.page
    checked = 0
    mismatches = []
    for key, audit in sorted(queries.items()):
        category, _ = key.split('|', 1)
        filters = audit.get('source_state_filter')
        if isinstance(filters, str):
            filters = [filters]
        if not isinstance(filters, list) or not filters or not all(isinstance(x, str) and x for x in filters):
            mismatches.append({'query': key, 'error': 'missing source state filter provenance'})
            continue
        total_now = 0
        errors = []
        for source_state in filters:
            try:
                rows, count, pages = page_reader(action, category, source_state, 1, page_size)
                if len(rows) != min(count, page_size):
                    raise FreshnessMismatch('first page truncated')
                total_now += count
                if count:
                    first_path = root / 'checkpoints' / star.checkpoint_key(category, source_state) / 'page_0001.json'
                    if not first_path.is_file():
                        raise FreshnessMismatch('missing page-one capture checkpoint')
                    captured = json.loads(first_path.read_text(encoding='utf-8'))
                    if captured.get('page') != 1 or captured.get('count') != count:
                        raise FreshnessMismatch('first-page stored count mismatch')
                    if star.digest(rows) != star.digest(captured.get('rows')):
                        raise FreshnessMismatch('first-page membership or fields changed')
            except Exception as exc:
                errors.append({'filter': source_state, 'error': type(exc).__name__ + ': ' + str(exc)[:170]})
            if delay > 0:
                time.sleep(delay)
        checked += 1
        if total_now != audit.get('count') or errors:
            mismatches.append({'query': key, 'previous_count': audit.get('count'),
                               'current_count': total_now, 'errors': errors})
        if checked % 36 == 0:
            print(f'STAR_LIVE_RECHECK {checked}/216 partitions, changed={len(mismatches)}', flush=True)
    return {'kind': 'STAR_LIVE_POST_CAPTURE_AUDIT',
            'checked_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'complete_queries': checked,
            'method': 'Live state/category count plus exact first-page comparison to captured checkpoint',
            'limitation': 'Not a frozen or atomic nationwide snapshot; same-count changes to unprobed pages remain possible',
            'matching': len(mismatches) == 0,
            'mismatches': mismatches}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', type=Path, default=Path('build/star'))
    p.add_argument('--page-size', type=int, default=10)
    p.add_argument('--delay', type=float, default=0.15)
    args = p.parse_args()
    if args.page_size != 10 or args.delay < 0:
        p.error('Expected verified page size of 10 and non-negative delay')
    report = json.loads((args.input / 'star_audit.json').read_text(encoding='utf-8'))
    action, _ = star.base.get_live_action_id()
    result = check(report, args.input, action, args.page_size, args.delay)
    result_path = args.input / 'star_live_freshness.json'
    star.write_atomic(result_path, result)
    print('STAR_LIVE_RECHECK_RESULT matching='+str(result['matching'])+
          ' checked='+str(result['complete_queries'])+
          ' mismatches='+str(len(result['mismatches'])), flush=True)
    if result['mismatches']:
        print('STAR_LIVE_RECHECK_ERRORS '+json.dumps(result['mismatches'][:12],ensure_ascii=False)[:5000], flush=True)
        return 2
    return 0

if __name__ == '__main__':
    sys.exit(main())
