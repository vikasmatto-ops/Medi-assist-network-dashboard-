"""NetworkIQ India / ICICI Lombard official *live HTML-form* connector.

Public active-network search only. No PDFs, Hexa Sheets, login, OTP, or Phase 3 writes.
Discovery evidence (2026-10-13): GET/POST /Customer/GetHospitalList returns HTML.
"""
from __future__ import annotations
import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

URL = 'https://ilhc.icicilombard.com/Customer/GetHospitalList'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; NetworkIQIndia/1.0; public-network-verification)',
           'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
COLUMNS = ('source', 'insurer', 'hospital_id', 'hospital_name', 'address', 'area',
           'city', 'state', 'pincode', 'contact_no', 'hospital_type',
           'accreditation_status', 'deposit_waiver', 'network_status',
           'coverage_status', 'observed_utc', 'source_url')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def norm(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def header_key(text):
    return re.sub(r'[^a-z0-9]', '', norm(text).lower())


def only_public_url(url):
    p = urlparse(url)
    return (p.scheme == 'https' and p.netloc.lower() == 'ilhc.icicilombard.com'
            and p.path.rstrip('/') == '/Customer/GetHospitalList')


def get_rows_from_html(html: str, state_hint: str = '', city_hint: str = '') -> tuple[list[dict], dict]:
    """Find matching hospital table; do not demand exact table-cell count.

    Uses only a known, source-specific table header containing Hospital Name,
    Address and City. Rejects empty/merged/header rows and unrelated tables.
    """
    soup = BeautifulSoup(html, 'html.parser')
    out: list[dict] = []
    signatures: list[dict] = []
    for table in soup.find_all('table'):
        trs = [tr for tr in table.find_all('tr') if tr.find_parent('table') is table]
        match_index = None
        columns = {}
        for i, tr in enumerate(trs):
            cells = tr.find_all(['td', 'th'], recursive=False)
            hdrs = [header_key(c.get_text(' ', strip=True)) for c in cells]
            if all(name in hdrs for name in ('hospitalname', 'address', 'city')) and len(hdrs) >= 7:
                match_index, columns = i, {h: idx for idx, h in enumerate(hdrs) if h}
                break
        if match_index is None:
            continue
        data_lengths = []
        accepted = 0
        for tr in trs[match_index+1:]:
            cells = [norm(el.get_text(' ', strip=True)) for el in tr.find_all(['td','th'], recursive=False)]
            if not cells or len(cells) < 7:
                continue
            if len(data_lengths) < 10:
                data_lengths.append(len(cells))
            def field(*keys):
                for key in keys:
                    idx = columns.get(key)
                    if idx is not None and idx < len(cells):
                        return cells[idx]
                return ''
            name = field('hospitalname')
            address = field('address')
            city = field('city') or city_hint
            state = field('state') or state_hint
            if (len(name) < 3 or len(name) > 250 or header_key(name) in ('hospitalname', 'selecthospital', 'hospital')
                    or not (address or (city and state))):
                continue
            # Data rows must have the expected core positional fields; a small
            # number of trailing action/checkbox columns may be missing.
            rec = {'hospital_id': field('refno','hospitalid','hospitalcode'),
                   'hospital_name': name, 'address': address, 'area': field('area'),
                   'city': city, 'state': state, 'pincode': '',
                   'contact_no': field('contactno','contactnumber'),
                   'hospital_type': field('hospitaltype'),
                   'accreditation_status': field('accreditationstatus'),
                   'deposit_waiver': field('depositwaiver')}
            out.append(rec)
            accepted += 1
        signatures.append({'headers': list(columns.keys()), 'row_count_after_header': len(trs)-match_index-1,
                           'first_ten_row_cell_counts': data_lengths, 'accepted_records': accepted})
    seen = set()
    unique = []
    for record in out:
        key = tuple(norm(record.get(k,'')).casefold() for k in ('hospital_id','hospital_name','address','city','state'))
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique, {'matched_tables': len(signatures), 'table_signatures': signatures, 'records_before_dedup': len(out),
                    'unique_records': len(unique)}


def select_options(html, name):
    soup = BeautifulSoup(html, 'html.parser')
    target = soup.find('select', attrs={'name': name})
    if not target:
        target = soup.find('select', attrs={'id': {'State':'ddlStateList','City':'ddlCityList'}.get(name, '')})
    options = []
    if target:
        for opt in target.find_all('option'):
            value = opt.get('value', '')
            label = norm(opt.get_text(' ', strip=True))
            if value and label and 'select' not in label.casefold()[:10] and not label.startswith('---'):
                options.append({'label': label, 'value': str(value)})
    return options


def choose_option(options, requested):
    if not requested:
        raise ValueError('State/City must be provided')
    q = header_key(requested)
    # No fuzzy substring matching: do not search the wrong city by accident.
    alias = {'gurugram':'gurgaon', 'gurgaon':'gurugram', 'odisha':'orissa', 'orissa':'odisha',
             'puducherry':'pondicherry', 'pondicherry':'puducherry'}
    hits = [o for o in options if header_key(o['label']) == q]
    if not hits and q in alias:
        hits = [o for o in options if header_key(o['label']) == alias[q]]
    if len(hits) == 1:
        return hits[0]
    choices = ', '.join(x['label'] for x in options[:50])
    raise ValueError(f'Could not uniquely select {requested!r}. Available (first 50): {choices}')


@dataclass
class Step:
    action: str
    status: int
    bytes: int
    final_url: str
    digest: str


class ICICISession:
    def __init__(self, pause=1.5, timeout=35):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.pause = pause
        self.timeout = timeout
        self.steps: list[dict] = []
    def send(self, action, params=None):
        if self.steps:
            time.sleep(self.pause)
        if params is None:
            r = self.s.get(URL, timeout=self.timeout)
        else:
            r = self.s.post(URL, data=params, timeout=self.timeout, headers={'Referer': URL})
        r.raise_for_status()
        if not only_public_url(r.url):
            raise RuntimeError(f'Unexpected redirect away from public hospital search: {urlparse(r.url).netloc}')
        body = r.content
        self.steps.append(Step(action, r.status_code, len(body), r.url,
                               hashlib.sha256(body).hexdigest()).__dict__)
        return r.text
    @staticmethod
    def payload(state='', city='', button='', area=''):
        return {'State': state, 'City': city, 'Area': area, 'Hospital_type': '',
                'HospitalCat': '', 'Hospital_status': '', 'Button': button}
    def prepare_state(self, requested_state):
        home = self.send('load_search_page')
        states = select_options(home, 'State')
        state = choose_option(states, requested_state)
        result = self.send('select_state', self.payload(state['value'], button='State Change'))
        cities = select_options(result, 'City')
        if not cities:
            # Some deployments don't refresh the city dropdown in the state-change form response.
            raise RuntimeError('State-change POST returned no City dropdown options. Check diagnostics; browser automation may be required.')
        return state, cities
    def search(self, state_value, city_value):
        # City Change populates Area; no personal fields or PDF involved.
        self.send('select_city', self.payload(state_value, city_value, 'City Change'))
        result = self.send('search', self.payload(state_value, city_value, 'Search'))
        return result


def connect_db(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS icici_observations (
      record_key TEXT PRIMARY KEY, source TEXT NOT NULL, insurer TEXT NOT NULL,
      hospital_id TEXT, hospital_name TEXT NOT NULL, address TEXT, area TEXT,
      city TEXT, state TEXT, pincode TEXT, contact_no TEXT, hospital_type TEXT,
      accreditation_status TEXT, deposit_waiver TEXT,
      network_status TEXT NOT NULL, coverage_status TEXT NOT NULL,
      observed_utc TEXT NOT NULL, source_url TEXT NOT NULL,
      first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL, last_run_id TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_icici_geo ON icici_observations(state,city);
    CREATE TABLE IF NOT EXISTS capture_runs (
      run_id TEXT PRIMARY KEY, captured_utc TEXT NOT NULL, state TEXT,
      requested_city TEXT, returned_records INTEGER, status TEXT NOT NULL, evidence_json TEXT NOT NULL
    );
    ''')
    return conn


def upsert(conn, records, run_id):
    moment=now()
    for r in records:
        key='\x1f'.join(norm(r.get(k,'')).casefold() for k in ('hospital_id','hospital_name','address','city','state'))
        key=hashlib.sha256(key.encode()).hexdigest()
        fields = {**r,'source':'icici_lombard','insurer':'ICICI Lombard General Insurance',
                  'network_status':'active_candidate','coverage_status':'SEARCH_RESULT_ONLY',
                  'observed_utc':moment,'source_url':URL}
        keys = ['record_key',*COLUMNS,'first_seen_utc','last_seen_utc','last_run_id']
        values = [key]+[fields.get(c,'') for c in COLUMNS]+[moment,moment,run_id]
        sql = (f"INSERT INTO icici_observations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
               'ON CONFLICT(record_key) DO UPDATE SET '
               'observed_utc=excluded.observed_utc,last_seen_utc=excluded.last_seen_utc,last_run_id=excluded.last_run_id')
        conn.execute(sql,values)
    conn.commit()


def export(conn, path):
    rows=conn.execute('SELECT '+','.join(COLUMNS)+' FROM icici_observations ORDER BY state,city,hospital_name').fetchall()
    with open(path,'w',newline='',encoding='utf-8-sig') as f:
        writer=csv.writer(f); writer.writerow(COLUMNS);writer.writerows(rows)
    return len(rows)


def main():
    ap=argparse.ArgumentParser(description='ICICI live public hospital search via official HTML form POST')
    ap.add_argument('--state',default='Haryana')
    ap.add_argument('--city',default='Gurgaon')
    ap.add_argument('--all-cities',action='store_true',help='Collect multiple cities in chosen state (limited by --max-cities)')
    ap.add_argument('--max-cities',type=int,default=5,help='Safety limit; max 25')
    ap.add_argument('--pause',type=float,default=1.5,help='Delay between public requests, minimum 1 second')
    ap.add_argument('--output',default='icici_live_output')
    args=ap.parse_args()
    if args.max_cities<1 or args.max_cities>25:
        ap.error('--max-cities must be from 1 to 25')
    if args.pause<1.0:
        ap.error('--pause must be at least 1.0 seconds')
    out=Path(args.output).resolve();out.mkdir(exist_ok=True,parents=True)
    conn=connect_db(out/'icici_active_search_staging.sqlite3')
    client=ICICISession(pause=args.pause)
    run_id=uuid.uuid4().hex
    report={'version':'1.0', 'run_id':run_id, 'started_utc':now(), 'official_source':URL,
            'transport':'live HTML form GET/POST (not JSON/XHR API)', 'source_scope':'public active-network search only',
            'complete_pan_india_network':False,'phase3_modified':False,
            'city_runs':[], 'error':None, 'steps':[]}
    try:
        selected_state, cities=client.prepare_state(args.state)
        report['chosen_state']=selected_state['label']
        report['available_city_count']=len(cities)
        selected_cities=cities[:args.max_cities] if args.all_cities else [choose_option(cities,args.city)]
        for c in selected_cities:
            print('SEARCH', selected_state['label'],'/', c['label'],flush=True)
            try:
                html=client.search(selected_state['value'],c['value'])
                records,signature=get_rows_from_html(html,selected_state['label'],c['label'])
                entry={'state':selected_state['label'],'city':c['label'],'records_extracted':len(records),
                       'table_evidence':signature,'status':'RECORDS_STAGED' if records else 'ZERO_ROWS_REQUIRES_INVESTIGATION'}
                if records:
                    upsert(conn,records,run_id)
                report['city_runs'].append(entry)
                conn.execute('INSERT INTO capture_runs VALUES (?,?,?,?,?,?,?)',
                             (run_id+':'+str(len(report['city_runs'])),now(),selected_state['label'],c['label'],len(records),entry['status'],json.dumps(signature)))
                conn.commit()
                print('  validated hospital records:',len(records), 'matched result tables:',signature['matched_tables'],flush=True)
            except Exception as exc:
                msg=f'{type(exc).__name__}: {str(exc)[:400]}'
                print('  SEARCH FAILED:',msg,flush=True)
                report['city_runs'].append({'city':c['label'],'records_extracted':0,'status':'FAILED', 'error':msg})
                if not args.all_cities:
                    raise
        report['staged_total']=export(conn,out/'icici_hospitals_staged.csv')
        if not report['staged_total']:
            report['next_diagnostic']='No result tables parsed. Inspect city_runs[].table_evidence. If no tables, dynamic browser capture is needed.'
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {str(exc)[:1000]}'
        print('CONNECTOR ERROR:',report['error'],flush=True)
    finally:
        report['steps']=client.steps
        report['finished_utc']=now()
        (out/'icici_connector_report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
        conn.close()
    print('Report:',out/'icici_connector_report.json')
    print('Phase 3 / Medi Assist: untouched.')
    if report['error'] or not report.get('staged_total',0):
        sys.exit(2)


if __name__=='__main__':main()
