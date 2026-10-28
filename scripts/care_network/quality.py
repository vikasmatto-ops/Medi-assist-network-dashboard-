"""Strict validation and quarantining for a public Care Health hospital locator.

Source pagination completeness is separate from state attribution correctness.
No guessed/relabeled hospitals, and no inference of policy-specific eligibility.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from math import ceil
import re

FIELDS = (
    'id','hospital_code','hospital_name','state','city','pincode','city_tier',
    'address_line_1','address_line_2','phone','latitude','logitude','distance',
    'hospital_category','locator_type','services','no_of_active_beds',
    'no_of_doctors','nabh','nabh_score'
)

STATES = (
    'Andaman and Nicobar Islands','Andhra Pradesh','Arunachal Pradesh',
    'Assam','Bihar','Chandigarh','Chhattisgarh',
    'Dadra and Nagar Haveli and Daman and Diu','Delhi','Goa','Gujarat',
    'Haryana','Himachal Pradesh','Jammu and Kashmir','Jharkhand','Karnataka',
    'Kerala','Ladakh','Lakshadweep','Madhya Pradesh','Maharashtra','Manipur',
    'Meghalaya','Mizoram','Nagaland','Odisha','Puducherry','Punjab',
    'Rajasthan','Sikkim','Tamil Nadu','Telangana','Tripura','Uttar Pradesh',
    'Uttarakhand','West Bengal'
)
ALIASES = {
    'delhi': ('nct of delhi','new delhi','delhi ncr'),
    'odisha': ('orissa',), 'puducherry': ('pondicherry',),
    'uttarakhand': ('uttaranchal',),
    'andaman and nicobar islands': ('andaman & nicobar islands','andaman and nicobar'),
    'jammu and kashmir': ('jammu & kashmir',),
    'dadra and nagar haveli and daman and diu': ('dadra & nagar haveli and daman & diu',
                                                'dadra and nagar haveli', 'daman and diu'),
}

class ValidationError(ValueError): pass

def norm(value):
    return re.sub(r'\s+',' ',str(value or '').strip()).casefold().replace('&','and')

def slug(value):
    return re.sub(r'[^a-z0-9]+','-',str(value).casefold()).strip('-')

def state_matches(raw, requested):
    val=norm(raw); key=norm(requested)
    return bool(val) and (val==key or val in {norm(v) for v in ALIASES.get(key,())})

def decode_page(payload):
    if not isinstance(payload,dict) or payload.get('status') is not True or payload.get('statusCode') != 200:
        raise ValidationError('Care API response is not a success')
    resp=payload.get('response')
    if not isinstance(resp,dict) or not isinstance(resp.get('data'),list) or not isinstance(resp.get('pagination'),dict):
        raise ValidationError('Missing hospital rows or pagination')
    p=resp['pagination']
    try: page,pages,total,limit=tuple(int(p[k]) for k in ('currentPage','totalPages','totalRecords','limit'))
    except (KeyError,TypeError,ValueError) as exc: raise ValidationError('Invalid pagination values') from exc
    if page<1 or pages<1 or total<1 or limit<1 or pages!=ceil(total/limit) or page>pages:
        raise ValidationError('Inconsistent pagination values')
    rows=resp['data']
    expected=limit if page<pages else total-(pages-1)*limit
    if len(rows)!=expected: raise ValidationError(f'Page {page} returned {len(rows)} rows, expected {expected}')
    out=[]
    for r in rows:
        if not isinstance(r,dict) or r.get('id') is None or not str(r.get('hospital_name') or '').strip():
            raise ValidationError(f'Page {page}: missing hospital ID/name')
        out.append({k:r[k] for k in FIELDS if k in r})
    return {'page':page,'pages':pages,'total':total,'limit':limit,'rows':out}

def reconcile(pages,requested,errors=()):
    now=datetime.now(timezone.utc).isoformat()
    issues=list(errors); all_rows=[]
    first=next(iter(pages.values()),None)
    total=first['total'] if first else 0
    expected=first['pages'] if first else 0
    limit=first['limit'] if first else 0
    missing=sorted(set(range(1,expected+1))-set(pages))
    if not first: issues.append('No valid source page captured')
    if missing: issues.append('Missing source pages: '+','.join(map(str,missing[:30])))
    for n in sorted(pages):
        p=pages[n]
        if (p['total'],p['pages'],p['limit'])!=(total,expected,limit):
            issues.append(f'Conflicting pagination on page {n}')
        all_rows.extend(p['rows'])
    if len(all_rows)!=total: issues.append(f'Source row count {len(all_rows)} != {total}')
    cnt=Counter(str(r['id']) for r in all_rows)
    dup=[k for k,n in cnt.items() if n>1]
    if dup: issues.append('Duplicate source IDs: '+','.join(dup[:10]))
    good=[r for r in all_rows if state_matches(r.get('state'),requested)]
    quarantine=[{'reason':'SOURCE_STATE_MISMATCH','requested_state':requested, 'hospital':r}
                for r in all_rows if not state_matches(r.get('state'),requested)]
    # A full source capture with a mislabeled row is source-complete but NOT
    # automatically publishable; humans must review the quarantined source row.
    complete=bool(first) and not issues
    publishable=complete and not quarantine and len(good)==total
    return {
      'metadata': {
        'source':'Care Health official public hospital locator',
        'api_endpoint':'https://ser.careinsurance.com/api/networkHospitalDetails',
        'collection_method':'Observation of website-generated Chrome network responses',
        'queried_state':requested,'generated_at_utc':now,
        'source_pages_complete':complete,
        'publishable':publishable,
        'expected_source_records':total,
        'source_records_observed':len(all_rows),
        'matching_state_records':len(good),
        'off_state_records':len(quarantine),
        'pages_expected':expected,'pages_captured':sorted(pages),
        'missing_pages':missing,'errors':issues[:25],
        'warning':'Directory listing is NOT policy-level confirmation of cashless eligibility.'
      },'hospitals':good,'quarantined_hospitals':quarantine
    }
