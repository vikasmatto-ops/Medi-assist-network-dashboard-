#!/usr/bin/env python3
"""NetworkIQ — Niva Bupa official public hospital-list API full pagination collector.

Only official public provider fields. No login, cookies, policyholder data, browser or PDF.
Never publishes or modifies the existing NetworkIQ repository. Requires ALL source
pages and unique IDs to mark result complete. If the source returns a filtered
subset, output documents the actual count; does not falsely assert pan-India.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

BASE='https://rules.nivabupa.com'
INITIAL=BASE+'/api/v1/hospital_network/get_hospital_list/'
HOST='rules.nivabupa.com'
FIELDS=(
    'id','provider_id','name','provider_address','provider_city','provider_state',
    'provider_pincode','provider_latitude','provider_longitude','provider_speciality',
    'is_ppn','network_stream','beds_count','doctors_count','no_of_icu_beds',
    'provider_accrediation','provider_avgrating','provider_opd_number',
    'provider_email_id','distance_km','avg_admission_time','avg_discharge_time',
    'rating_count','review_count','rating_labels','ppn_benefits'
)
PUBLIC_FIELDS=tuple(f for f in FIELDS if f!='provider_email_id')

class SourceError(RuntimeError):pass

def stamp():return datetime.now(timezone.utc).isoformat()

def valid_url(url):
    """Only accept HTTPS official public hospital API URLs, including next links."""
    p=urllib.parse.urlsplit(url)
    return p.scheme=='https' and p.hostname==HOST and (p.port is None or p.port==443) and p.path.startswith('/api/v1/hospital_network/get_hospital_list/') and not (p.username or p.password or p.fragment)

def next_url(current, raw):
    if not raw:return None
    if not isinstance(raw,str): raise SourceError('Unexpected pagination next link type')
    url=urllib.parse.urljoin(current,raw)
    if not valid_url(url):raise SourceError('Unsafe or external pagination URL refused')
    return url

def get_json(url, retries=2, pause=1.0):
    if not valid_url(url): raise SourceError('Unexpected source URL refused')
    for attempt in range(retries+1):
        try:
            req=urllib.request.Request(url,headers={
                'Accept':'application/json',
                'User-Agent':'NetworkIQ-SourceAudit/1.0 (+public hospital network; no credentials)',
                'Referer':BASE+'/hospital-network/',
            })
            with urllib.request.urlopen(req, timeout=45) as res:
                if not valid_url(res.url):raise SourceError('Official request redirected outside allowed host/path')
                if res.status!=200:raise SourceError(f'HTTP {res.status}')
                data=res.read(7_000_001)
                if len(data)>7_000_000:raise SourceError('Source page unexpectedly large')
            obj=json.loads(data)
            if not isinstance(obj,dict):raise SourceError('Expected JSON object')
            return obj
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403,404):raise SourceError(f'HTTP {exc.code} — no login or bypass attempted') from exc
            if exc.code not in (429,500,502,503,504) or attempt>=retries:raise SourceError(f'HTTP {exc.code}') from exc
        except SourceError:raise
        except (urllib.error.URLError,TimeoutError,json.JSONDecodeError) as exc:
            if attempt>=retries:raise SourceError(f'Source connection/JSON failure: {type(exc).__name__}') from exc
        time.sleep(pause*(attempt+1))
    raise SourceError('Fetch failed')

def source_id(row):
    # id and provider_id are distinct fields on the source. Use source row id as
    # primary identity; do not merge hospitals that merely share names/ZIPs.
    value=row.get('id')
    if value is None or str(value).strip()=='':raise SourceError('Missing Niva row id')
    return str(value)

def clean_row(raw):
    if not isinstance(raw,dict):raise SourceError('Non-object hospital row')
    if not str(raw.get('name') or '').strip():raise SourceError('Hospital row missing name')
    if not source_id(raw):raise SourceError('Missing id')
    out={key:raw.get(key) for key in PUBLIC_FIELDS if key in raw}
    if not (str(out.get('provider_address') or '').strip() and str(out.get('provider_city') or '').strip() and str(out.get('provider_state') or '').strip()):
        # We preserve the hospital and report deficient fields; not silently synthesize.
        out['quality_note']='Incomplete source address/city/state'
    return out

def parsed_page(obj,page_number,baseline=None):
    if obj.get('success') is False: raise SourceError(f'Source success=false, page {page_number}')
    vals=obj.get('results'); links=obj.get('links')
    if not isinstance(vals,list) or not isinstance(links,dict): raise SourceError(f'Unexpected results/links schema on page {page_number}')
    try:
        count=int(obj['count']); total_pages=int(obj['total_pages'])
    except (KeyError,ValueError,TypeError) as exc:raise SourceError('Missing count/total_pages') from exc
    if count<=0 or count>500000 or total_pages<=0 or total_pages>100000:raise SourceError('Implausible source count/pages')
    if not vals:raise SourceError(f'Empty page {page_number}/{total_pages}')
    if len(vals)>500:raise SourceError('Unexpected >500 rows on source page')
    if baseline and (count,total_pages)!=baseline:raise SourceError(f'Count/page drift on page {page_number}: {(count,total_pages)} vs {baseline}')
    return vals,links,count,total_pages

def collect(fetch=get_json, first=INITIAL, max_pages=0, delay=.35, min_expected_count=5000, progress=print):
    """Follow only provider-supplied next links. Never construct guessed page URLs."""
    if not valid_url(first):raise SourceError('First URL not in official hospital API')
    url=first; visited=set(); rows=[]; seen_ids=set(); page=0; expected=None
    pages=[]
    while url:
        if url in visited:raise SourceError('Pagination loop/cycle detected')
        visited.add(url);page+=1
        obj=fetch(url)
        vals, links, count,total_pages=parsed_page(obj,page,expected)
        expected=(count,total_pages)
        if page>total_pages:raise SourceError('More pages returned than source total_pages')
        for raw in vals:
            item=clean_row(raw); sid=source_id(item)
            if sid in seen_ids:raise SourceError(f'Duplicate source hospital id {sid} on page {page}')
            seen_ids.add(sid);rows.append(item)
        pages.append({'number':page,'row_count':len(vals)})
        if page==1:
            progress(f'Niva API reports {count} records over {total_pages} pages.')
            if count<min_expected_count:
                raise SourceError(f'Only {count} records returned by the unfiltered API. Likely filtered/default subset; nationwide scope not established, refusing complete collection')
        if page%25==0 or page==total_pages:progress(f'Page {page}/{total_pages}: {len(rows)} distinct records captured')
        new_url=next_url(url,links.get('next'))
        if page<total_pages and new_url is None:raise SourceError(f'Next link missing after page {page}/{total_pages}')
        if page>=total_pages and new_url:raise SourceError(f'Unexpected next link after final page {page}/{total_pages}')
        if max_pages and page>=max_pages:
            # Explicit smoke test; NEVER marked complete.
            return rows,{'complete':False,'verification':'SMOKE TEST ONLY - not all pages read','source_count':count,
                         'source_pages':total_pages,'pages_captured':page,'records_captured':len(rows),'pages':pages}
        url=new_url
        if url and delay>0:time.sleep(delay)
    if page!=total_pages or len(rows)!=count:
        raise SourceError(f'Full pagination count mismatch: pages={page}/{total_pages}, rows={len(rows)}/{count}')
    return rows,{'complete':True,'verification':'All provider pages and IDs reconciled',
                 'source_count':count,'source_pages':total_pages,'pages_captured':page,'records_captured':len(rows),'pages':pages}

def write_result(output,rows,meta):
    output.mkdir(parents=True,exist_ok=True)
    m={
        'source_name':'Niva Bupa', 'source':'Niva Bupa official hospital-network public API',
        'source_endpoint':INITIAL,'captured_utc':stamp(),
        'network_scope':'API query scope (nationwide scope not independently verified)',
        'policy_specific_cashless_eligibility_verified':False,
        'no_patient_or_auth_data':True,
        **meta,
    }
    file=output/'niva_hospitals.json'
    file.write_text(json.dumps({'metadata':m,'hospitals':rows},indent=2,ensure_ascii=False),encoding='utf8')
    cities=Counter((str(x.get('provider_state') or '').strip(),str(x.get('provider_city') or '').strip()) for x in rows)
    streams=Counter(str(x.get('network_stream') or 'UNKNOWN') for x in rows)
    stats={'metadata':m,'states':dict(sorted(Counter(x.get('provider_state') or 'UNKNOWN' for x in rows).items())),
           'network_streams':dict(streams),'blank_location_fields':sum(bool(x.get('quality_note')) for x in rows),
           'unique_provider_ids':len({str(x.get('provider_id')) for x in rows if x.get('provider_id') is not None}),
           'distinct_state_city_pairs':len(cities)}
    (output/'audit.json').write_text(json.dumps(stats,indent=2,ensure_ascii=False),encoding='utf8')
    bundle=output.parent/('NetworkIQ_Niva_DirectAPI_COMPLETE.zip' if meta['complete'] else 'NetworkIQ_Niva_DirectAPI_SMOKE_ONLY.zip')
    with zipfile.ZipFile(bundle,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in [file, output/'audit.json']:z.write(p,arcname=p.name)
    return bundle

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,default=Path('Niva_DirectAPI_Results'))
    ap.add_argument('--smoke-pages',type=int,default=0,help='0=full collection (default); 2=first two pages ONLY')
    ap.add_argument('--delay',type=float,default=.35,help='Seconds between official API pages; default 0.35')
    args=ap.parse_args()
    if args.smoke_pages<0 or args.delay<0:ap.error('Positive values required')
    try:
        rows,meta=collect(max_pages=args.smoke_pages,delay=args.delay)
        zipfile=write_result(args.output,rows,meta)
        print(f'RESULT {"COMPLETE" if meta["complete"] else "SMOKE ONLY"}: {zipfile}')
    except Exception as e:
        args.output.mkdir(parents=True,exist_ok=True)
        err={'source':'Niva Bupa','captured_utc':stamp(),'complete':False,'error':str(e),
             'verification':'FAILED - no nationwide hospital feed produced'}
        (args.output/'failure.json').write_text(json.dumps(err,indent=2))
        print('UNVERIFIED —',str(e),file=sys.stderr)
        print('No publishable data was produced. See failure.json.',file=sys.stderr)
        failure_zip=args.output.parent/'NetworkIQ_Niva_DirectAPI_FAILURE.zip'
        with zipfile.ZipFile(failure_zip,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(args.output/'failure.json',arcname='failure.json')
        print('Upload this diagnostic ZIP:',failure_zip,file=sys.stderr)
        raise SystemExit(1)
if __name__=='__main__':main()
