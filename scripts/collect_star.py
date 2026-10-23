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
import os
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
PARTITION_MAX_ATTEMPTS=3  # bounded: retry only this category/state, never whole country
CHECKPOINT_MAX_AGE_SECONDS=3600  # never trust stale pages from previous days
STABILITY_RECHECK_MIN_PAGES=20  # large, change-prone state partitions
MARKER='NETWORKIQ_STAR_V6'
PATCH_VERSION='V9_PARTITION_EPOCH_RETRY'
# The public source has used differing geographic spellings. Never infer that a
# failed name filter means zero hospitals; reconcile ALL category totals globally.
STATE_ALIASES={
  'Andaman and Nicobar Islands':('Andaman & Nicobar Island','Andaman & Nicobar Islands','Andaman and Nicobar Island'),
  'Dadra and Nagar Haveli and Daman and Diu':('Dadra & Nagar Haveli and Daman & Diu',),
  'Jammu and Kashmir':('Jammu & Kashmir',),
  'Puducherry':('Pondicherry',),
  'Odisha':('Orissa',),
  'Uttarakhand':('Uttaranchal',),
  'Chhattisgarh':('Chattisgarh',),
  'Delhi':('New Delhi',),
}
# These are small, explicit mappings, NOT an unrestricted search for alternate APIs.

def source_state_key(v):
    s=norm(v).casefold().replace('&','and')
    s=re.sub(r'\s+',' ',s).strip()
    if s in ('andaman and nicobar island','andaman and nicobar islands'):
        return 'andaman and nicobar islands'
    if s in ('pondicherry','puducherry'):return 'puducherry'
    if s in ('orissa','odisha'):return 'odisha'
    if s in ('uttaranchal','uttarakhand'):return 'uttarakhand'
    if s in ('chattisgarh','chhattisgarh'):return 'chhattisgarh'
    if s in ('jammu and kashmir','jammu kashmir'):return 'jammu and kashmir'
    if s in ('new delhi','delhi'):return 'delhi'
    return s



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
class UnverifiedSourceStatus(SourceError):pass
class PartitionDrift(SourceError):pass

def parse_hospital_response(blob,page_size):
    frames,errors=base.parse_flight(blob)
    if errors:raise SourceError('Server Action error frame: '+errors[0])
    obj=base.first_obj(frames)
    if not isinstance(obj,dict):raise SourceError('Missing expected Flight data frame')
    if obj.get('errorDesc'):raise SourceError('Star Server Action errorDesc: '+str(obj['errorDesc'])[:180])
    code=str(obj.get('responseCode','<missing>'))
    description=norm(obj.get('description','<missing>'))[:90]
    # Star's *observed* API contract: a genuine empty filter responds with
    # 2012 / Provider Details Not Available / count=0 / details=[]. Do not
    # treat other application errors, malformed data or nonzero counts as zero.
    if code=='2012' and description.casefold()=='provider details not available':
        raw_count=obj.get('count')
        if str(raw_count)=='0' and obj.get('details')==[]:
            return [],0,0
        raise SourceError('Star empty-result status has inconsistent count/details')
    if code!='200' or description.lower()!='success':
        if code.startswith('5'):
            raise SourceError('Star backend failure: responseCode='+repr(code)+' description='+repr(description))
        # First-party application status, not a successful zero-hit response.
        # May be a missing state/category, a source error or something else.
        # Only an independent, successful global count can confirm completeness.
        raise UnverifiedSourceStatus('Star source responseCode='+repr(code)+
             ' description='+repr(description)+' count='+repr(obj.get('count','<missing>'))[:32]+
             ' details_type='+type(obj.get('details')).__name__)
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
    if not isinstance(args,list) or len(args)!=7 or not any(norm(x) for x in args[:3]):
        raise SourceError('Star hospital API requires hospital name, city or state; blank nationwide query refused')
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
            try:
                return parse_hospital_response(body,page_size)
            except SourceError as exc:
                # On rare live requests Star returns 200 Flight content without
                # the expected data frame. Retry this exact transient response
                # before failing closed. Never treat it as an empty hospital page.
                if str(exc) != 'Missing expected Flight data frame':
                    raise
                if i == retries:
                    raise SourceError(
                        'Missing expected Flight data frame after '
                        + str(retries+1) + ' bounded attempts; source unavailable'
                    ) from exc
                print('STAR_TRANSIENT_FLIGHT_RETRY category='+str(args[3])+
                      ' state='+str(args[0])+' page='+str(args[5])+
                      ' attempt='+str(i+1)+'/'+str(retries+1),flush=True)
                time.sleep(2**i)
                continue
        except (urllib.error.URLError,TimeoutError) as e:
            if i==retries:raise SourceError('Network error after bounded retry: '+str(e)[:170]) from e
            time.sleep(2**i)

