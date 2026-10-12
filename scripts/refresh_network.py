#!/usr/bin/env python3
"""Medi Assist 11-city snapshot builder. stdlib only, never edits a live website.
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
CITIES = ['Delhi', 'Faridabad', 'Gurgaon', 'Noida', 'Ghaziabad', 'Mumbai',
          'Navi Mumbai', 'Pune', 'Hyderabad', 'Bangalore', 'Ahmedabad']
OLD_CITY_COUNTS = dict(zip(CITIES,[809,235,311,216,207,756,1049,1355,1176,936,1320]))
NAVI_PINS = {'400614','400701','400702','400703','400704','400705','400706','400707',
             '400708','400709','400710','400711','410206','410208','410209','410210',
             '410218','410221'}
NAVI_TERMS = ('navi mumbai','vashi','nerul','sanpada','cbd belapur','belapur',
              'kharghar','ghansoli','kopar khairane','koparkhairane','airoli',
              'turbhe','rabale','panvel','kamothe','kalamboli','taloja',
              'ulwe','seawoods','juinagar')


def plain(v):
    return re.sub(r'\s+', ' ', html.unescape(str(v or ''))).strip()


def key(v):
    return plain(v).casefold()


def assign_city(h):
    """Conservative operational/metro geography: never equate entire Thane with Navi Mumbai.
    Returns dashboard city and a rule string, or None and a rejection reason.
    """
    st, ci, dist = map(key,(h.get('state'),h.get('city'),h.get('district')))
    pin = plain(h.get('pinCode'))
    addr = key(h.get('address'))
    if not re.fullmatch(r'\d{6}',pin):return None,'invalid_pin'
    if st == 'delhi' and pin.startswith('110'):
        return 'Delhi','delhi_state_110_pin'
    if st == 'haryana' and pin.startswith('121') and ci!='palwal' and (ci in ('faridabad','ballabhgarh','ballabgarh') or dist=='faridabad'):
        return 'Faridabad','faridabad_city_district_pin'
    if st == 'haryana' and pin.startswith('122') and (ci in ('gurugram','gurgaon') or dist in ('gurugram','gurgaon')):
        return 'Gurgaon','gurgaon_city_district_pin'
    if st == 'uttar pradesh' and pin.startswith('201') and (ci in ('noida','gautam buddha nagar') or dist in ('gautam buddha nagar','noida')):
        if 'greater noida' in addr or 'greater noida' in ci or 'pari chowk' in addr or pin in ('201308','201310','201306','201312'):
            return None,'outside_noida_greater_noida'
        if ci=='noida' or 'noida' in addr or pin in ('201301','201303','201304','201305','201307','201309'):
            return 'Noida','noida_city_locality_pin'
    if st == 'uttar pradesh' and pin.startswith('201') and (ci=='ghaziabad' or dist=='ghaziabad'):
        return 'Ghaziabad','ghaziabad_city_district_pin'
    if st == 'maharashtra':
        # Some upstream Nagpur/Kolhapur hospital records have clearly wrong Navi Mumbai pincodes.
        if ci in ('nagpur','kolhapur') and ci not in addr:
            pass # An obviously contradictory source city will be reviewed below.
        if ((pin in NAVI_PINS or ((any(k in (ci+' '+addr) for k in NAVI_TERMS)) and (pin.startswith('400') or pin.startswith('410'))))
            and dist in ('raigad','raigarh','thane','mumbai','navi mumbai','kalamboli','vashi','nerul','kalyan (e)')):
            if ci in ('nagpur','kolhapur') or ('central avenue' in addr and ci=='nagpur'):
                return None,'conflicting_city_and_pin'
            return 'Navi Mumbai','navi_mumbai_locality_pin'
        if pin.startswith('400') and (ci=='mumbai' or dist=='mumbai'):
            return 'Mumbai','mumbai_city_district_pin'
        pune_localities = ('chinchwadgaon','pimpri','chinchwad','pimpri-chinchwad','pimpri chinchwad','nigdi','akurdi','bhosari','wakad','hadapsar','hinjewadi')
        if ((ci=='pune' or (dist=='pune' and pin.startswith('411')) or ci in pune_localities)
            and (pin.startswith('411') or pin.startswith('412') or pin.startswith('410'))):
            return 'Pune','pune_metro_city_pin'
    if st=='telangana' and (pin.startswith('500') or (ci in ('hyderabad','secunderabad') and pin.startswith('501'))):
        if ci in ('khammam','mancherial','karimnagar','kamareddy'):
            return None,'conflicting_hyderabad_city'
        return 'Hyderabad','hyderabad_500_pin_metro'
    if st=='karnataka' and (pin.startswith('560') or (ci in ('bengaluru','bangalore') and pin.startswith('562'))):
        if ci in ('mysore','hubli'):
            return None,'conflicting_bangalore_city'
        return 'Bangalore','bangalore_560_pin_metro'
    if st=='gujarat' and ci!='dahegam' and (ci=='ahmedabad' or (dist=='ahmedabad' and (pin.startswith('380') or pin.startswith('382')))):
        return 'Ahmedabad','ahmedabad_city_district_pin'
    return None,'not_in_selected_city'


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
    hospitals=source.get('hospitals')
    if source.get('isSuccess') is False or not isinstance(hospitals,list) or len(hospitals)<25000:
        raise RuntimeError('Source API count/shape failed completeness checks; refusing to publish')
    original_ids = [r.get('id') for r in hospitals if isinstance(r,dict)]
    if len(set(map(str,original_ids))) != len(original_ids):
        raise RuntimeError('Duplicate source hospital IDs: refusing to publish')
    chosen=[]; excluded=Counter(); city_reasons=Counter(); flagged=[]
    for h in hospitals:
        if not isinstance(h,dict):continue
        city,reason=assign_city(h)
        if not city:
            excluded[reason]+=1
            if reason.startswith(('outside_noida','conflicting_')):
                flagged.append({'id':h.get('id'),'name':plain(h.get('name')),'source_city':plain(h.get('city')),'pin':plain(h.get('pinCode')),'reason':reason})
            continue
        x=clean_one(h,city,reason)
        if not x['name'] or x['id'] is None or not x['address']:
            flagged.append({'id':x['id'],'name':x['name'],'source_city':x['source_city'],'pin':x['pin'],'reason':'missing_identifying_field'})
            continue
        if x['blacklisted']:
            flagged.append({'id':x['id'],'name':x['name'],'source_city':x['source_city'],'pin':x['pin'],'reason':'blacklisted_in_source'})
            continue
        chosen.append(x);city_reasons[(city,reason)]+=1
    chosen.sort(key=lambda x:(CITIES.index(x['city']),x['name'].casefold(),x['id']))
    if len(chosen)!=len(set(x['id'] for x in chosen)):
        raise RuntimeError('Duplicate ID in selected network: refusing to publish')
    counts=Counter(x['city'] for x in chosen)
    if any(counts[c]<50 for c in CITIES):
        raise RuntimeError('Implausibly low city result: '+str(counts))
    metadata={'snapshot_retrieved_at_utc':fetched_at,'source':SOURCE_URL,
              'data_warning':'API retrieval time is not insurer-level empanelment verification. Confirm cashless eligibility with insurer/TPA before admission.',
              'network_source_count':len(hospitals),'selected_city_count':len(chosen),
              'city_counts':{c:counts[c] for c in CITIES},
              'mapping_version':'geographic-v1.1','blacklisted_and_invalid_records_excluded':True,
              'city_definitions_note':'Delhi state 110 PIN; Noida excludes Greater Noida; Navi Mumbai includes adjoining Panvel-Kharghar localities, excludes Thane proper; Hyderabad includes 500 PIN metro; Pune includes PCMC.'}
    audit={'metadata':metadata,'old_june_counts':OLD_CITY_COUNTS,
           'counts_delta_from_june':{c:counts[c]-OLD_CITY_COUNTS[c] for c in CITIES},
           'excluded_reasons':dict(excluded),
           'flagged_records':flagged,
           'selected_hospital_ppn_count':sum(x['ppn'] for x in chosen),
           'selected_insurer_ppn_hospital_count':sum(bool(x['ppn_insurers']) for x in chosen),
           'hospital_ppn_false_but_insurer_ppn_true':sum(not x['ppn'] and bool(x['ppn_insurers']) for x in chosen),
           'missing_speciality_count':sum(not x['spec'] for x in chosen),
           'missing_phone_count':sum(not x['phone'] for x in chosen),
           'insurer_missing_count':sum(x['ins_count']==0 for x in chosen)}
    return {'metadata':metadata,'hospitals':chosen},audit


def safety_gate(new_data,existing_path):
    if not existing_path.exists():return
    old=json.loads(existing_path.read_text(encoding='utf-8'))
    old_counts=old.get('metadata',{}).get('city_counts',{})
    new_counts=new_data['metadata']['city_counts']
    if not old_counts:return
    old_total=sum(old_counts.values());new_total=sum(new_counts.values())
    if old_total and abs(new_total-old_total)/old_total>.05:
        raise RuntimeError(f'Snapshot change {old_total}->{new_total} exceeds 5% global guard; manual review required')
    for city in CITIES:
        prev=old_counts.get(city,0);cur=new_counts.get(city,0)
        if prev and abs(cur-prev)/prev>.08:
            raise RuntimeError(f'{city} changed {prev}->{cur} (>8%); manual review required')


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
    print('Selected/approved for staged preview:',records['metadata']['selected_city_count'])
    for k,v in records['metadata']['city_counts'].items():print('  %-13s %5d'%(k,v))
    print('Excluded blacklisted/quality records listed in:',args.audit)
    print('STAGING ONLY: generated data. No GitHub publication performed.')

if __name__=='__main__':main()
