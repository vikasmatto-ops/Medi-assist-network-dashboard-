"""NetworkIQ ICICI public locator: resumable, rate-limited, city-by-city staged capture.

Builds on independently confirmed HTML form GET/POST collector. NEVER mutates Phase 3.
City search presence means active-network candidate, NOT guaranteed cashless eligibility.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from icici_live_collector import (URL, ICICISession, choose_option, connect_db,
    export, get_rows_from_html, header_key, now, select_options, upsert)

DEFAULT_OUT = 'icici_live_output'

def setup_batch_db(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS icici_city_sync (
      state_key TEXT NOT NULL, city_key TEXT NOT NULL,
      state_label TEXT NOT NULL, city_label TEXT NOT NULL,
      last_attempt_utc TEXT NOT NULL, last_success_utc TEXT,
      status TEXT NOT NULL, rows_last_scan INTEGER NOT NULL DEFAULT 0,
      last_run_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      detail TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (state_key, city_key)
    );
    CREATE INDEX IF NOT EXISTS idx_city_sync_status ON icici_city_sync(status);
    CREATE TABLE IF NOT EXISTS icici_state_catalog (
      state_key TEXT PRIMARY KEY, state_label TEXT NOT NULL,
      discovered_utc TEXT NOT NULL, cities_json TEXT NOT NULL,
      city_count INTEGER NOT NULL
    );
    ''')
    conn.commit()


def city_key(state, city):
    return header_key(state), header_key(city)


def status_row(conn,state,city):
    return conn.execute('SELECT status,attempts FROM icici_city_sync WHERE state_key=? AND city_key=?',
                        city_key(state,city)).fetchone()


def write_city_status(conn, state, city, status, count, run_id, detail=''):
    sk,ck=city_key(state,city)
    stamp=now()
    conn.execute('''INSERT INTO icici_city_sync (state_key,city_key,state_label,city_label,
      last_attempt_utc,last_success_utc,status,rows_last_scan,last_run_id,attempts,detail)
      VALUES (?,?,?,?,?,?,?,?,?,1,?)
      ON CONFLICT(state_key,city_key) DO UPDATE SET
      state_label=excluded.state_label, city_label=excluded.city_label,
      last_attempt_utc=excluded.last_attempt_utc,
      last_success_utc=CASE WHEN excluded.status='CAPTURED' THEN excluded.last_success_utc ELSE icici_city_sync.last_success_utc END,
      status=excluded.status, rows_last_scan=excluded.rows_last_scan,
      last_run_id=excluded.last_run_id, attempts=icici_city_sync.attempts+1,
      detail=excluded.detail''',
       (sk,ck,state,city,stamp,stamp if status=='CAPTURED' else None,status,count,run_id,detail[:600]))
    conn.commit()


def bootstrap_existing(conn):
    """Recognize verified first run as completed so it isn't re-scraped."""
    records = conn.execute('''SELECT state,requested_city,returned_records,run_id
                              FROM capture_runs WHERE status='RECORDS_STAGED' AND returned_records>0''').fetchall()
    added=0
    for state,city,count,rid in records:
        if state and city and not status_row(conn,state,city):
            write_city_status(conn,state,city,'CAPTURED',count,rid,'Imported successful previous capture_run')
            added+=1
    return added


def check_records(records, state, city):
    if not records:
        return 'ZERO_UNVERIFIED','Official table returned zero parsed records; may be legitimately empty or a response variation'
    sk,ck=city_key(state,city)
    alias={'gurgaon':'gurugram','gurugram':'gurgaon', 'orissa':'odisha','odisha':'orissa'}
    mismatched=[]
    for r in records:
        rs,rc=city_key(r['state'],r['city'])
        if rs != sk and alias.get(rs)!=sk:
            mismatched.append(f"state={r['state']}")
        if rc != ck and alias.get(rc)!=ck:
            mismatched.append(f"city={r['city']}")
        if not r['hospital_name'].strip() or not r['address'].strip() or not r['hospital_id'].strip():
            mismatched.append('empty hospital id/name/address')
    if mismatched:
        return 'REJECTED_MISMATCH', 'Validation failed: '+', '.join(mismatched[:6])
    ids=[r['hospital_id'].casefold() for r in records]
    if len(ids)!=len(set(ids)):
        return 'REJECTED_MISMATCH', 'Duplicate hospital insurer reference IDs within returned city'
    return 'CAPTURED', 'Insurer ID, name, address, city, state validated'


def list_states(client):
    doc=client.send('list_states')
    states=select_options(doc,'State')
    if not states:
        raise RuntimeError('Official page returned zero state dropdown entries')
    return states


