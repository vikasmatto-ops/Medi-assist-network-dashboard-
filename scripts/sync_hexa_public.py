"""Publish a MINIMAL, curated Hexa hospital feed from an authorized Google Sheet.

The published JSON is PUBLIC. NEVER emit comments, tariffs, ASP, POC, phone,
rooms, sheet URL, original raw empanelment strings, or arbitrary columns.
A service account must have Viewer permission and Sheets read-only scope.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, re, sys, tempfile
from collections import Counter
from pathlib import Path

TAB='Hospital network'
SCHEMA='networkiq.hexa.public.v1'
EXPECTED=['city','area','pin code','hospital name','active status','mop status']


def clean(v): return re.sub(r'\s+',' ',str(v if v is not None else '').strip())
def norm(v): return re.sub(r'[^a-z0-9]+',' ',clean(v).lower()).strip()
def city_norm(v):
    aliases={'gurugram':'Gurgaon','gurgoan':'Gurgaon','gurgaon':'Gurgaon','bengaluru':'Bangalore', 'delhi ncr':'Delhi'}
    key=norm(v)
    return aliases.get(key,clean(v).title())
def status(v):
    key=norm(v)
    if key in ('active','operational'):return 'ACTIVE'
    if key in ('inactive','in active','closed'):return 'INACTIVE'
    if key in ('on hold','onhold','hold','paused','temporarily on hold'):return 'ON_HOLD'
    return 'UNKNOWN'
def panel(v):
    s=norm(v)
    if not s:return 'UNKNOWN'
    if s in ('yes','y','empanelled','empaneled','available','active','cashless'):return 'YES'
    if s in ('no','n','not empanelled','not empaneled','not available','inactive'):return 'NO'
    if s.startswith(('yes ','yes package','conditional','subject to','only ')) or s in ('package','yes package'):return 'CONDITIONAL'
    return 'REVIEW'
def pin_norm(value):
    s=clean(value)
    m=re.fullmatch(r'(\d{6})(?:\.0+)?',s)
    return m.group(1) if m else ''

def hospital_key(name,city,pin):
    name_key=' '.join(t for t in norm(name).split() if t not in ('hospital','hospitals','pvt','ltd','private','limited','the','a','unit','of'))
    key='|'.join((name_key,norm(city),pin_norm(pin)))
    return hashlib.sha256(key.encode()).hexdigest()[:18]

def prepare(rows,previous=None, minimum_hospitals=50):
    if not rows or len(rows[0])<40:raise ValueError('Missing or truncated hospital sheet headers')
    headers=[clean(c) for c in rows[0]]
    if [norm(x) for x in headers[:6]]!=EXPECTED:raise ValueError('Master sheet columns moved; refusing publication')
    # Columns 10-58 comprise TPA and insurer matrix; NEVER read raw comments (col 7-9).
    providers={i:headers[i] for i in range(9,len(headers)) if headers[i]}
    if len(providers)<35 or not any('care health' in norm(s) for s in providers.values()):
        raise ValueError('Provider header validation failed')
    seen={}; source_row_count=0
    for row in rows[1:]:
        if len(row)<4 or not clean(row[3]):continue
        source_row_count+=1
        city=city_norm(row[0] if len(row)>0 else '')
        name=clean(row[3]);pin=pin_norm(row[2])
        key=hospital_key(name,city,pin)
        st=status(row[4] if len(row)>4 else '')
        emp={provider:panel(row[i] if len(row)>i else '') for i,provider in providers.items()}
        if key not in seen:
            seen[key]={'id':key,'name':name,'city':city,'pin':pin,'status':st,'empanelment':emp,
                       'duplicate_rows':1,'conflict':False}
            continue
        current=seen[key];current['duplicate_rows']+=1
        if current['status']!=st:
            current['status']='REVIEW';current['conflict']=True
        for provider,value in emp.items():
            if current['empanelment'][provider]!=value:
                current['empanelment'][provider]='REVIEW';current['conflict']=True
    hospitals=sorted(seen.values(),key=lambda x:(x['city'].lower(),x['name'].lower(),x['pin']))
    if len(hospitals)<minimum_hospitals:raise ValueError(f'Only {len(hospitals)} hospitals; refusing destructive refresh')
    if sum(x['status']!='UNKNOWN' for x in hospitals)<len(hospitals)*.75:
        raise ValueError('Too many missing hospital statuses; master schema may have changed')
    if previous:
        old=previous.get('hospitals',[])
        if old and len(hospitals)<int(len(old)*.90):raise ValueError('Unexpected >10% hospital reduction; old snapshot preserved')
        if previous.get('metadata',{}).get('schema')!=SCHEMA:raise ValueError('Old publication schema mismatch')
    s=Counter(h['status'] for h in hospitals)
    changes=[]
    if previous and previous.get('hospitals'):
        old_map={h['id']:h for h in previous['hospitals']}
        for h in hospitals:
            prior=old_map.get(h['id'])
            if not prior:changes.append({'name':h['name'],'city':h['city'],'change':'NEW_HOSPITAL'})
            else:
                if prior.get('status')!=h['status']:
                    changes.append({'name':h['name'],'city':h['city'],'change':'STATUS_CHANGED','to':h['status']})
                changed=sum(prior.get('empanelment',{}).get(k)!=v for k,v in h['empanelment'].items())
                if changed:changes.append({'name':h['name'],'city':h['city'],'change':'EMPANELMENT_CHANGED','provider_count':changed})
        newids={h['id'] for h in hospitals}
        for old in previous['hospitals']:
            if old['id'] not in newids:changes.append({'name':old['name'],'city':old['city'],'change':'REMOVED_FROM_MASTER'})
    metadata={'schema':SCHEMA,'source':'Hexa Hospital network master — limited public fields only',
              'rows_in_master':source_row_count,'record_count':len(hospitals),
              'duplicates':sum(h['duplicate_rows']>1 for h in hospitals),
              'conflicting_duplicate_groups':sum(bool(h['conflict']) for h in hospitals),
              'status_counts':dict(s),'provider_count':len(providers),
              'as_of_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
              'publication_scope':'hospital identity, city, PIN, operational status and normalized provider empanelment only',
              'not_official_insurer_confirmation':True}
    return {'metadata':metadata,'providers':list(providers.values()),'hospitals':hospitals,'recent_changes':changes[:100]}

def fetch_sheet(credentials, sheet_id, session_factory=None):
    if not sheet_id or not credentials:raise ValueError('Missing authorized sheet ID or service-account credentials')
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    from urllib.parse import quote
    info=json.loads(credentials)
    auth=service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    session=(session_factory or AuthorizedSession)(auth)
    url='https://sheets.googleapis.com/v4/spreadsheets/'+quote(sheet_id,safe='')+'/values/'+quote("'"+TAB+"'!A1:BF6000",safe='')
    response=session.get(url,timeout=45);response.raise_for_status()
    result=response.json()
    if 'values' not in result:raise ValueError('Sheet API returned no values')
    return result['values']

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--output',default='data/hexa_hospitals.json')
    ap.add_argument('--input-json',help='Offline testing: JSON array of source sheet rows (never publish raw input)')
    args=ap.parse_args()
    if os.environ.get('HEXA_PUBLIC_PUBLISH_APPROVED','').lower()!='true':
        raise SystemExit('HEXA_PUBLIC_PUBLISH_APPROVED must equal true: no publication performed')
    path=Path(args.output)
    previous=json.loads(path.read_text('utf-8')) if path.exists() else None
    rows=json.loads(Path(args.input_json).read_text('utf-8')) if args.input_json else fetch_sheet(
      os.environ.get('HEXA_GOOGLE_CREDENTIALS_JSON',''),os.environ.get('HEXA_SHEET_ID',''))
    result=prepare(rows,previous)
    # atomic write, only after every check passes
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=str(path.parent),delete=False) as f:
        tmp=f.name;json.dump(result,f,ensure_ascii=False,separators=(',',':'));f.write('\n')
    os.replace(tmp,path)
    print(f"Published {len(result['hospitals'])} curated hospital profiles from {result['metadata']['rows_in_master']} source rows; {len(result['recent_changes'])} changes")

if __name__=='__main__':main()
