#!/usr/bin/env python3
"""Official IndusInd General SearchList -> NetworkIQ, fail closed.
No policy-level cashless claims and no city/PIN inference. Run in clean staging.
"""
from __future__ import annotations
import argparse,hashlib,json,math,sys,zipfile
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
import collect_indusind as source

def norm(x): return str(x or '').strip()
def to_public(r,idx):
    cat=norm(r.get('type'))
    if cat not in {'IPD','OPD','Both'}:raise ValueError('Unknown hospital network type '+repr(cat))
    status=norm(r.get('status'))
    if status!='1':raise ValueError('Unexpected source network status '+repr(status))
    name=norm(r.get('hospitalName'));address=norm(r.get('address'));state=norm(r.get('state'))
    if not name or not address:raise ValueError('Source name or address missing')
    return {'name':name,'address':address,'state':state,'city':norm(r.get('city')),
        'pin':norm(r.get('pinCode')),'phone':norm(r.get('phone')),'email':norm(r.get('mailId')),
        'network_type':cat,'provider_type':norm(r.get('providerType')),'hospital_grade':norm(r.get('hospitalGradeCategory')),
        'source_status_code':status,'latitude':r.get('latitude') or None,'longitude':r.get('longitude') or None,
        'ins':'IndusInd General Insurance','ins_count':1,'ppn':False,'ppn_insurers':[],
        'spec':'','rating':0,'reference_id':'','id':'',
        'surrogate_fingerprint':source.fingerprint(r),'source_record_index':idx}

def build(raw, report, old):
    rows=raw['hospitals']; total=report.get('totalCount_reported_by_API')
    if not isinstance(rows,list) or not isinstance(total,int) or total!=len(rows) or total<10000:
        raise ValueError('Full source count missing or insufficient')
    if report.get('pages_collected')!=math.ceil(total/source.PAGE_SIZE):
        raise ValueError('Incomplete source page pagination')
    if len(report.get('page_hashes') or {})!=report['pages_collected']:
        raise ValueError('Page-level evidence incomplete')
    if len(rows)!=len({int(r.get('currentIndex',i)) for i,r in enumerate(rows,1)}):
        # currentIndex, where present, is a pagination ordinal, not a stable hospital ID.
        raise ValueError('Repeated source index across pages')
    new=[to_public(row,i) for i,row in enumerate(rows,1)]
    previous=old.get('hospitals',[]);before=len(previous)
    if before>=10000 and not .85*before<=total<=1.15*before:
        raise ValueError(f'Suspicious source total change {before}->{total}')
    kind=dict(Counter(x['network_type'] for x in new))
    m={'source_name':'IndusInd General Insurance','source_endpoint':source.ENDPOINT,
       'retrieved_at_utc':report['captured_at_utc'],'record_count':total,
       'unique_surrogate_fingerprints':report['unique_surrogate_fingerprints'],
       'duplicate_fingerprint_surplus':total-report['unique_surrogate_fingerprints'],
       'missing_city_count':sum(not x['city'] for x in new),
       'missing_state_count':sum(not x['state'] for x in new),'source_status_code':'1',
       'source_network_types':kind,'verification_level':'OFFICIAL_SEARCH_RESULT_ONLY',
       'policy_cashless_verified':False,'stable_source_hospital_id_available':False,
       'refresh_mode':'SCHEDULED_OFFICIAL_API_SNAPSHOT',
       'note':'Source listing; city may be missing. Confirm cashless status with insurer.'}
    return {'metadata':m,'hospitals':new}

def verify_live_bookends(raw,report):
    # Count and payload parity at start and end. Source lacks an atomic snapshot token.
    rows=raw['hospitals'];total=len(rows);pages=math.ceil(total/source.PAGE_SIZE)
    for page,expected in [(1,rows[:source.PAGE_SIZE]),(pages,rows[(pages-1)*source.PAGE_SIZE:])]:
        obj=source.retry_fetch(page,source.http_fetch)
        n,observed=source.check_page(obj,page,total)
        if n!=total or source.page_digest(observed)!=source.page_digest(expected):
            raise ValueError('IndusInd source changed during collection; refusing mixed snapshot')

def refresh(old_path,stage,workdir,out,live=True):
    stage.parent.mkdir(parents=True,exist_ok=True)
    ok,audit=source.collect(workdir,stage)
    if not ok:raise ValueError('Source collection incomplete')
    with zipfile.ZipFile(stage) as z:
        raw=json.loads(z.read('indusind_network_hospitals.json'))
        report=json.loads(z.read('indusind_collection_report.json'))
    previous=json.loads(Path(old_path).read_text(encoding='utf-8'))
    candidate=build(raw,report,previous)
    if live:verify_live_bookends(raw,report)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(candidate,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print('INDUSIND_SOURCE_COMPLETE_AND_VERIFIED records='+str(len(candidate['hospitals'])))
    return candidate

def main():
    p=argparse.ArgumentParser();p.add_argument('--previous',type=Path,default=Path('data/indusind_general.json'))
    p.add_argument('--staging-dir',type=Path,default=Path('build/indusind'))
    p.add_argument('--output',type=Path,default=Path('data/indusind_general.json'))
    a=p.parse_args()
    # Output is touched only after ALL live and schema checks pass.
    return refresh(a.previous,a.staging_dir/'source.zip',a.staging_dir/'pages',a.output)
if __name__=='__main__':
    try:main()
    except Exception as e:print('INDUSIND_NO_PUBLISH '+type(e).__name__+': '+str(e),file=sys.stderr);sys.exit(2)
