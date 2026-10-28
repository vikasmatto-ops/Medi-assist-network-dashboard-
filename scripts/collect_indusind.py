#!/usr/bin/env python3
"""NetworkIQ: read-only, resumable capture of IndusInd Brobot public SearchList.
Source evidence: public SPA JS and successful API replies tested 2026-10-15.
This collector never changes a remote service or publishes to GitHub.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

BASE = 'https://messenger.brobotinsurance.com/GeoLocator'
ENDPOINT = BASE + '/api/NetworkHospitalApi/SearchList'
PAGE_SIZE = 200
MAX_PAGES = 250
MAX_BYTES = 3_000_000
EXPECTED_MIN_TOTAL = 10_000


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def identity(row):
    """Not an insurer ID; only a fingerprint to detect duplicates/collisions."""
    return [normalized(row.get(k)) for k in ('hospitalName', 'address', 'state', 'pinCode')]


def fingerprint(row):
    return hashlib.sha256(compact(identity(row)).encode('utf-8')).hexdigest()


def page_digest(items):
    return hashlib.sha256(compact(items).encode('utf-8')).hexdigest()


def http_fetch(page, page_size=PAGE_SIZE, timeout=100):
    body = json.dumps({'pageNumber':page, 'pageSize':page_size}).encode('utf-8')
    req = urllib.request.Request(ENDPOINT, method='POST', data=body, headers={
        'Content-Type': 'application/json; charset=UTF-8',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': BASE,
        'Referer': BASE+'/locator/Network_hospital',
        'User-Agent': 'Mozilla/5.0 (NetworkIQ read-only network validation; research)',
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f'Server returned HTTP {response.status}')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Unexpectedly large single-page response; refusing to continue')
    return json.loads(raw.decode('utf-8-sig'))


def check_page(obj, page, expected_total=None):
    if not isinstance(obj, dict) or not isinstance(obj.get('items'), list):
        raise ValueError(f'Page {page}: missing items array')
    total = obj.get('totalCount')
    if isinstance(total, bool) or not isinstance(total, int) or not EXPECTED_MIN_TOTAL <= total <= 50_000:
        raise ValueError(f'Page {page}: invalid totalCount {total!r}')
    if expected_total is not None and total != expected_total:
        raise ValueError(f'Page {page}: source count changed {expected_total} -> {total}; stop, preserve staging')
    rows = obj['items']
    if len(rows) > PAGE_SIZE or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f'Page {page}: unexpected row structure')
    if not rows:
        raise ValueError(f'Page {page}: empty response before final page')
    if page < math.ceil(total/PAGE_SIZE) and len(rows) != PAGE_SIZE:
        raise ValueError(f'Page {page}: expected {PAGE_SIZE}, got {len(rows)}; refusing silent gaps')
    if page == math.ceil(total/PAGE_SIZE) and len(rows) != total-(page-1)*PAGE_SIZE:
        raise ValueError(f'Last page expected {total-(page-1)*PAGE_SIZE}, received {len(rows)}')
    for i, row in enumerate(rows):
        if not str(row.get('hospitalName') or '').strip() or not str(row.get('address') or '').strip():
            raise ValueError(f'Page {page}, index {i}: missing hospital name/address')
    return total, rows


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(path)


def retry_fetch(page, fetch_fn, sleep_fn=time.sleep, attempts=4):
    for attempt in range(1, attempts+1):
        try:
            return fetch_fn(page)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (400,401,403,404,405):
                raise
            if attempt == attempts: raise
            wait = min(12, 2 ** (attempt - 1))
            print(f'Page {page}: transient {type(exc).__name__}, retry {attempt}/{attempts} after {wait}s',flush=True)
            sleep_fn(wait)


def audit_records(rows, total, page_hashes):
    counts = Counter()
    duplicate_samples = []
    seen = defaultdict(list)
    invalid_samples=[]
    field_names=('hospitalName','address','state','city','pinCode','phone','mailId')
    for i,r in enumerate(rows):
        for field in field_names:
            if not str(r.get(field) or '').strip(): counts['missing_'+field] += 1
        counts['type_'+str(r.get('type'))] += 1
        counts['status_'+str(r.get('status'))] += 1
        if not r.get('latitude') or not r.get('longitude'):counts['missing_geocoordinates']+=1
        if r.get('hospitalId') is not None: counts['has_source_hospital_id']+=1
        if not re.fullmatch(r'\d{6}',str(r.get('pinCode') or '')):counts['invalid_pin']+=1
        seen[fingerprint(r)].append(i)
        if len(invalid_samples)<30 and not r.get('state'):
            invalid_samples.append({'row':i+1,'hospitalName':r.get('hospitalName'),'pinCode':r.get('pinCode'),'reason':'missing_state'})
    for k,inds in seen.items():
        if len(inds)>1:
            counts['duplicate_fingerprint_surplus']+=len(inds)-1
            if len(duplicate_samples)<40:
                r=rows[inds[0]]
                duplicate_samples.append({'fingerprint':k,'rows':[x+1 for x in inds[:8]],'name':r.get('hospitalName'),'address':r.get('address'),'state':r.get('state'),'pin':r.get('pinCode')})
    return {
        'source':'IndusInd General Insurance public Brobot locator, SearchList',
        'endpoint':ENDPOINT,
        'captured_at_utc':now(),
        'totalCount_reported_by_API':total,
        'raw_records_collected':len(rows),
        'unique_surrogate_fingerprints':len(seen),
        'pages_collected':len(page_hashes),
        'page_size':PAGE_SIZE,
        'source_supplies_stable_hospital_ids':counts.get('has_source_hospital_id',0)>0,
        'source_status_values':{k[7:]:v for k,v in counts.items() if k.startswith('status_')},
        'source_network_types':{k[5:]:v for k,v in counts.items() if k.startswith('type_')},
        'field_quality':{k:v for k,v in counts.items() if k.startswith('missing_') or k in ('invalid_pin','has_source_hospital_id','duplicate_fingerprint_surplus')},
        'duplicate_fingerprint_examples':duplicate_samples,
        'missing_state_examples':invalid_samples,
        'page_hashes':page_hashes,
        'coverage_warning':'Full pagination capture is a source snapshot, not policy-specific cashless verification. City and coordinates may be absent. No independent insurer ID has been established.',
        'automatic_refresh':'Guarded scheduled NetworkIQ source collection; publication independently validated',
        'publish_cleared':False,
    }


def collect(root, output, fetch_fn=http_fetch, sleep_fn=time.sleep, delay=.35, max_pages=None):
    """max_pages only for offline test/diagnostics; no export until fully complete."""
    root=Path(root)
    root.mkdir(parents=True,exist_ok=True)
    pages=root/'pages'
    pages.mkdir(exist_ok=True)
    manifest_path=root/'manifest.json'
    manifest=None
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('endpoint') != ENDPOINT or manifest.get('page_size') != PAGE_SIZE:
            raise ValueError('Checkpoint source/size mismatch; use a new folder')
    # One fresh first-page request each run to check the total before resuming.
    first=retry_fetch(1,fetch_fn,sleep_fn)
    total,first_rows=check_page(first,1)
    first_hash=page_digest(first_rows)
    if manifest and (manifest.get('total') != total or manifest.get('first_page_sha256') != first_hash):
        raise ValueError('Source data changed since checkpoint. Do NOT mix old/new pages; use a new folder.')
    pages_required=math.ceil(total/PAGE_SIZE)
    if pages_required>MAX_PAGES:raise ValueError('Unexpected page count; stopping')
    if not manifest:
        manifest={'endpoint':ENDPOINT,'page_size':PAGE_SIZE,'total':total,'first_page_sha256':first_hash,'started_at_utc':now()}
        atomic_json(manifest_path,manifest)
    atomic_json(pages/'page_0001.json',first)
    limit=min(pages_required,max_pages) if max_pages is not None else pages_required
    print(f'SearchList reports {total} records across {pages_required} pages.',flush=True)
    rows=[]
    page_hashes={}
    # For safety, read persisted checkpoints through the same validation as new pages.
    for page in range(1,limit+1):
        path=pages/f'page_{page:04d}.json'
        if path.exists():
            obj=json.loads(path.read_text(encoding='utf-8'))
            source='checkpoint'
        else:
            if delay: sleep_fn(delay)
            obj=retry_fetch(page,fetch_fn,sleep_fn)
            source='live'
        _,items=check_page(obj,page,total)
        digest=page_digest(items)
        if digest in page_hashes.values():
            raise ValueError(f'Page {page} duplicates another complete page; stopped instead of publishing incomplete data')
        page_hashes[str(page)]=digest
        if source=='live':atomic_json(path,obj)
        rows.extend(items)
        if page==1 or page%10==0 or page==pages_required:
            print(f'Page {page}/{pages_required} {source}; collected {len(rows)}/{total}',flush=True)
    if len(rows)!=total or limit!=pages_required:
        print(f'INCOMPLETE: {len(rows)}/{total}. Saved page checkpoints; no export published.',flush=True)
        return False,None
    audit=audit_records(rows,total,page_hashes)
    data={'metadata':{'source_name':'IndusInd General Insurance','source_type':'delegated locator SearchList API','source_endpoint':ENDPOINT,'retrieved_at_utc':audit['captured_at_utc'],'record_count':len(rows),'network_status':'source_listing_unverified','policy_cashless_verified':False,'refresh_type':'manual_snapshot'}, 'hospitals':rows}
    ztmp=Path(str(output)+'.tmp')
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(ztmp,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        z.writestr('indusind_network_hospitals.json',json.dumps(data,ensure_ascii=False,separators=(',',':')))
        z.writestr('indusind_collection_report.json',json.dumps(audit,ensure_ascii=False,indent=2))
    ztmp.replace(output)
    print('COMPLETE: '+str(output),flush=True)
    print('Source rows: %d | fingerprint duplicates: %d | missing city: %d | missing coordinates: %d'%(len(rows),audit['field_quality'].get('duplicate_fingerprint_surplus',0),audit['field_quality'].get('missing_city',0),audit['field_quality'].get('missing_geocoordinates',0)),flush=True)
    print('STAGING ONLY, not published to NetworkIQ dashboard.',flush=True)
    return True,audit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work-dir',required=True)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    try:
        ok,_=collect(args.work_dir,args.output)
    except Exception as exc:
        print('STOPPED SAFELY:',str(exc),file=sys.stderr,flush=True)
        print('Any completed page checkpoints were retained. Re-run to resume after resolving the issue.',file=sys.stderr)
        sys.exit(2)
    if not ok:sys.exit(2)

if __name__=='__main__': main()