def page(action,category,state,page_no,per_page=10):
    args=base.make_args(state=state,category=category,page=page_no,page_size=per_page)
    return source_action(action,args,per_page)

IDENTITY_FIELDS=('hospitalName','address','cityName','stateName','pinCode')

def source_issues(row,requested_state=None):
    issues=[f'missing_{k}' for k in IDENTITY_FIELDS if not norm(row.get(k))]
    pin=norm(row.get('pinCode'))
    if pin and not re.fullmatch(r'\d{6}',pin):issues.append('invalid_pinCode')
    source_state=norm(row.get('stateName'))
    if requested_state and source_state and source_state_key(source_state)!=source_state_key(requested_state):
        issues.append('state_mismatch')
    return issues

def raw_identity(row):
    values=[norm(row.get(k)).casefold() for k in IDENTITY_FIELDS]
    if not source_issues(row):return digest(values)[:32]
    # A quarantine-only stable identity: never invent missing location/name.
    additional=[norm(row.get(k)).casefold() for k in
                ('phoneNo','mobileNumber','latitude','longitude','hospitalType')]
    return 'q_'+digest(values+additional)[:30]

def make_record(row,category,requested_state):
    ident=raw_identity(row)
    issues=source_issues(row,requested_state)
    if 'state_mismatch' in issues:
        raise SourceError('Source state mismatch: '+norm(row.get('stateName'))+' vs '+requested_state)
    raw_excluded=row.get('excludedHospital')
    if not isinstance(raw_excluded,bool) and norm(raw_excluded).upper() not in (
            'YES','Y','TRUE','1','NO','N','FALSE','0'):
        issues.append('missing_or_invalid_exclusion_flag')
    def safe_flag(key):
        try:return yn(row.get(key))
        except ValueError:
            issues.append('invalid_flag_'+key)
            return False
    flags={
      'excluded':safe_flag('excludedHospital') or category=='nonpreferred',
      'ayush':safe_flag('ayushHospital') or category=='ayush',
      'vsp':safe_flag('isVsp') or safe_flag('valuableserviceProvider') or category=='vsp',
      'pratham':safe_flag('isPratham'),
      'smart_network':safe_flag('smartNetwork'),
      'homecare':safe_flag('homecareHospital'),
    }
    record={
      'id':'star_'+ident,'reference_id':ident,
      'name':norm(row.get('hospitalName')),'address':norm(row.get('address')),
      'city':norm(row.get('cityName')),'state':norm(row.get('stateName')),
      'pin':norm(row.get('pinCode')),
      'phone':norm(row.get('phoneNo')) or norm(row.get('mobileNumber')),
      'latitude':norm(row.get('latitude')),'longitude':norm(row.get('longitude')),
      'network_type':norm(row.get('networkType')),
      'source_hospital_type':norm(row.get('hospitalType')),
      'star_categories':[category],'star_flags':flags,
      'ins':'Star Health','ins_count':1,'spec':'',
      'ppn':False,'ppn_insurers':[],
      'policy_specific_cashless_eligibility_verified':False,
    }
    if issues:
        record['source_identity_issues']=sorted(set(issues))
        record['source_review_row']=row
        record['source_filter_for_review']=requested_state
    return record

