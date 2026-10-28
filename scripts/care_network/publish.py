"""Safe GitHub Pages promotion of verified Care state/UT snapshots.

Default is DRY-RUN. Bad/partial captures never erase verified source data.
"""
from __future__ import annotations
import argparse,json,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
try:
 from .quality import STATES,slug,state_matches
except ImportError:
 from quality import STATES,slug,state_matches


def atomic_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')
    tmp.replace(path)


def inspect_state(doc):
    if not isinstance(doc,dict) or 'metadata' not in doc:return 'Invalid report document'
    meta=doc['metadata'];query=meta.get('queried_state')
    if query not in STATES:return 'Report state not in canonical list'
    if not meta.get('source_pages_complete') or not meta.get('publishable'):
        return 'Collector flagged incomplete or source-anomaly data'
    rows=doc.get('hospitals')
    if not isinstance(rows,list) or len(rows)<1:return 'Empty hospital records'
    if meta.get('quarantined_hospitals') or doc.get('quarantined_hospitals'):
        return 'Quarantined cross-state hospital records require manual review'
    if meta.get('off_state_records')!=0:return 'Source includes records outside requested state'
    if meta.get('expected_source_records')!=len(rows):return 'Record count differs from source total'
    if meta.get('source_records_observed')!=len(rows):return 'Observed count differs from rows'
    ids=[str(r.get('id')) for r in rows]
    if len(set(ids))!=len(ids):return 'Duplicate hospital IDs'
    if any(not r.get('hospital_name') or not state_matches(r.get('state'),query) for r in rows):
        return 'Missing name or hospital row state mismatch'
    return None


def read_json(path,default):
    if not path.exists():return default
    return json.loads(path.read_text(encoding='utf8'))


def stage_reports(root):
    reports={};duplicates=[]
    for path in sorted(root.rglob('*.json')):
        if path.name.startswith('_') or path.name=='manifest.json':continue
        try:
            obj=json.loads(path.read_text(encoding='utf8'))
            state=obj.get('metadata',{}).get('queried_state')
            if state not in STATES:continue
            if state in reports:
                duplicates.append(state)
                # Don't arbitrarily trust one of two state reports.
                continue
            reports[state]=(path,obj)
        except (OSError,ValueError,TypeError):continue
    return reports,set(duplicates)


def plan(staging,repo_root,drop_limit=0.05):
    reports,duplicates=stage_reports(staging)
    if not reports:return {'generated_at_utc':datetime.now(timezone.utc).isoformat(),
                           'approved':[],'held':{'ALL':'No state reports found'},
                           'candidate_count':0},{}
    selected={};held={}
    pathbase=repo_root/'data'/'care_health_states'
    for state,(path,doc) in reports.items():
        if state in duplicates:
            held[state]='Multiple staging reports for same state';continue
        reason=inspect_state(doc)
        if reason:
            held[state]=reason;continue
        oldfile=pathbase/(slug(state)+'.json')
        old=read_json(oldfile,{})
        oldrows=old.get('hospitals',[])
        old_count=len(oldrows)
        new_count=len(doc['hospitals'])
        if old_count and new_count<old_count*(1-drop_limit):
            held[state]=f'Suspicious network reduction: {old_count} -> {new_count} (> {100*drop_limit:.1f}%)'
            continue
        # Preserve the original source spelling of each state and category code.
        selected[state]=doc
    # Cross-state source hospital IDs should be unique. Check both newly approved
    # captures and the existing snapshots of states not being replaced today.
    owners={}
    for state in STATES:
        if state in selected:
            records=selected[state]['hospitals']
        else:
            records=read_json(pathbase/(slug(state)+'.json'),{}).get('hospitals',[])
        for row in records:
            owners.setdefault(str(row.get('id')),set()).add(state)
    collisions={state for states in owners.values() if len(states)>1 for state in states}
    for state in list(selected):
        if state in collisions:
            held[state]='Hospital IDs overlap another state/UT snapshot; review required'
            del selected[state]
    summary={'generated_at_utc':datetime.now(timezone.utc).isoformat(),
             'candidate_count':len(reports), 'approved':sorted(selected),
             'held':dict(sorted(held.items())),
             'omitted_states':[x for x in STATES if x not in reports],
             'notes':'Only independently validated source-complete states are promoted; policy eligibility unverified.'}
    return summary,selected


def apply(summary,selected,repo_root):
    folder=repo_root/'data'/'care_health_states'
    manifestfile=repo_root/'data'/'care_health_manifest.json'
    current=read_json(manifestfile,{'source':'Care Health official public locator','states':{}})
    current.setdefault('states',{})
    stamp=summary['generated_at_utc']
    for state,doc in selected.items():
        canonical=slug(state)
        snap={'metadata':{
              'source':'Care Health official public locator',
              'state':state,'verified_at_utc':doc['metadata']['generated_at_utc'],
              'source_records':doc['metadata']['expected_source_records'],
              'category_codes_preserved':True,
              'warning':'Directory presence is not policy-specific cashless eligibility.'},
              'hospitals':doc['hospitals']}
        atomic_json(folder/(canonical+'.json'),snap)
        current['states'][state]={
            'status':'VERIFIED','records':len(doc['hospitals']),
            'last_verified_utc':doc['metadata']['generated_at_utc'],
            'last_attempt_utc':stamp,'last_attempt_status':'VERIFIED',
            'path':'data/care_health_states/'+canonical+'.json'}
    for state,reason in summary['held'].items():
        if state not in STATES:continue
        existing=current['states'].get(state,{})
        existing.update({'last_attempt_utc':stamp,'last_attempt_status':'HELD_FOR_REVIEW',
                         'last_attempt_note':reason[:180]})
        if 'last_verified_utc' not in existing:
            existing['status']='UNVERIFIED'
        current['states'][state]=existing
    current.update({'source':'Care Health official public locator',
                    'last_attempt_utc':stamp,
                    'published_states':sum(bool(v.get('last_verified_utc')) for v in current['states'].values()),
                    'total_verified_records':sum(int(v.get('records',0)) for v in current['states'].values()),
                    'all_india_complete':all(x in current['states'] and current['states'][x].get('status')=='VERIFIED' for x in STATES),
                    'policy_eligibility_verified':False})
    atomic_json(manifestfile,current)


def run(args):
    summary,selected=plan(args.staging_dir,args.repo_root,args.max_drop_percent/100)
    atomic_json(args.report,summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='omitted_states'},indent=2))
    if args.apply:
        if summary['candidate_count']<1: raise ValueError('No source reports; refused apply')
        apply(summary,selected,args.repo_root)
        print('Applied',len(selected),'verified state snapshots; held states preserved')
    else:print('DRY RUN ONLY: no repo data files modified')
    return 0

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--staging-dir',type=Path,required=True)
    ap.add_argument('--repo-root',type=Path,default=Path('.'))
    ap.add_argument('--report',type=Path,default=Path('care_promotion_report.json'))
    ap.add_argument('--max-drop-percent',type=float,default=5)
    ap.add_argument('--apply',action='store_true')
    try:sys.exit(run(ap.parse_args()))
    except (ValueError,OSError) as ex:print('PROMOTION REFUSED:',ex,file=sys.stderr);sys.exit(2)
