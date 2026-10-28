#!/usr/bin/env python3
"""Safely promote validated Care Health state captures into existing NetworkIQ JSON.
Unverified states retain old records and remain visibly dated; no false national completeness.
"""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
from scripts.care_network.quality import STATES,state_matches,norm
from scripts.care_network.publish import stage_reports,inspect_state

def stamp():return datetime.now(timezone.utc).isoformat()
def val(x):return str(x or '').strip()
def publicize(r):
    # Source raw fields and category codes are preserved without inferring active policy eligibility.
    x=dict(r);name=val(r.get('hospital_name'))
    addr=', '.join(y for y in [val(r.get('address_line_1')),val(r.get('address_line_2'))] if y)
    sv=r.get('services') or []
    if isinstance(sv,str):sv=[sv]
    x.update({'reference_id':val(r.get('id')),'name':name,'address':addr,
        'pin':val(r.get('pincode')),'spec':', '.join(str(z) for z in sv if str(z).strip()),
        'source':'Care Health','ins':'Care Health','ins_count':1,'rating':0,
        'ppn':False,'ppn_insurers':[]})
    if not name or not addr or not val(r.get('state')):raise ValueError('Missing Care source hospital identity')
    return x

def merge(previous,staging):
    existing=json.loads(previous.read_text(encoding='utf8'))
    if existing['metadata'].get('source_name')!='Care Health':raise ValueError('Wrong existing dataset')
    original=existing['hospitals']
    if not isinstance(original,list) or len(original)<1000:raise ValueError('Missing old hospitals')
    reports,duplicates=stage_reports(staging)
    if not reports:raise ValueError('No Care collector state outputs')
    accepted={};held={}
    for state,(path,doc) in reports.items():
        if state in duplicates:held[state]='Duplicated state reports';continue
        reason=inspect_state(doc)
        if reason:held[state]=reason;continue
        old=[r for r in original if state_matches(r.get('state'),state)]
        records=doc['hospitals']
        if old and not .95*len(old)<=len(records)<=1.15*len(old):
            held[state]=f'Unusual state count movement {len(old)} -> {len(records)}';continue
        try:converted=[publicize(r) for r in records]
        except ValueError as e:held[state]=str(e);continue
        if len({str(x['id']) for x in converted})!=len(converted):
            held[state]='Repeated Care source IDs';continue
        accepted[state]=(converted,doc['metadata']['generated_at_utc'])
    if not accepted:raise ValueError('No Care state fully validated; old data preserved')
    retained=[r for r in original if not any(state_matches(r.get('state'),s) for s in accepted)]
    merged=retained+[r for rows,_ in accepted.values() for r in rows]
    if len({str(r['id']) for r in merged})!=len(merged):
        raise ValueError('Care hospital IDs overlap between captured and old states')
    if len(merged)<.85*len(original):raise ValueError('Suspicious national Care decline')
    merged.sort(key=lambda r:(val(r.get('state')).casefold(),val(r.get('city')).casefold(),val(r.get('name')).casefold(),str(r['id'])))
    meta=dict(existing['metadata'])
    per_state=dict(meta.get('state_verified_at_utc') or {})
    per_state.update({s:date for s,(_,date) in accepted.items()})
    # Old state rows are explicitly not represented as newly source-verified.
    meta.update({'state_attributed_hospital_rows':len(merged),'unique_hospital_ids':len(merged),
       'nationwide_network_completeness_verified':False,
       'coverage_type':'PARTIAL_PAN_INDIA_FREE_TEXT_SEARCH_SNAPSHOTS',
       'state_verified_at_utc':per_state,'last_partial_refresh_utc':stamp(),
       'last_verified_states':sorted(accepted),'refresh_method_status':'AUTOMATED_STATEWISE_BROWSER_CAPTURES_PARTIAL',
       'refresh_scope_warning':'Some states retain older hospital listings; check state_verified_at_utc. This is NOT a complete nationwide live directory.',
       'policy_specific_cashless_eligibility_verified':False})
    return {'metadata':meta,'hospitals':merged},{'updated_states':sorted(accepted),'held_states':held,
        'prior_records':len(original),'new_records':len(merged),'published_scope':'PARTIAL_PER_STATE'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--staging',type=Path,required=True)
    p.add_argument('--previous',type=Path,default=Path('data/care_health.json'))
    p.add_argument('--output',type=Path,default=Path('data/care_health.json'))
    p.add_argument('--audit',type=Path,default=Path('build/care_refresh_audit.json'))
    a=p.parse_args()
    updated,report=merge(a.previous,a.staging)
    a.audit.parent.mkdir(parents=True,exist_ok=True)
    a.audit.write_text(json.dumps(report,indent=2),encoding='utf8')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(updated,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    print('CARE_STATEWISE_VERIFIED '+str(len(report['updated_states']))+' changed states; '+str(len(updated['hospitals']))+' total records',flush=True)
    for k,v in report['held_states'].items():print('CARE_STATE_HELD '+k+' '+v,flush=True)
if __name__=='__main__':
    try:main()
    except Exception as exc:print('CARE_NO_PUBLISH '+type(exc).__name__+': '+str(exc),file=sys.stderr);sys.exit(2)