def source_city_missing_safe(row):
    """Only permit source-blank city with strong alternative official identity.

    The source city remains blank. No city is inferred from PIN/address/coordinates.
    Other malformed identities or unknown exclusion classifications fail closed.
    """
    if row.get('source_identity_issues')!=['missing_cityName'] or row.get('city')!='':
        return False
    if not all(norm(row.get(k)) for k in ('name','address','state','pin')):
        return False
    if not re.fullmatch(r'\d{6}',norm(row['pin'])):
        return False
    try:
        lat=float(row.get('latitude') or '')
        lon=float(row.get('longitude') or '')
    except (TypeError, ValueError):
        return False
    return 6.0 <= lat <= 38.5 and 68.0 <= lon <= 98.5

def merge_records(dst,src):
    for key in ('id','name','address','city','state','pin'):
        if dst[key]!=src[key]:raise SourceError('Identity conflict in merged source hospital')
    dst['star_categories']=sorted(set(dst['star_categories']+src['star_categories']))
    for flag,val in src['star_flags'].items():dst['star_flags'][flag]=dst['star_flags'][flag] or val
    for key in ('phone','latitude','longitude','network_type','source_hospital_type'):
        if not dst[key]:dst[key]=src[key]
    if src.get('source_identity_issues'):
        dst['source_identity_issues']=sorted(set(dst.get('source_identity_issues',[])+src['source_identity_issues']))
        dst['source_review_row']=src['source_review_row']
        dst['source_filter_for_review']=src.get('source_filter_for_review','')
    return dst

def write_atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)

def checkpoint_key(category,state):
    return re.sub('[^a-z0-9]+','_',category+'_'+state).strip('_').lower()

def _collect_query_once(action,category,state,out,page_size=10,delay=.5):
    target=out/'checkpoints'/checkpoint_key(category,state)
    target.mkdir(parents=True,exist_ok=True)
    # Always refresh page 1: detect changed ordering/count before reusing old pages.
    first,count,pages=page(action,category,state,1,page_size)
    if count==0:
        if first:raise SourceError('Nonempty page while count=0')
        return [],{'count':0,'pages':0,'captured':0,'distinct':0}
    if pages>MAX_PAGES:raise SourceError(f'Pagination safety cap exceeded ({pages})')
    expected_first=min(page_size,count)
    if len(first)!=expected_first:raise PartitionDrift('First page truncated')
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
                # Cache must be recent as well as matching the first-page count/hash.
                # A matching first page does NOT make a two-day-old middle page safe.
                age=time.time()-checkpoint.stat().st_mtime
                if (0<=age<=CHECKPOINT_MAX_AGE_SECONDS and
                    old_page.get('meta_sha')==digest(meta) and old_page.get('page')==p):
                    cached=old_page
            except (ValueError,OSError):pass
        if cached:
            rows,n,m=cached['rows'],cached['count'],cached['pages']
        else:
            time.sleep(max(.1,delay))
            rows,n,m=page(action,category,state,p,page_size)
        if n!=count or m!=pages:
            raise PartitionDrift(f'Source count/page count changed during query: '
                 f'category={category} state={state} page={p}/{pages} '
                 f'first_count={count} page_count={n} first_pages={pages} page_pages={m}')
        # Only save a checkpoint after verifying it belongs to this count epoch.
        if cached is None:
            write_atomic(checkpoint,{'meta_sha':digest(meta),'rows':rows,'count':n,'pages':m,'page':p})
        if len(rows)!=min(page_size,count-(p-1)*page_size):raise PartitionDrift(f'Truncated page {p}/{pages}')
        sig=digest(rows)
        if sig in signatures:raise PartitionDrift(f'Repeated page payload at {p}/{pages}')
        signatures.add(sig)
        for r in rows:
            rid=raw_identity(r)
            if rid in identities:raise PartitionDrift(f'Duplicate hospital across pages: {p}/{pages}')
            identities.add(rid)
        all_rows.extend(rows)
        if p%50==0 or p==pages:print(category,state,f'{p}/{pages}, rows {len(all_rows)}/{count}',flush=True)
    if len(all_rows)!=count or len(identities)!=count:raise PartitionDrift('Query completeness reconciliation failed')
    # Offset-paginated live API has no snapshot cursor. Confirm the bookends
    # still match after a large traversal, instead of accepting a changed epoch.
    if pages>=STABILITY_RECHECK_MIN_PAGES:
        # Five distributed probes guard against same-count reorderings in a
        # mutable offset-paginated endpoint (not just a changed total count).
        probe_numbers=sorted({1,pages,max(1,math.ceil(pages*.25)),
                              max(1,math.ceil(pages*.50)),
                              max(1,math.ceil(pages*.75))})
        for probe in probe_numbers:
            expected=all_rows[(probe-1)*page_size:probe*page_size]
            observed,n,m=page(action,category,state,probe,page_size)
            if n!=count or m!=pages or digest(observed)!=digest(expected):
                raise PartitionDrift(
                    f'State pagination changed during final verification: '
                    f'category={category} state={state} probe={probe}/{pages} '
                    f'first_count={count} now_count={n} first_pages={pages} now_pages={m}')
    return [make_record(r,category,state) for r in all_rows],{'count':count,'pages':pages,'captured':len(all_rows),'distinct':len(identities)}