def ranked_states(states, state_name=''):
    if state_name:
        return [choose_option(states,state_name)]
    # Begin where our verified 110 records came from, then cover every other available state.
    return sorted(states, key=lambda s:(0 if header_key(s['label'])=='haryana' else 1,
                                        s['label'].casefold()))


def get_catalog(conn, state, ttl_hours=24):
    """Return full official city-option snapshot if recent, or None if absent/stale."""
    key=header_key(state)
    rec=conn.execute('SELECT discovered_utc,cities_json FROM icici_state_catalog WHERE state_key=?',(key,)).fetchone()
    if not rec:
        return None
    try:
        age=datetime.now(timezone.utc)-datetime.fromisoformat(rec[0])
        if age < timedelta(0) or age > timedelta(hours=ttl_hours):
            return None
        cities=json.loads(rec[1])
        if not isinstance(cities,list) or not cities:
            return None
        return cities
    except (ValueError,TypeError):
        return None


def save_catalog(conn, state, cities):
    """Only cache a successfully discovered non-empty dropdown."""
    if not cities:
        raise ValueError('Cannot save an empty state-city catalog')
    conn.execute("""INSERT INTO icici_state_catalog (state_key,state_label,discovered_utc,cities_json,city_count)
      VALUES (?,?,?,?,?) ON CONFLICT(state_key) DO UPDATE SET
      state_label=excluded.state_label,discovered_utc=excluded.discovered_utc,
      cities_json=excluded.cities_json,city_count=excluded.city_count""",
      (header_key(state),state,now(),json.dumps(cities,ensure_ascii=False),len(cities)))
    conn.commit()


def eligible(prev, retry_failed=False):
    if prev is None:
        return True
    status,attempts=prev
    if status=='CAPTURED':
        return False
    return bool(retry_failed and attempts < 3)


def unresolved(conn):
    rows=conn.execute("SELECT status,COUNT(*) FROM icici_city_sync WHERE status!='CAPTURED' GROUP BY status").fetchall()
    return {s:n for s,n in rows}


def summary(conn):
    rows=conn.execute('''SELECT status,COUNT(*),SUM(rows_last_scan) FROM icici_city_sync GROUP BY status''').fetchall()
    return {s:{'cities':n,'rows_last_scanned':records or 0} for s,n,records in rows}


