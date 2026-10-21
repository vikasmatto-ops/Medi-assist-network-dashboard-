#!/usr/bin/env python3
"""Fail-closed Star Health source snapshot gate. No writes until validation passes."""
import argparse
from collections import Counter
import json
import re
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
    source_city_missing=0
    for r in rows+ex_rows:
        if not expected.issubset(r) or not all(str(r[k]).strip() for k in ('id','name','address','state','pin')):
            raise ValueError('Hospital identity incomplete')
        if not re.fullmatch(r'\d{6}',str(r['pin'])):
            raise ValueError('Hospital PIN invalid')
        if not r['city']:
            if (r.get('source_city_missing') is not True or
                r.get('source_identity_issues')!=['missing_cityName']):
                raise ValueError('City blank without exact source provenance')
            try:
                lat=float(r.get('latitude') or '')
                lon=float(r.get('longitude') or '')
            except (ValueError,TypeError):
                raise ValueError('City blank without source coordinates')
            if not (6.0 <= lat <= 38.5 and 68.0 <= lon <= 98.5):
                raise ValueError('City blank with implausible source coordinates')
            source_city_missing+=1
        elif r.get('source_city_missing') or r.get('source_identity_issues'):
            raise ValueError('Unexpected source identity quality flag')
        if 'source_review_row' in r or 'source_filter_for_review' in r:
            raise ValueError('Raw review-only fields included in published listing')
    if (type(meta.get('source_city_missing_count')) is not int or
        meta['source_city_missing_count']!=source_city_missing or
        meta.get('source_city_missing_policy')!='blank preserved; source city not inferred' or
        ex_meta.get('source_city_missing_count')!=source_city_missing or
        meta.get('source_city_missing_eligible_count')!=sum(not r.get('city') for r in rows) or
        meta.get('source_city_missing_excluded_count')!=sum(not r.get('city') for r in ex_rows) or
        ex_meta.get('source_city_missing_eligible_count')!=meta.get('source_city_missing_eligible_count') or
        ex_meta.get('source_city_missing_excluded_count')!=meta.get('source_city_missing_excluded_count') or
        report.get('source_city_missing_count')!=source_city_missing or
        (report.get('summary') or {}).get('source_city_missing_count')!=source_city_missing):
        raise ValueError('Source-missing city provenance totals do not reconcile')
    if source_city_missing>max(10,__import__('math').ceil((len(rows)+len(ex_rows))*.005)):
        raise ValueError('Unexpected spike in missing source cities')
    for r in rows+ex_rows:
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
    preflight=report.get('state_filter_preflight') or {}
    if preflight.get('expected_states')!=36 or preflight.get('accepted_states')!=36 or preflight.get('unresolved'):
        raise ValueError('Star state filters have unresolved territories')
    if set(preflight.get('accepted',{}))!=set(meta.get('states_queried',[])):
        raise ValueError('Star preflight geographic coverage differs from captured states')
    if meta.get('state_partitions_reconciled') is not True or report.get('state_partitions_reconciled') is not True:
        raise ValueError('State-partition pagination reconciliation missing')
    if meta.get('source_state_filter_preflight_passed') is not True or meta.get('unverified_state_filters'):
        raise ValueError('Incomplete Star source-filter coverage')
    if any(a.get('status') not in ('COMPLETE','SOURCE_REPORTED_ZERO') for a in audit_queries.values()):
        raise ValueError('Unverified Star source state/category filter in query audit')
    for name,a in audit_queries.items():
        if a.get('status')=='SOURCE_REPORTED_ZERO' and not (
            a['count']==0 and a['captured']==0 and a['distinct']==0 and a['pages']==0):
            raise ValueError('A source-reported-zero partition has positive records/pages: '+name)
    zero_queries=sorted(name for name,a in audit_queries.items() if a.get('status')=='SOURCE_REPORTED_ZERO')
    if sorted(meta.get('source_reported_zero_queries',[])) != zero_queries or sorted(report.get('source_zero_queries',[]))!=zero_queries:
        raise ValueError('Source zero-status query provenance missing or inconsistent')
    zero_states=sorted(st for st,item in preflight['accepted'].items() if item.get('status')=='SOURCE_REPORTED_ZERO')
    if sorted(meta.get('source_reported_zero_states',[]))!=zero_states or sorted(report.get('source_zero_states',[]))!=zero_states:
        raise ValueError('Source zero-status preflight provenance missing or inconsistent')
    if meta.get('national_totals_not_available') != 'Star requires state, city or hospital filter; no unfiltered national response':
        raise ValueError('Source completeness limitation not declared')
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