def _invalidate_partition(out,category,state):
    # Restrict cleanup to this one category/state; preserve all other checkpoints.
    target=out/'checkpoints'/checkpoint_key(category,state)
    for old in target.glob('page_*.json'):old.unlink()
    meta=target/'_meta.json'
    if meta.exists():meta.unlink()


def collect_query(action,category,state,out,page_size=10,delay=.5):
    last=None
    for attempt in range(1,PARTITION_MAX_ATTEMPTS+1):
        try:
            result=_collect_query_once(action,category,state,out,page_size,delay)
            if attempt>1:
                print('STAR_PARTITION_RECOVERED '+category+'|'+state+
                      ' stable_attempt='+str(attempt),flush=True)
            return result
        except PartitionDrift as exc:
            last=exc
            print('STAR_PARTITION_COUNT_DRIFT '+category+'|'+state+
                  ' attempt='+str(attempt)+'/'+str(PARTITION_MAX_ATTEMPTS)+
                  ' reason='+str(exc)[:400],flush=True)
            _invalidate_partition(out,category,state)
            if attempt<PARTITION_MAX_ATTEMPTS:
                time.sleep(min(12.,max(0.,delay)*4*attempt))
    raise SourceError('Partition unstable after '+str(PARTITION_MAX_ATTEMPTS)+
        ' complete attempts; refused mixed-epoch data: '+str(last))


def collect_state_with_aliases(action,category,canonical,out,page_size=10,delay=.5):
    # Backwards-compatible with the initial pipeline: only supported explicit
    # server-action status errors cause bounded alias attempts. A malformed or
    # partially paginated result is fatal and is NEVER allowed to disappear.
    attempted=[];verified_empty=None
    for state in dict.fromkeys((canonical,)+STATE_ALIASES.get(canonical,())):
        try:
            rows,audit=collect_query(action,category,state,out,page_size,delay)
            audit['source_state_filter']=state
            audit['status']='SOURCE_REPORTED_ZERO' if audit['count']==0 else 'COMPLETE'
            if audit['count']==0:
                # A successful zero is evidence for this exact spelling only.
                # Check known aliases before concluding the territory is empty.
                verified_empty=(rows,audit)
                continue
            return rows,audit
        except UnverifiedSourceStatus as exc:
            attempted.append(state+': '+str(exc))
            print('STAR_STATE_ALIAS_RETRY category='+category+' state='+state+' reason='+str(exc),flush=True)
    # This 2020 merged UT may still be partitioned as TWO names in the insurer's
    # source. Collect BOTH, and don't allow a single accepted part to hide the other.
    if canonical=='Dadra and Nagar Haveli and Daman and Diu':
        parts=(('Dadra & Nagar Haveli','Dadra and Nagar Haveli'),
               ('Daman & Diu','Daman and Diu'))
        combined=[]; used=[]; pages=0; had_unresolved=False
        for alternatives in parts:
            part_ok=False
            for alias in alternatives:
                try:
                    records,audit=collect_query(action,category,alias,out,page_size,delay)
                    combined.extend(records)
                    pages+=audit['pages'];used.append(alias);part_ok=True
                    break
                except UnverifiedSourceStatus as exc:
                    attempted.append(alias+': '+str(exc))
            if not part_ok:had_unresolved=True
        if combined:
            unique={r['id'] for r in combined}
            if len(unique)!=len(combined):
                raise SourceError('Split merged territory returned overlapping records')
            return combined,{'count':len(combined),'pages':pages,'captured':len(combined),
                 'distinct':len(combined),'status':'UNVERIFIED_FILTER' if had_unresolved else 'COMPLETE',
                 'source_state_filter':used,'attempts':attempted}
    if verified_empty is not None:
        # A successful Star 2012/0/[] is a *source-reported no-match* for this
        # filter; no claim that the state has zero hospitals beyond this feed.
        return verified_empty
    # No data were fetched for this partition. It MUST NOT be assumed empty.
    # Caller may only approve if exact unfiltered national count reconciles.
    return [],{'count':0,'pages':0,'captured':0,'distinct':0,
               'status':'UNVERIFIED_FILTER','source_state_filter':None,
               'attempts':attempted}