def main(argv=None):
    ap=argparse.ArgumentParser(description='Incrementally scan official ICICI public hospital search by state and city')
    ap.add_argument('--state',default='',help='Optional exact state name; omit for nationwide')
    ap.add_argument('--max-new-cities',type=int,default=20,help='Bounded batch of NEW city searches (default 20)')
    ap.add_argument('--pause',type=float,default=1.5,help='Minimum pause 1.5 seconds between HTTP requests')
    ap.add_argument('--timeout',type=float,default=35)
    ap.add_argument('--retry-failed',action='store_true',help='Retry previous failed/zero searches (up to 3 total attempts)')
    ap.add_argument('--output',default=DEFAULT_OUT)
    ap.add_argument('--catalog-ttl-hours',type=float,default=24.0,help='Refresh successful state/city catalogs after 24h')
    ap.add_argument('--state-retries',type=int,default=2,help='Transient retries per state, capped at 3 attempts')
    args=ap.parse_args(argv)
    if not (1<=args.max_new_cities<=100):ap.error('--max-new-cities must be 1..100')
    if args.pause<1.5:ap.error('--pause must be at least 1.5 seconds')
    if not (0 <= args.state_retries <= 3):ap.error('--state-retries must be 0..3')
    if not (1 <= args.catalog_ttl_hours <= 168):ap.error('--catalog-ttl-hours must be 1..168')
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    conn=connect_db(out/'icici_active_search_staging.sqlite3')
    setup_batch_db(conn)
    bootstrap_count=bootstrap_existing(conn)
    cli=ICICISession(pause=args.pause,timeout=args.timeout)
    rid=uuid.uuid4().hex
    report={'run_id':rid,'started_utc':now(),'source':URL,'mode':'resumable_nationwide_city_batch',
      'phase3_modified':False,'complete_pan_india_network':False,
      'previous_runs_bootstrapped':bootstrap_count,'target_state':args.state or 'all',
      'max_new_cities':args.max_new_cities,'new_city_attempts':[], 'errors':[],
      'states_skipped_from_fresh_catalog':[], 'state_catalogs_refreshed':[],
      'source_discovery_unavailable':False}
    attempts=0
    try:
        states=ranked_states(list_states(cli),args.state)
        report['available_states']=len(states)
        consecutive_state_failures=0
        for s in states:
            if attempts>=args.max_new_cities:break
            label=s['label']
            cached=get_catalog(conn,label,args.catalog_ttl_hours)
            # A *complete*, fresh dropdown snapshot avoids pointless repeat state posts.
            # No stale catalog is used to claim newly added city options are exhausted.
            if cached and all(not eligible(status_row(conn,label,c['label']),args.retry_failed) for c in cached):
                report['states_skipped_from_fresh_catalog'].append(label)
                continue
            selected=None
            cities=[]
            for retry_index in range(args.state_retries+1):
                try:
                    selected,cities=cli.prepare_state(label)
                    if header_key(selected['label'])!=header_key(label):
                        raise RuntimeError('State dropdown selection unexpectedly changed')
                    save_catalog(conn,label,cities)
                    print(f"STATE {label}: {len(cities)} city options",flush=True)
                    report['state_catalogs_refreshed'].append(label)
                    consecutive_state_failures=0
                    break
                except Exception as exc:
                    msg=f'{type(exc).__name__}: {str(exc)[:400]}'
                    print(f'  State discovery retry {retry_index+1}/{args.state_retries+1} failed for {label}: {msg}',flush=True)
                    if retry_index < args.state_retries:
                        time.sleep(min(30,3*(2**retry_index)))
                    else:
                        report['errors'].append({'state':label,'error':msg})
                        consecutive_state_failures+=1
            if selected is None:
                # If the site is having systemic DNS/network failures, STOP. Continuing
                # through every other state amplifies load and falsely implies completeness.
                if consecutive_state_failures >= 3:
                    report['source_discovery_unavailable']=True
                    print('Circuit breaker: consecutive state discovery failures; stopping safely.',flush=True)
                    break
                continue
            for c in cities:
                if attempts>=args.max_new_cities:break
                prev=status_row(conn,label,c['label'])
                if not eligible(prev,args.retry_failed):continue
                attempts+=1
                print(f"  [{attempts}/{args.max_new_cities}] {label} / {c['label']}",flush=True)
                entry={'state':label,'city':c['label'],'at':now()}
                try:
                    html=cli.search(selected['value'],c['value'])
                    data,evidence=get_rows_from_html(html,selected['label'],c['label'])
                    status,detail=check_records(data,selected['label'],c['label'])
                    entry.update({'status':status,'extracted':len(data),
                      'table_evidence':evidence, 'detail':detail})
                    if status=='CAPTURED':
                        upsert(conn,data,rid)
                    write_city_status(conn,label,c['label'],status,len(data),rid,detail)
                    print(f'     {status}: {len(data)} extracted',flush=True)
                except Exception as exc:
                    detail=f'{type(exc).__name__}: {str(exc)[:350]}'
                    entry.update({'status':'FAILED','extracted':0,'detail':detail})
                    write_city_status(conn,label,c['label'],'FAILED',0,rid,detail)
                    print(f'     FAILED: {detail}',flush=True)
                report['new_city_attempts'].append(entry)
                # Flush progressive report at every city for crash/cancellation diagnostics.
                (out/'icici_nationwide_batch_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        report['finished_utc']=now()
        report['cities_attempted_this_run']=attempts
        report['city_status_totals']=summary(conn)
        report['unresolved_city_statuses']=unresolved(conn)
        report['discovery_errors_present']=bool(report['errors'])
        report['verified_complete']=False  # Never equate UI dropdown scan with policy-level active entitlement
        report['unique_hospital_records_staged']=export(conn,out/'icici_hospitals_staged.csv')
        # Report all pending failed/zero entries separately; NEVER call them 'not networked'.
        cur=conn.execute('SELECT state_label,city_label,status,attempts,detail FROM icici_city_sync ORDER BY state_label,city_label')
        with (out/'icici_city_progress.csv').open('w',newline='',encoding='utf-8-sig') as fh:
            wr=csv.writer(fh);wr.writerow(['state','city','status','attempts','detail']);wr.writerows(cur)
    except Exception as exc:
        report['fatal_error']=f'{type(exc).__name__}: {str(exc)[:1000]}'
        print(report['fatal_error'],file=sys.stderr,flush=True)
    finally:
        report['finished_utc']=now()
        report['last_request_evidence']=cli.steps[-4:]
        (out/'icici_nationwide_batch_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        conn.close()
    print('Output:',out)
    print('All current staged records:',report.get('unique_hospital_records_staged','unknown'))
    if report.get('errors'):
        print('INCOMPLETE: some state dropdowns were unreachable; do not claim Pan-India completion.',flush=True)
    if report.get('unresolved_city_statuses'):
        print('REVIEW: pending city outcomes:',report['unresolved_city_statuses'],flush=True)
    print('Phase 3 and Medi Assist: UNMODIFIED')
    return 2 if report.get('fatal_error') else 0

if __name__=='__main__':sys.exit(main())
