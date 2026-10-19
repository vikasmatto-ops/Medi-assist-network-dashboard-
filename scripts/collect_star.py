#!/usr/bin/env python3
"""NetworkIQ Star Health public hospital source, version 3.
Direct HTTPS Next.js Server Actions only, no browser, user credentials or bypass.
All data stays in staging. A separate quality gate controls publishing.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

import star_transport as base

STATES=base.STATES
CATEGORIES=('Network','agreed','value_network','vsp','ayush','nonpreferred')
SOURCE='Star Health'
SOURCE_URL=base.PAGE
MAX_PAGES=700
MARKER='NETWORKIQ_STAR_V3'

def utc():return dt.datetime.now(dt.timezone.utc).isoformat()
def jdump(o):return json.dumps(o,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(o):return hashlib.sha256(jdump(o).encode('utf-8')).hexdigest()
def norm(x):return re.sub(r'\s+',' ',str(x or '')).strip()
def yn(x):
    if isinstance(x,bool):return x
    s=norm(x).upper()
    if s in ('YES','Y','TRUE','1'):return True
    if s in ('NO','N','FALSE','0',''):return False
    raise ValueError('Unexpected Star yes/no flag '+repr(x)[:65])

class SourceError(RuntimeError):pass

def parse_hospital_response(blob,page_size):
    frames,errors=base.parse_flight(blob)
    if errors:raise SourceError('Server Action error frame: '+errors[0])
    obj=base.first_obj(frames)
    if not isinstance(obj,dict):raise SourceError('Missing expected Flight data frame')
    if obj.get('errorDesc'):raise SourceError('Source error: '+str(obj['errorDesc'])[:180])
    if str(obj.get('responseCode','200'))!='200' or str(obj.get('description','success')).lower()!='success':
        raise SourceError('Unexpected source status in Flight data frame')
    data=obj.get('data') if isinstance(obj.get('data'),dict) and 'details' in obj['data'] else obj
    records=data.get('details')
    if not isinstance(records,list) or not all(isinstance(r,dict) for r in records):
        raise SourceError('Hospital details missing or malformed')
    try:count=int(data.get('count'))
    except (ValueError,TypeError):raise SourceError('Missing/invalid count')
    if count<0 or len(records)>page_size:raise SourceError('Contradictory source count or page size')
    pages=math.ceil(count/page_size) if count else 0
    explicit=data.get('totalPages',data.get('totalpages'))
    if explicit is not None and int(explicit)!=pages:raise SourceError('Reported totalPages contradicts count')
    return records,count,pages

def source_action(action,args,page_size,retries=2):
    payload=json.dumps(args,ensure_ascii=False,separators=(',',':')).encode()
    headers={'Next-Action':action,'Content-Type':'text/plain;charset=UTF-8','Accept':'text/x-component',
             'Origin':base.ROOT,'Referer':base.PAGE}
    for i in range(retries+1):
        try:
            status,ct,body=base.fetch(base.PAGE,'POST',payload,headers)
            if status in (401,403,404,429):raise SourceError(f'HTTP {status}; pause, do not bypass site controls')
            if status in (500,502,503,504) and i<retries:
                time.sleep(2**i);continue
            if status!=200:raise SourceError(f'HTTP {status}: {body[:120]}')
            if 'text/x-component' not in ct:raise SourceError('Unexpected Flight content type: '+ct)
            return parse_hospital_response(body,page_size)
        except (urllib.error.URLError,TimeoutError) as e:
            if i==retries:raise SourceError('Network error after bounded retry: '+str(e)[:170]) from e
            time.sleep(2**i)

def page(action,category,state,page_no,per_page=10):
    args=base.make_args(state=state,category=category,page=page_no,page_size=per_page)
    return source_action(action,args,per_page)

def raw_identity(row):
    # Star does not expose a provider ID in the verified rescue response.
    # Do not use phone/latitude alone as a facility ID.
    values=[norm(row.get(x)).casefold() for x in ('hospitalName','address','cityName','stateName','pinCode')]
    if not all(values):raise SourceError('Hospital missing name/address/city/state/PIN')
    if not re.fullmatch(r'\d{6}',values[4]):raise SourceError('Invalid PIN '+repr(values[4]))
    return digest(values)[:32]

def make_record(row,category,requested_state):
    ident=raw_identity(row)
    origin_state=norm(row['stateName'])
    if origin_state.casefold()!=requested_state.casefold():
        raise SourceError(f'Source state mismatch: requested {requested_state} returned {origin_state}')
    flags={
      'excluded':yn(row.get('excludedHospital')) or category=='nonpreferred',
      'ayush':yn(row.get('ayushHospital')) or category=='ayush',
      'vsp':yn(row.get('isVsp')) or yn(row.get('valuableserviceProvider')) or category=='vsp',
      'pratham':yn(row.get('isPratham')),
      'smart_network':yn(row.get('smartNetwork')),
      'homecare':yn(row.get('homecareHospital')),
    }
    return {
      'id':'star_'+ident,'reference_id':ident,
      'name':norm(row['hospitalName']),'address':norm(row['address']),
      'city':norm(row['cityName']),'state':origin_state,'pin':norm(row['pinCode']),
      'phone':norm(row.get('phoneNo')) or norm(row.get('mobileNumber')),
      'latitude':norm(row.get('latitude')),'longitude':norm(row.get('longitude')),
      'network_type':norm(row.get('networkType')),
      'source_hospital_type':norm(row.get('hospitalType')),
      'star_categories':[category],'star_flags':flags,
      'ins':'Star Health','ins_count':1,'spec':'',
      'ppn':False,'ppn_insurers':[],
      'policy_specific_cashless_eligibility_verified':False,
    }

def merge_records(dst,src):
    for key in ('id','name','address','city','state','pin'):
        if dst[key]!=src[key]:raise SourceError('Identity conflict in merged source hospital')
    dst['star_categories']=sorted(set(dst['star_categories']+src['star_categories']))
    for flag,val in src['star_flags'].items():dst['star_flags'][flag]=dst['star_flags'][flag] or val
    for key in ('phone','latitude','longitude','network_type','source_hospital_type'):
        if not dst[key]:dst[key]=src[key]
    return dst

def write_atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)

def checkpoint_key(category,state):
    return re.sub('[^a-z0-9]+','_',category+'_'+state).strip('_').lower()

def collect_query(action,category,state,out,page_size=10,delay=.5):
    target=out/'checkpoints'/checkpoint_key(category,state)
    target.mkdir(parents=True,exist_ok=True)
    # Always refresh page 1: detect changed ordering/count before reusing old pages.
    first,count,pages=page(action,category,state,1,page_size)
    if count==0:
        if first:raise SourceError('Nonempty page while count=0')
        return [],{'count':0,'pages':0,'captured':0,'distinct':0}
    if pages>MAX_PAGES:raise SourceError(f'Pagination safety cap exceeded ({pages})')
    expected_first=min(page_size,count)
    if len(first)!=expected_first:raise SourceError('First page truncated')
    meta={'schema':MARKER,'category':category,'state':state,'page_size':page_size,
          'action_sha':hashlib.sha256(action.encode()).hexdigest(),
          'count':count,'pages':pages,'first_page_hash':digest(first)}
    meta_path=target/'_meta.json'
    old=None
    if meta_path.exists():
        try:old=json.loads(meta_path.read_text(encoding='utf-8'))
        except (ValueError,OSError):pass
    if old!=meta:
        for old_page in target.glob('page_*.json'):old_page.unlink()
        write_atomic(meta_path,meta)
    write_atomic(target/'page_0001.json',{'meta_sha':digest(meta),'rows':first,'count':count,'pages':pages,'page':1})
    all_rows=list(first)
    signatures={digest(first)}
    identities={raw_identity(r) for r in first}
    if len(identities)!=len(first):raise SourceError('Duplicate hospitals on first page')
    for p in range(2,pages+1):
        checkpoint=target/f'page_{p:04d}.json'
        cached=None
        if checkpoint.exists():
            try:
                old_page=json.loads(checkpoint.read_text(encoding='utf-8'))
                if old_page.get('meta_sha')==digest(meta) and old_page.get('page')==p:
                    cached=old_page
            except (ValueError,OSError):pass
        if cached:
            rows,n,m=cached['rows'],cached['count'],cached['pages']
        else:
            time.sleep(max(.1,delay))
            rows,n,m=page(action,category,state,p,page_size)
            write_atomic(checkpoint,{'meta_sha':digest(meta),'rows':rows,'count':n,'pages':m,'page':p})
        if n!=count or m!=pages:raise SourceError('Source count/page count changed during query')
        if len(rows)!=min(page_size,count-(p-1)*page_size):raise SourceError(f'Truncated page {p}/{pages}')
        sig=digest(rows)
        if sig in signatures:raise SourceError(f'Repeated page payload at {p}/{pages}')
        signatures.add(sig)
        for r in rows:
            rid=raw_identity(r)
            if rid in identities:raise SourceError(f'Duplicate hospital across pages: {p}/{pages}')
            identities.add(rid)
        all_rows.extend(rows)
        if p%50==0 or p==pages:print(category,state,f'{p}/{pages}, rows {len(all_rows)}/{count}',flush=True)
    if len(all_rows)!=count or len(identities)!=count:raise SourceError('Query completeness reconciliation failed')
    return [make_record(r,category,state) for r in all_rows],{'count':count,'pages':pages,'captured':len(all_rows),'distinct':len(identities)}

def pilot(action):
    checked={}
    for key,state,city in [('delhi','Delhi',''),('new_delhi','','New Delhi'),('maharashtra','Maharashtra','')]:
        args=base.make_args(state=state,city=city,page=1,page_size=10)
        rows,count,pages=source_action(action,args,10)
        checked[key]={'count':count,'pages':pages,'page1_rows':len(rows)}
        if count and len(rows)!=min(10,count):raise SourceError(key+' first page truncated')
        if pages>1:
            args[5]=2
            second,n,m=source_action(action,args,10)
            if count!=n or pages!=m or not second:raise SourceError(key+' page2 inconsistent')
            a={raw_identity(r) for r in rows};b={raw_identity(r) for r in second}
            if len(a)!=len(rows) or len(b)!=len(second) or a.intersection(b):
                raise SourceError(key+' page2 overlaps page1')
            checked[key]['page2_rows']=len(second)
            checked[key]['page2_distinct']=True
    return checked

def collect(out,pilot_only=False,delay=.5,page_size=10):
    out.mkdir(parents=True,exist_ok=True)
    report={'version':MARKER,'source':SOURCE_URL,'captured_at_utc':utc(),'complete':False,
            'published':False,'status':'RUNNING','pilot':{},'queries':{},'errors':[]}
    try:
        action,asset=base.get_live_action_id()
        report['action_sha256']=hashlib.sha256(action.encode()).hexdigest()
        report['current_source_js']=asset
        report['pilot']=pilot(action)
        if pilot_only:
            report['status']='PILOT_PAGE2_VERIFIED'
            return report
        gathered={}; category_counts=Counter()
        for cat in CATEGORIES:
            for i,state in enumerate(STATES,1):
                print(f'[{cat} {i}/{len(STATES)}] {state}',flush=True)
                try:
                    rows,audit=collect_query(action,cat,state,out,page_size,delay)
                except Exception as exc:
                    report['errors'].append({'category':cat,'state':state,'error':str(exc)[:350]})
                    report['status']='PAUSED_INCOMPLETE_CHECKPOINTS'
                    return report
                report['queries'][cat+'|'+state]=audit
                category_counts[cat]+=len(rows)
                for row in rows:
                    if row['id'] in gathered:merge_records(gathered[row['id']],row)
                    else:gathered[row['id']]=row
        if len(report['queries'])!=len(STATES)*len(CATEGORIES):
            raise SourceError('Incomplete category/state matrix')
        if category_counts['Network']<5000:raise SourceError('Network count abnormally low; validate first')
        for category in CATEGORIES[1:]:
            if category_counts[category]==0:
                raise SourceError(f'Category {category} returned zero nationwide; verify source routing before publication')
        records=sorted(gathered.values(),key=lambda r:(r['state'],r['city'],r['name'],r['id']))
        eligible=[r for r in records if not r['star_flags']['excluded']]
        excluded=[r for r in records if r['star_flags']['excluded']]
        if len(eligible)<5000:raise SourceError('Eligible-source-listing count implausibly low')
        if not any(r['star_flags']['vsp'] for r in records):raise SourceError('No VSP flags found; verify source/category contract')
        m={'source_name':SOURCE,'source_url':SOURCE_URL,'captured_utc':utc(),
           'complete':True,'network_scope':'All 36 states and union territories queried in all 6 source categories',
           'state_count':len(STATES),'states_queried':STATES,'categories_queried':list(CATEGORIES),
           'query_count':len(report['queries']),'expected_query_count':len(STATES)*len(CATEGORIES),
           'raw_records_per_category':dict(category_counts),
           'source_count':len(eligible),'excluded_count':len(excluded),'total_unique_across_categories':len(records),
           'policy_specific_cashless_eligibility_verified':False,
           'verified_page2_distinct':all(x.get('page2_distinct') for x in report['pilot'].values() if x['pages']>1),
           'dedupe_key':'source name/address/city/state/PIN',
           'validation':'page count derived from ceil(count/page_size); every category/state page and distinct ID reconciled',
           'listing_disclaimer':'Star official public directory only; eligibility and hospital status require insurer/plan confirmation.'}
        # Write to staging, never to the repository's live data path.
        write_atomic(out/'star_health_candidate.json',{'metadata':m,'hospitals':eligible})
        write_atomic(out/'star_excluded_candidate.json',{'metadata':{**m,'source_count':len(excluded),'record_type':'EXCLUDED_ONLY'},'hospitals':excluded})
        report['status']='ALL_216_QUERIES_STAGED_VALIDATED'
        report['complete']=True
        report['summary']={'source_count':len(eligible),'excluded_count':len(excluded),
            'raw_records_per_category':dict(category_counts),'unique_across_categories':len(records)}
        return report
    except Exception as exc:
        report['status']='FAILED_CLOSED'
        report['errors'].append({'stage':'bootstrap_or_validation','error':repr(exc)[:500]})
        return report
    finally:
        write_atomic(out/'star_audit.json',report)
        print('Star:',report['status'],flush=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',type=Path,default=Path('build/star'))
    ap.add_argument('--pilot',action='store_true',help='Verify official API page1/page2 without nationwide run')
    ap.add_argument('--delay',type=float,default=.6)
    ap.add_argument('--page-size',type=int,default=10)
    args=ap.parse_args()
    if not 1<=args.page_size<=10:ap.error('Only demonstrated page sizes 1..10 are permitted')
    report=collect(args.out,pilot_only=args.pilot,delay=args.delay,page_size=args.page_size)
    return 0 if report['status'] in ('PILOT_PAGE2_VERIFIED','ALL_216_QUERIES_STAGED_VALIDATED') else 2

if __name__=='__main__':sys.exit(main())