def verify_source_state_filters(action, page_size=10, delay=.35):
    """Request only valid state-filtered public API pages; NEVER query all blanks.

    Capture every unresolved state, not just the first. Even if 35 succeed,
    36th missing means no nationwide publication. This verifies label acceptance,
    NOT a server-supplied nationwide record total (the API offers no such query).
    """
    found={}
    unresolved={}
    for canonical in STATES:
        names=(canonical,)+STATE_ALIASES.get(canonical,())
        if canonical=='Dadra and Nagar Haveli and Daman and Diu':
            names+=('Dadra & Nagar Haveli','Dadra and Nagar Haveli','Daman & Diu','Daman and Diu')
        accepted=[]; failures=[]
        for st in dict.fromkeys(names):
            if delay:time.sleep(max(0,delay))  # bounded first-party request rate
            try:
                rows,count,pages=page(action,'Network',st,1,page_size)
                if count and not rows:raise SourceError('Empty page for positive count')
                accepted.append({'name':st,'count':count,'page1':len(rows),
                                 'source_result':'SOURCE_REPORTED_ZERO' if count==0 else 'FOUND'})
                # Do not stop after the first positive answer for combined territories,
                # because the insurer may split the official 2020 union territory.
                if canonical!='Dadra and Nagar Haveli and Daman and Diu' and count>0:break
            except UnverifiedSourceStatus as e:
                failures.append({'state':st,'status':str(e)[:200]})
        # A combined UT split is complete only if BOTH constituent labels work,
        # or the unsplit combined state filter itself returns positive records.
        if canonical=='Dadra and Nagar Haveli and Daman and Diu' and accepted:
            full={canonical,'Dadra & Nagar Haveli and Daman & Diu'}
            accepted_labels={x['name'] for x in accepted if x['count']>0}
            if not accepted_labels.intersection(full):
                has_dadra=bool(accepted_labels.intersection({'Dadra & Nagar Haveli','Dadra and Nagar Haveli'}))
                has_daman=bool(accepted_labels.intersection({'Daman & Diu','Daman and Diu'}))
                if not (has_dadra and has_daman):
                    failures.append({'status':'Both split territories not verified'})
                    accepted=[]
        if accepted:
            found[canonical]={'status':('SOURCE_REPORTED_ZERO' if all(x['count']==0 for x in accepted)
                                      else 'FILTER_ACCEPTED'), 'accepted':accepted}
        else:
            unresolved[canonical]=failures or [{'status':'No accepted state filter'}]
        print('STAR_STATE_PREFLIGHT '+canonical+' '+
              ('ACCEPTED' if canonical in found else 'UNVERIFIED'),flush=True)
    return {'accepted':found,'unresolved':unresolved,
            'accepted_states':len(found),'expected_states':len(STATES),
            'method':'Mandatory source state filters; unfiltered nationwide query is invalid'}

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
    report={'version':MARKER,'patch':PATCH_VERSION,'source':SOURCE_URL,'captured_at_utc':utc(),'complete':False,
            'published':False,'status':'RUNNING','pilot':{},'queries':{},'errors':[]}
    try:
        action,asset=base.get_live_action_id()
        report['action_sha256']=hashlib.sha256(action.encode()).hexdigest()
        report['current_source_js']=asset
        report['pilot']=pilot(action)
        if pilot_only:
            report['status']='PILOT_PAGE2_VERIFIED'
            return report
        # Star rejects every API query with no hospital/city/state filter.
        # Run source-state preflight instead of the impossible blank-state probe.
        report['state_filter_preflight']=verify_source_state_filters(action,page_size)
        bad=report['state_filter_preflight']['unresolved']
        if bad:
            print('STAR_UNRESOLVED_STATES '+jdump(bad)[:8000],flush=True)
            raise SourceError('Official source state filters could not be verified: '+
                              ', '.join(bad)+'. No nationwide publication.')
        report['source_zero_states']=[st for st,item in report['state_filter_preflight']['accepted'].items()
                                      if item['status']=='SOURCE_REPORTED_ZERO']
        print('STAR_ALL_STATE_FILTERS_ACCEPTED '+str(len(STATES))+
              ' source_reported_zero='+jdump(report['source_zero_states']),flush=True)
        gathered={};category_counts=Counter();category_ids=defaultdict(set);unverified=[]
        for cat in CATEGORIES:
            for i,state in enumerate(STATES,1):
                print(f'[{cat} {i}/{len(STATES)}] {state}',flush=True)
                try:
                    rows,audit=collect_state_with_aliases(action,cat,state,out,page_size,delay)
                except Exception as exc:
                    report['errors'].append({'category':cat,'state':state,'error':str(exc)[:350]})
                    print('STAR_PARTITION_STOP '+cat+'|'+state+' '+type(exc).__name__+': '+str(exc)[:250],file=sys.stderr,flush=True)
                    report['status']='PAUSED_INCOMPLETE_CHECKPOINTS'
                    return report
                report['queries'][cat+'|'+state]=audit
                if audit['status']=='UNVERIFIED_FILTER':unverified.append(cat+'|'+state)
                category_counts[cat]+=len(rows)
                for row in rows:
                    if row['id'] in category_ids[cat]:
                        raise SourceError('Duplicate source hospital across state filters: '+cat+' / '+row['id'])
                    category_ids[cat].add(row['id'])
                    if row['id'] in gathered:merge_records(gathered[row['id']],row)
                    else:gathered[row['id']]=row
        if len(report['queries'])!=len(STATES)*len(CATEGORIES):
            raise SourceError('Incomplete category/state matrix')
        # Per-state page and identity counts are verified by collect_query(),
        # including the last page. Never conflate a source error with zero.
        if unverified:
            raise SourceError('Source state/category query filters unverified: '+str(unverified[:30]))
        for cat in CATEGORIES:
            counted=category_counts[cat]
            if counted!=len(category_ids[cat]):
                raise SourceError('Duplicate network records across state filters: '+cat)
            print(f'STAR_CATEGORY_STATE_PAGES_RECONCILED {cat}={counted}',flush=True)
        report['state_partitions_reconciled']=True
        report['unverified_state_filters']=[]
        report['source_zero_queries']=[q for q,a in report['queries'].items()
                                      if a.get('status')=='SOURCE_REPORTED_ZERO']
        if category_counts['Network']<5000:raise SourceError('Network count abnormally low; validate first')
        # Star might report a legitimately empty subnetwork category. Only the
        # exact API 2012/0/[] response in ALL 36 state partitions can prove it.
        for category in CATEGORIES[1:]:
            if category_counts[category]==0 and any(
                r.get('status')!='SOURCE_REPORTED_ZERO'
                for k,r in report['queries'].items() if k.startswith(category+'|')):
                raise SourceError('Unverified zero-count category: '+category)
        # A missing source city is not an invented city. Hospital identity is
        # still reliable with source name, address, state, PIN and coordinates.
        # OTHER missing fields or unknown source flags still prevent publication.
        quality_records=[r for r in gathered.values() if r.get('source_identity_issues')]
        city_without_source=[r for r in quality_records if source_city_missing_safe(r)]
        unresolved_quality=[r for r in quality_records if not source_city_missing_safe(r)]
        report['source_identity_review_count']=len(quality_records)
        report['source_identity_review_reasons']=dict(Counter(
            issue for r in quality_records for issue in r['source_identity_issues']))
        report['source_city_missing_count']=len(city_without_source)
        # Refuse a dramatic new loss of source city coverage.
        if len(city_without_source)>max(10,math.ceil(len(gathered)*0.005)):
            unresolved_quality.extend(city_without_source)
        if quality_records:
            write_atomic(out/'star_identity_review.json',{
              'kind':'STAR_PUBLIC_SOURCE_DATA_QUALITY_REVIEW',
              'query_count':len(report['queries']),
              'publication_permitted':not bool(unresolved_quality),
              'source_record_issues':[
                {'id':r['id'],'issues':r['source_identity_issues'],
                 'categories':r['star_categories'],
                 'source_state_filter':r.get('source_filter_for_review'),
                 'public_source_row':r.get('source_review_row',{}),
                 'disposition':('SOURCE_CITY_NOT_PROVIDED' if source_city_missing_safe(r)
                                else 'UNRESOLVED_DATA_QUALITY')}
                for r in quality_records]})
        if unresolved_quality:
            report['status']='PAUSED_SOURCE_DATA_QUALITY'
            report['errors'].append({'stage':'identity_review',
                'error':f'{len(unresolved_quality)} source records have unresolved identity/flag problems'})
            print('STAR_DATA_QUALITY_REVIEW '+jdump(report['source_identity_review_reasons'])+
                  '; unresolved records retained in audit; NO PUBLISH',flush=True)
            return report
        # Retain source-blank city in published rows. The review artifact retains
        # the full raw source row; the public dataset exposes only the missing
        # city provenance, not duplicate raw contact details.
        for r in city_without_source:
            r['source_city_missing']=True
            r.pop('source_review_row',None)
            r.pop('source_filter_for_review',None)
        print('STAR_SOURCE_CITY_MISSING preserved='+str(len(city_without_source))+
              ' without location inference',flush=True)
        records=sorted(gathered.values(),key=lambda r:(r['state'],r['city'],r['name'],r['id']))
        eligible=[r for r in records if not r['star_flags']['excluded']]
        excluded=[r for r in records if r['star_flags']['excluded']]
        if len(eligible)<5000:raise SourceError('Eligible-source-listing count implausibly low')
        if not any(r['star_flags']['vsp'] for r in records):raise SourceError('No VSP flags found; verify source/category contract')
        m={'source_name':SOURCE,'source_url':SOURCE_URL,'captured_utc':utc(),
           'complete':True,'network_scope':'All 36 geographic state/UT queries validated; independent national total unavailable from this API',
           'state_count':len(STATES),'states_queried':STATES,'categories_queried':list(CATEGORIES),
           'query_count':len(report['queries']),'expected_query_count':len(STATES)*len(CATEGORIES),
           'raw_records_per_category':dict(category_counts),
           'state_partitions_reconciled':True,
           'source_state_filter_preflight_passed':True,
           'national_totals_not_available':'Star requires state, city or hospital filter; no unfiltered national response',
           'unverified_state_filters':[],
           'source_reported_zero_states':report['source_zero_states'],
           'source_reported_zero_queries':report['source_zero_queries'],
           'zero_result_meaning':'Star API 2012/Provider Details Not Available/count 0; no independently verified nationwide zero claim',
           'source_count':len(eligible),'excluded_count':len(excluded),'total_unique_across_categories':len(records),
           'source_city_missing_count':len(city_without_source),
           'source_city_missing_eligible_count':sum(r.get('source_city_missing') is True for r in eligible),
           'source_city_missing_excluded_count':sum(r.get('source_city_missing') is True for r in excluded),
           'source_city_missing_policy':'blank preserved; source city not inferred',
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
            'raw_records_per_category':dict(category_counts),'unique_across_categories':len(records),
            'unverified_state_filters':[],
            'source_reported_zero_states':report['source_zero_states'],
            'source_reported_zero_queries':report['source_zero_queries'],
            'state_partitions_reconciled':True,
           'source_city_missing_count':len(city_without_source)}
        return report
    except Exception as exc:
        report['status']='FAILED_CLOSED'
        reason=(type(exc).__name__+': '+str(exc))[:800]
        print('STAR_BOOTSTRAP_STOPPED reason='+reason,file=sys.stderr,flush=True)
        report['errors'].append({'stage':'bootstrap_or_validation','error':reason})
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
