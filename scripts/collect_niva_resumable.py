#!/usr/bin/env python3
"""Bounded, resumable official Niva Bupa network collection.

Never publishes a partial snapshot. Every page URL comes from the source's next
link. Checkpoints are source evidence, not an assertion of nationwide freshness.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import collect_niva as base

MAX_AGE_HOURS = 24

class SourceEpochChanged(base.SourceError):
    pass

def utc():
    return datetime.now(timezone.utc).isoformat()

def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(path)

def pfile(cp, n):
    return cp / 'pages' / f'page_{n:06d}.json'

def fresh_age_ok(state, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(state['started_utc'])
        return 0 <= (now - d).total_seconds() <= MAX_AGE_HOURS * 3600
    except (KeyError, TypeError, ValueError, OverflowError):
        return False

def read_pages(cp, state):
    """Rebuild identities and verify all saved links, not just final page count."""
    ids = set()
    all_rows = []
    pages = []
    current_url = base.INITIAL
    for n in range(1, state['pages_captured'] + 1):
        path = pfile(cp, n)
        obj = json.loads(path.read_text(encoding='utf8'))
        if obj['page'] != n or obj['url'] != current_url:
            raise base.SourceError(f'Checkpoint URL/sequence mismatch on page {n}')
        if obj['count'] != state['source_count'] or obj['total_pages'] != state['source_pages']:
            raise base.SourceError(f'Checkpoint count mismatch on page {n}')
        for row in obj['rows']:
            sid = base.source_id(row)
            if sid in ids:
                raise base.SourceError('Checkpoint repeated ID on page ' + str(n))
            ids.add(sid)
            all_rows.append(row)
        pages.append({'number':n, 'row_count':len(obj['rows'])})
        current_url = obj['next_url']
    if current_url != state['next_url'] or len(ids) != len(all_rows):
        raise base.SourceError('Checkpoint next link/IDs inconsistent')
    return all_rows, ids, pages

def page_data(url, page_num, expected=None, fetch=None):
    if not base.valid_url(url):
        raise base.SourceError('Nonofficial Niva page URL refused')
    obj = (fetch or base.get_json)(url)
    rows, links, count, total_pages = base.parsed_page(obj, page_num, expected)
    clean = [base.clean_row(r) for r in rows]
    next_link = base.next_url(url, links.get('next'))
    if page_num < total_pages and not next_link:
        raise base.SourceError(f'Missing next link at page {page_num}/{total_pages}')
    if page_num == total_pages and next_link:
        raise base.SourceError(f'Unexpected next link at final page {page_num}')
    return {'page':page_num,'url':url,'rows':clean,'count':count,
            'total_pages':total_pages,'next_url':next_link}

def invalidate(cp):
    if cp.exists():
        shutil.rmtree(cp)

def collect_session(out, seconds=1200, delay=.35, progress=print, fetch=None, monotonic=None,
                    max_new_pages=0):
    """Return a partial status or a complete ZIP path; never publish directly."""
    if seconds <= 0 or delay < 0 or max_new_pages < 0:
        raise ValueError('Invalid collection limits')
    monotonic = monotonic or time.monotonic
    started = monotonic()
    out.mkdir(parents=True, exist_ok=True)
    cp = out / 'checkpoint'
    state_file = cp / 'state.json'
    # Every run rechecks the official starting page, including a resumed run.
    first = page_data(base.INITIAL, 1, fetch=fetch)
    if first['count'] < 10000:
        raise base.SourceError('Source returned fewer than 10,000 rows; incomplete scope unverified')
    state = None
    if state_file.is_file():
        try:
            previous = json.loads(state_file.read_text(encoding='utf8'))
            if (fresh_age_ok(previous) and
                previous['source_count'] == first['count'] and
                previous['source_pages'] == first['total_pages'] and
                previous['page_one_sha256'] == digest(first['rows'])):
                state = previous
            else:
                progress('NIVA_CHECKPOINT_INVALIDATED source first page/count changed or older than 24 hours')
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            progress('NIVA_CHECKPOINT_INVALIDATED bad state '+type(exc).__name__)
    if state is None:
        invalidate(cp)
        save(pfile(cp, 1), first)
        state = {'started_utc':utc(), 'source_count':first['count'],
                 'source_pages':first['total_pages'], 'page_one_sha256':digest(first['rows']),
                 'pages_captured':1, 'next_url':first['next_url']}
        save(state_file, state)
        progress(f'NIVA_START provider_count={first["count"]} total_pages={first["total_pages"]}')
    else:
        progress(f'NIVA_RESUME {state["pages_captured"]}/{state["source_pages"]} stored pages')
    all_rows, ids, pages = read_pages(cp, state)
    url = state['next_url']
    new_pages = 0
    while url and (monotonic() - started) < seconds and (not max_new_pages or new_pages < max_new_pages):
        n = state['pages_captured'] + 1
        try:
            record = page_data(url, n, (state['source_count'], state['source_pages']), fetch)
        except base.SourceError as exc:
            if 'Count/page drift' in str(exc):
                invalidate(cp)
                raise SourceEpochChanged('Niva count changed; old partition checkpoints invalidated. '+str(exc)) from exc
            raise
        for row in record['rows']:
            sid = base.source_id(row)
            if sid in ids:
                invalidate(cp)
                raise SourceEpochChanged(f'Duplicate hospital ID across pages at {n}; old checkpoints invalidated')
            ids.add(sid)
            all_rows.append(row)
        save(pfile(cp, n), record)
        pages.append({'number':n,'row_count':len(record['rows'])})
        state['pages_captured'] = n
        url = record['next_url']
        state['next_url'] = url
        save(state_file, state)
        new_pages += 1
        if n % 25 == 0 or url is None:
            progress(f'NIVA_CAPTURE {n}/{state["source_pages"]} rows={len(all_rows)}/{state["source_count"]}')
        if url and delay:
            time.sleep(delay)
    if url:
        status = {'complete':False,'status':'PAUSED_CHECKPOINT_SAVED','captured_pages':state['pages_captured'],
                  'expected_pages':state['source_pages'],'captured_rows':len(all_rows),
                  'expected_rows':state['source_count'],'started_utc':state['started_utc']}
        save(out/'niva_status.json',status)
        progress('NIVA_PAUSED '+json.dumps(status,separators=(',',':')))
        return status
    if state['pages_captured'] != state['source_pages'] or len(all_rows) != state['source_count']:
        raise base.SourceError('All page and source record totals have not reconciled')
    # The source provides no immutable snapshot cursor: compare five distributed
    # pages after collection. Any drift prevents publication.
    indices = sorted({1, state['source_pages'],
                      max(1,round(state['source_pages']*0.25)),
                      max(1,round(state['source_pages']*0.5)),
                      max(1,round(state['source_pages']*0.75))})
    for n in indices:
        captured=json.loads(pfile(cp,n).read_text(encoding='utf8'))
        observed=page_data(captured['url'],n,(state['source_count'],state['source_pages']),fetch)
        if digest(observed['rows'])!=digest(captured['rows']):
            invalidate(cp)
            raise SourceEpochChanged(f'Live Niva page {n} differs from captured data; snapshot not published')
    source_meta={'complete':True,'verification':'All provider pages and IDs reconciled',
                 'source_count':state['source_count'],'source_pages':state['source_pages'],
                 'pages_captured':state['pages_captured'],'records_captured':len(all_rows),
                 'pages':pages,'captured_window_start_utc':state['started_utc'],
                 'verification_method':'All pages plus five live distributed-page checks',
                 'snapshot_limitation':'Source has no frozen snapshot cursor'}
    bundle = base.write_result(out,all_rows,source_meta)
    status={'complete':True,'status':'COMPLETE','captured_pages':state['pages_captured'],
            'expected_pages':state['source_pages'],'captured_rows':len(all_rows),
            'verified_utc':utc(),'bundle':str(bundle)}
    save(out/'niva_status.json',status)
    # Do not reuse a finished snapshot as if it were a fresh collection next day.
    invalidate(cp)
    save(cp/'completed.json', {'completed_utc':status['verified_utc'],'source_count':status['captured_rows']})
    progress('NIVA_COMPLETE '+json.dumps(status,separators=(',',':')))
    return status

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,default=Path('build/niva'))
    p.add_argument('--seconds',type=int,default=1200)
    p.add_argument('--delay',type=float,default=.35)
    args=p.parse_args()
    try:
        collect_session(args.output,seconds=args.seconds,delay=args.delay,
                        progress=lambda msg:print(msg,flush=True))
    except Exception as exc:
        args.output.mkdir(parents=True,exist_ok=True)
        save(args.output/'niva_status.json',{'complete':False,'status':'FAILED_SAFE',
             'error':type(exc).__name__+': '+str(exc)[:300],'observed_utc':utc()})
        print('NIVA_FAILED_SAFE '+type(exc).__name__+': '+str(exc)[:300],file=sys.stderr,flush=True)
        return 2
    return 0

if __name__=='__main__':sys.exit(main())
