#!/usr/bin/env python3
"""ICICI Lombard first-party public form: fresh, state-sharded acquisition.
Never report zero for an unverified empty result, and never infer cashless entitlement.
Collect all states/cities in one run; publication is a separate fail-closed stage.
"""
import argparse,hashlib,json,re,sys,time
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))
from icici_live_collector import (URL,ICICISession,select_options,get_rows_from_html,header_key,norm)
from icici_nationwide_collector import check_records

def utc():return datetime.now(timezone.utc).isoformat()
def atomic(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf8');tmp.replace(path)
def sl(x):return re.sub(r'[^a-z0-9]+','-',x.casefold()).strip('-')[:90]

def run_shard(shard,total,out,session=None):
    if not 0<=shard<total:raise ValueError('Invalid shard index')
    cli=session or ICICISession(pause=1.5,timeout=45)
    html=cli.send('list_states');states=select_options(html,'State')
    if not 30<=len(states)<=40:raise RuntimeError('ICICI state selector unexpectedly incomplete')
    keys=[(header_key(x['label']),x) for x in states]
    if len({x[0] for x in keys})!=len(keys):raise RuntimeError('Duplicate state selector values')
    selected=sorted(states,key=lambda x:x['label'].casefold())[shard::total]
    result={'source':URL,'shard':shard,'shards':total,'all_states_seen':len(states),
      'states_assigned':[x['label'] for x in selected],'states':[], 'started':utc(), 'complete':False}
    for st in selected:
        info={'state':st['label'],'cities':[],'complete':False}
        result['states'].append(info)
        try:
            chosen,cities=cli.prepare_state(st['label'])
            if len(cities)<1 or len(cities)>2000:raise ValueError('City option count invalid')
            ck=[header_key(x['label']) for x in cities]
            if len(ck)!=len(set(ck)):
                raise RuntimeError('Duplicate city label; cannot prove which to select')
            info['city_count']=len(cities)
            for city in cities:
                cityinfo={'label':city['label'],'captured_utc':utc(),'count':0,'status':'PENDING','hospitals':[]}
                info['cities'].append(cityinfo)
                try:
                    response=cli.search(chosen['value'],city['value'])
                    records,signature=get_rows_from_html(response,st['label'],city['label'])
                    verdict,reason=check_records(records,st['label'],city['label'])
                    if verdict!='CAPTURED':raise RuntimeError(verdict+': '+reason)
                    cityinfo.update({'status':'CAPTURED','count':len(records),'hospitals':records,
                                     'table_evidence':signature})
                except Exception as e:
                    cityinfo.update({'status':'FAILED','error':type(e).__name__+': '+str(e)[:220]})
                print('ICICI_CITY '+st['label']+' '+city['label']+' '+cityinfo['status']+' '+str(cityinfo['count']),flush=True)
                atomic(out,result)
            info['complete']=all(c['status']=='CAPTURED' for c in info['cities']) and len(info['cities'])==len(cities)
        except Exception as e:
            info['error']=type(e).__name__+': '+str(e)[:250]
        atomic(out,result)
    result['finished']=utc()
    result['complete']=len(result['states'])==len(selected) and all(s['complete'] for s in result['states'])
    atomic(out,result)
    print('ICICI_SHARD_COMPLETE '+str(result['complete'])+' index='+str(shard),flush=True)
    return 0 if result['complete'] else 2

def publish(files,previous,out):
    fragments=[json.loads(f.read_text(encoding='utf8')) for f in sorted(files)]
    if len(fragments)!=6 or {x.get('shard') for x in fragments}!=set(range(6)):
        raise ValueError('Six complete ICICI source shards required')
    if any(not x.get('complete') or x.get('all_states_seen')!=fragments[0]['all_states_seen'] for x in fragments):
        raise ValueError('Source state/city scan incomplete')
    states=[st for frag in fragments for st in frag['states']]
    all_assigned=[s for frag in fragments for s in frag['states_assigned']]
    if len(all_assigned)!=len(set(all_assigned)) or len(all_assigned)!=fragments[0]['all_states_seen']:
        raise ValueError('Missing or overlapping ICICI states')
    count_cities=sum(len(s['cities']) for s in states)
    if count_cities<850:raise ValueError('Too few cities: '+str(count_cities))
    rows=[];references=set();id_identity={};duplicates=[]
    for st in states:
        if not st['complete']:raise ValueError('Incomplete state '+st['state'])
        for c in st['cities']:
            if c['status']!='CAPTURED' or len(c['hospitals'])!=c['count']:
                raise ValueError('Incomplete city '+c['label'])
            for r in c['hospitals']:
                key=norm(r.get('hospital_id'))
                if not key:raise ValueError('Hospital source reference missing')
                # A hospital may appear in multiple city searches. Accept only exact same reference+identity.
                mapped={'id':'icici:'+key,'reference_id':key,'name':norm(r.get('hospital_name')),
                    'address':norm(r.get('address')),'area':norm(r.get('area')),
                    'city':norm(r.get('city')),'state':norm(r.get('state')),
                    'pin':norm(r.get('pincode')),'phone':norm(r.get('contact_no')),
                    'ins':'ICICI Lombard','ins_count':1,'source':'ICICI Lombard',
                    'observed_utc':c['captured_utc'], 'hospital_type':norm(r.get('hospital_type')),
                    'deposit_waiver':norm(r.get('deposit_waiver'))}
                ident=(mapped['id'],mapped['name'],mapped['address'],mapped['city'],mapped['state'])
                prior=id_identity.get(mapped['id'])
                if prior is not None and prior!=ident:
                    raise ValueError('Same ICICI insurer reference maps to conflicting hospital identities: '+mapped['id'])
                id_identity[mapped['id']]=ident
                if ident in references:duplicates.append(key);continue
                references.add(ident);rows.append(mapped)
    old=json.loads(previous.read_text(encoding='utf8'))
    n=len(rows);before=len(old.get('hospitals',[]))
    if n<11000 or (before>=11000 and not .85*before<=n<=1.15*before):
        raise ValueError(f'Suspicious ICICI hospital total {before}->{n}')
    rows.sort(key=lambda x:(x['state'],x['city'],x['name'],x['id']))
    result={'metadata':{'source':'ICICI Lombard General Insurance','source_url':URL,
        'scope':'nationwide_search_baseline','record_count':n,'cities_captured':count_cities,
        'state_labels':len(states),'observed_from_utc':min(x['started'] for x in fragments),
        'observed_through_utc':max(x['finished'] for x in fragments),
        'verification_level':'OFFICIAL_SEARCH_RESULT_ONLY','refresh_mode':'SCHEDULED_FULL_CITY_SEARCH',
        'duplicate_search_result_refs':len(duplicates),'data_warning':'Insurer public locator search results; coverage and policy-specific eligibility require insurer verification.'},'hospitals':rows}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    print('ICICI_REFRESH_VERIFIED records='+str(n)+' city_queries='+str(count_cities),flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--shard-index',type=int);p.add_argument('--shard-count',type=int,default=6)
    p.add_argument('--out',type=Path);p.add_argument('--publish',action='store_true')
    p.add_argument('--input',type=Path);p.add_argument('--previous',type=Path);a=p.parse_args()
    if a.publish:
        publish(list(a.input.rglob('icici-shard-*.json')),a.previous,a.out)
    else:
        if a.shard_index is None:raise ValueError('Missing shard index')
        return run_shard(a.shard_index,a.shard_count,a.out)
    return 0
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:print('ICICI_NO_PUBLISH '+type(e).__name__+': '+str(e),file=sys.stderr);sys.exit(2)
