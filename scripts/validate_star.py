#!/usr/bin/env python3
"""Fail-closed Star Health source snapshot gate. No writes until validation passes."""
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile

ALL_CATEGORIES={'Network','agreed','value_network','vsp','ayush','nonpreferred'}
ALL_STATE_QUERIES=36*len(ALL_CATEGORIES)

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def validate(candidate,excluded,report,previous=None):
    meta=candidate.get('metadata') or {}
    rows=candidate.get('hospitals')
    ex_meta=excluded.get('metadata') or {}
    ex_rows=excluded.get('hospitals')
    if report.get('status')!='ALL_216_QUERIES_STAGED_VALIDATED' or report.get('complete') is not True:
        raise ValueError('Collector audit is not completely validated')
    if not isinstance(rows,list) or not isinstance(ex_rows,list):raise ValueError('Hospital arrays absent')
    if meta.get('source_name')!='Star Health' or meta.get('complete') is not True or meta.get('policy_specific_cashless_eligibility_verified') is not False:
        raise ValueError('Star source metadata invalid')
    if meta.get('source_count')!=len(rows) or meta.get('excluded_count')!=len(ex_rows):
        raise ValueError('Star eligible and excluded totals do not reconcile')
    if meta.get('query_count')!=ALL_STATE_QUERIES or meta.get('expected_query_count')!=ALL_STATE_QUERIES:
        raise ValueError('All 216 Star queries have not been confirmed')
    if set(meta.get('categories_queried',[]))!=ALL_CATEGORIES or len(set(meta.get('states_queried',[])))!=36:
        raise ValueError('Star category/state coverage incomplete')
    if meta.get('verified_page2_distinct') is not True:raise ValueError('Star page2 proof missing')
    if len(rows)<5000 or len({r['id'] for r in rows})!=len(rows):raise ValueError('Star active-source listings too small or duplicate')
    if len({r['id'] for r in ex_rows})!=len(ex_rows):raise ValueError('Duplicate excluded hospital')
    if {r['id'] for r in rows} & {r['id'] for r in ex_rows}:raise ValueError('Same hospital is included and excluded')
    if ex_meta.get('record_type')!='EXCLUDED_ONLY' or ex_meta.get('source_count')!=len(ex_rows):
        raise ValueError('Excluded source classification metadata invalid')
    expected={'id','name','address','city','state','pin','star_categories','star_flags'}
    for r in rows+ex_rows:
        if not expected.issubset(r) or not all(str(r[k]).strip() for k in ('id','name','address','city','state','pin')):
            raise ValueError('Hospital identity incomplete')
        if not isinstance(r['star_categories'],list) or not set(r['star_categories']).issubset(ALL_CATEGORIES):
            raise ValueError('Unknown classification')
        if type(r.get('ppn')) is not bool or r['ppn'] is not False:
            raise ValueError('Unexpected PPN claim')
        flags=r['star_flags']
        for k in ('excluded','ayush','vsp','pratham','smart_network','homecare'):
            if type(flags.get(k)) is not bool:raise ValueError('Invalid flag '+k)
    if any(r['star_flags']['excluded'] for r in rows):raise ValueError('Excluded hospital mixed with active-source rows')
    if any(not r['star_flags']['excluded'] for r in ex_rows):raise ValueError('Nonexcluded hospital mixed into excluded rows')
    audit_queries=report.get('queries',{})
    if len(audit_queries)!=ALL_STATE_QUERIES:raise ValueError('Incomplete per-query audit')
    for q,a in audit_queries.items():
        if not all(a.get('count')==a.get(k) for k in ('captured','distinct')):
            raise ValueError('Source page completeness failed: '+q)
        if a.get('pages',-1)<0:raise ValueError('Missing page count')
    counts=Counter({cat:sum(a['count'] for key,a in audit_queries.items() if key.startswith(cat+'|')) for cat in ALL_CATEGORIES})
    if dict(meta.get('raw_records_per_category',{}))!=dict(counts):
        raise ValueError('Per-category counts do not reconcile')
    if previous is not None:
        before=len(previous.get('hospitals',[]))
        if before>=5000 and not (.8*before<=len(rows)<=1.2*before):
            raise ValueError(f'Unexpected Star network count movement {before}->{len(rows)}, approval required')
    return {'source_listed':len(rows),'excluded':len(ex_rows),'categories':dict(counts),'queries':ALL_STATE_QUERIES}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',type=Path,default=Path('build/star'))
    ap.add_argument('--live',type=Path,default=Path('data'))
    ap.add_argument('--verify-only',action='store_true')
    args=ap.parse_args()
    c=read(args.input/'star_health_candidate.json')
    x=read(args.input/'star_excluded_candidate.json')
    r=read(args.input/'star_audit.json')
    prior=args.live/'star_health.json'
    report=validate(c,x,r,read(prior) if prior.exists() else None)
    print(json.dumps(report,indent=2))
    if args.verify_only:return
    args.live.mkdir(parents=True,exist_ok=True)
    # Never update an existing Git repo without explicit --verify-only validation above.
    for name,obj in [('star_health.json',c),('star_health_excluded.json',x)]:
        tmp=args.live/(name+'.candidate.tmp')
        tmp.write_text(json.dumps(obj,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        tmp.replace(args.live/name)
    print('Source-only publish gate passed; Star data files prepared. Git deployment remains separate.')
if __name__=='__main__':main()
