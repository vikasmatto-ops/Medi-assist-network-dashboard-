#!/usr/bin/env python3
"""Medi Assist nationwide API snapshot builder. stdlib only, never edits a live website.
Run offline: python scripts/refresh_network.py --snapshot fullnetwork.json
Run live:    python scripts/refresh_network.py --live
Data source returns current API SNAPSHOT, not guaranteed empanelment verification.
"""
import argparse
import csv
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import sys
import urllib.request
from collections import Counter, defaultdict

SOURCE_URL = 'https://api-eligibility.mediassist.in/network/hospitals'



def plain(v):
    return re.sub(r'\s+', ' ', html.unescape(str(v or ''))).strip()


def key(v):
    return plain(v).casefold()





def clean_one(h,city,rule):
    insurers = h.get('associatedInsuranceCompanies') or []
    if not isinstance(insurers,list):insurers=[]
    unique = {}
    ppn_names = set()
    full_names = {}
    for i in insurers:
        if not isinstance(i,dict):continue
        short = plain(i.get('shortName')) or plain(i.get('fullName'))
        if not short:continue
        unique.setdefault(short.casefold(), short)
        full_names[short] = plain(i.get('fullName'))
        if i.get('isPPN') is True:ppn_names.add(short)
    names = sorted(unique.values(),key=str.casefold)
    spec = h.get('hospSpecialities') or []
    if isinstance(spec,str):spec=[spec]
    spec = sorted(set(plain(s) for s in spec if plain(s) and key(s) not in ('null','not available')),key=str.casefold)
    try:rating=float(h.get('avgRating') or 0)
    except (ValueError,TypeError):rating=0.0
    try:id_value=int(h['id'])
    except (ValueError,TypeError,KeyError):id_value=None
    return {
        'id':id_value,'name':plain(h.get('name')),'address':plain(h.get('address')),
        'city':city,'pin':plain(h.get('pinCode')),'phone':plain(h.get('phone')),
        'ppn':h.get('isPPN') is True,'ppn_insurers':sorted(ppn_names,key=str.casefold),
        'rating':rating,'ins':', '.join(names),'ins_count':len(names),
        'spec':', '.join(spec),'source_city':plain(h.get('city')),
        'source_district':plain(h.get('district')),'match_rule':rule,
        'blacklisted':h.get('blacklisted') is True,
        'entity_id':h.get('entityId')
    }


def load_snapshot(fp):
    obj=json.loads(Path(fp).read_text(encoding='utf-8-sig'))
    return obj


def fetch_api():
    req=urllib.request.Request(SOURCE_URL,data=b'{}',method='POST',headers={
        'Content-Type':'application/json','accessToken':'',
        'Origin':'https://mediassisttpa.in','Referer':'https://mediassisttpa.in/',
        'User-Agent':'Mozilla/5.0 (compatible; network-dashboard-refresh/1.0)'})
    with urllib.request.urlopen(req,timeout=240) as res:
        if res.status not in (200,201):raise RuntimeError('Unexpected HTTP status: '+str(res.status))
        obj=json.load(res)
    return obj


def build(source, fetched_at):
    """Build full-country data from the entire live Medi Assist API snapshot.

    Only source-blacklisted or invalid-identity rows are excluded. Geography does
    not restrict a record. Cashless approval remains policy/TPA-specific.
    """
    hospitals = source.get('hospitals')
    if source.get('isSuccess') is False or not isinstance(hospitals, list) or len(hospitals) < 25000:
        raise RuntimeError('Source API count/shape failed completeness checks; refusing to publish')
    ids = [h.get('id') for h in hospitals if isinstance(h, dict)]
    if len(ids) != len(hospitals) or len(set(map(str, ids))) != len(ids):
        raise RuntimeError('Missing/duplicate source hospital IDs; refusing to publish')
    chosen, flagged = [], []
    excluded = Counter()
    for h in hospitals:
        city = plain(h.get('city'))
        state = plain(h.get('state'))
        # Do not incorrectly assign facilities to another district/city.
        if not city or not state:
            excluded['missing_source_city_or_state'] += 1
            flagged.append({'id':h.get('id'), 'name':plain(h.get('name')),
                            'state':state, 'city':city,
                            'reason':'missing_source_city_or_state'})
            continue
        x = clean_one(h, city, 'source_api_city')
        x['state'] = state
        x['district'] = plain(h.get('district'))
        if not x['name'] or x['id'] is None or not x['address']:
            excluded['missing_identifying_field'] += 1
            flagged.append({'id':x['id'],'name':x['name'],'state':state,
                            'city':city,'reason':'missing_identifying_field'})
            continue
        if x['blacklisted']:
            excluded['blacklisted_in_source'] += 1
            flagged.append({'id':x['id'],'name':x['name'],'state':state,
                            'city':city,'reason':'blacklisted_in_source',
                            'effective_date':None,
                            'note':'Medi Assist source flag only; no blacklist effective date supplied'})
            continue
        chosen.append(x)
    chosen.sort(key=lambda x:(x['state'].casefold(),x['city'].casefold(),x['name'].casefold(),x['id']))
    if len(chosen) != len(set(x['id'] for x in chosen)):
        raise RuntimeError('Duplicate hospital ID after filtering')
    counts = Counter(x['city'] for x in chosen)
    states = Counter(x['state'] for x in chosen)
    if len(chosen) < 20000 or len(states) < 15:
        raise RuntimeError('Implausibly small nationwide result; refusing to publish')
    metadata = {
        'snapshot_retrieved_at_utc':fetched_at,
        'source':SOURCE_URL,
        'scope':'nationwide',
        'mapping_version':'nationwide-source-city-v1',
        'network_source_count':len(hospitals),
        'selected_city_count':len(chosen),
        'published_hospital_count':len(chosen),
        'state_count':len(states),
        'state_counts':dict(sorted(states.items())),
        'city_counts':dict(sorted(counts.items())),
        'blacklisted_and_invalid_records_excluded':True,
        'data_warning':'API appearance is not insurer/policy cashless confirmation. Verify with insurer/TPA before admission.'
    }
    audit = {
        'metadata':metadata,
        'excluded_reasons':dict(excluded),
        'flagged_records':flagged,
        'selected_hospital_ppn_count':sum(x['ppn'] for x in chosen),
        'selected_insurer_ppn_hospital_count':sum(bool(x['ppn_insurers']) for x in chosen),
        'hospital_ppn_false_but_insurer_ppn_true':sum(not x['ppn'] and bool(x['ppn_insurers']) for x in chosen),
        'missing_speciality_count':sum(not x['spec'] for x in chosen),
        'missing_phone_count':sum(not x['phone'] for x in chosen),
        'insurer_missing_count':sum(x['ins_count']==0 for x in chosen),
    }
    return {'metadata':metadata,'hospitals':chosen}, audit


def safety_gate(new_data, existing_path):
    """Allow a deliberate 11-city -> nationwide migration, never a silent bad feed.

    Across migration compare SOURCE totals (like for like), not selected UI rows.
    Subsequent nationwide snapshots have separate source and published-count gates.
    """
    if not existing_path.exists():
        return
    old = json.loads(existing_path.read_text(encoding='utf-8-sig'))
    old_meta = old.get('metadata', {})
    new_meta = new_data['metadata']
    before_source = old_meta.get('network_source_count')
    after_source = new_meta['network_source_count']
    if before_source and not (0.85 * before_source <= after_source <= 1.15 * before_source):
        raise RuntimeError(f'Source feed count changed unexpectedly: {before_source}->{after_source}; manual review')
    if old_meta.get('scope') == 'nationwide':
        old_count = len(old.get('hospitals', []))
        new_count = len(new_data['hospitals'])
        if old_count and not (0.85 * old_count <= new_count <= 1.15 * old_count):
            raise RuntimeError(f'Nationwide published count changed unexpectedly: {old_count}->{new_count}; manual review')


def main():
    pa=argparse.ArgumentParser()
    mode=pa.add_mutually_exclusive_group(required=True)
    mode.add_argument('--snapshot',help='Full API JSON snapshot path (offline; no API call)')
    mode.add_argument('--live',action='store_true',help='Single official API POST request')
    pa.add_argument('--output',default='data/hospitals.json')
    pa.add_argument('--audit',default='audit/refresh_audit.json')
    pa.add_argument('--review-csv',default='audit/selected_hospitals.csv')
    pa.add_argument('--ignore-change-gate',action='store_true',help='USE ONLY FOR INITIAL REVIEW / MANUALLY APPROVED CHANGES')
    args=pa.parse_args()
    source = fetch_api() if args.live else load_snapshot(args.snapshot)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if args.live else '2026-10-12T08:49:10Z'
    records,audit=build(source,ts)
    dest=Path(args.output)
    if not args.ignore_change_gate:safety_gate(records,dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(records,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    p=Path(args.audit);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    p=Path(args.review_csv);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['id','name','address','city','pin','phone','ppn','ppn_insurers','rating','ins','ins_count','spec','source_city','source_district','match_rule'])
        writer.writeheader()
        for h in records['hospitals']:
            writer.writerow({k:', '.join(h[k]) if k=='ppn_insurers' else h[k] for k in writer.fieldnames})
    print('Full API records:',records['metadata']['network_source_count'])
    print('Nationwide hospitals published:',records['metadata']['published_hospital_count'])
    for k,v in records['metadata']['city_counts'].items():print('  %-13s %5d'%(k,v))
    print('Excluded blacklisted/quality records listed in:',args.audit)
    print('STAGING ONLY: generated data. No GitHub publication performed.')

if __name__=='__main__':main()
