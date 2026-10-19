#!/usr/bin/env python3
"""Minimal first-party HTTPS transport for Star's public Next.js Server Actions.
No stored action IDs, cookies, authentication, browser automation or DOM scraping.
"""
import html.parser
import json
import re
from urllib.error import HTTPError
from urllib.parse import urljoin,urlparse
from urllib.request import Request,build_opener,HTTPRedirectHandler

ROOT='https://www.starhealth.in'
PAGE=ROOT+'/lookup/hospital/?page=network-lab'
UA='Mozilla/5.0 (compatible; NetworkIQ hospital-directory source collector/1.0)'
CATEGORIES=('Network','agreed','value_network','vsp','ayush','nonpreferred')
STATES=(
 'Andaman and Nicobar Islands','Andhra Pradesh','Arunachal Pradesh','Assam','Bihar',
 'Chandigarh','Chhattisgarh','Dadra and Nagar Haveli and Daman and Diu','Delhi',
 'Goa','Gujarat','Haryana','Himachal Pradesh','Jammu and Kashmir','Jharkhand',
 'Karnataka','Kerala','Ladakh','Lakshadweep','Madhya Pradesh','Maharashtra',
 'Manipur','Meghalaya','Mizoram','Nagaland','Odisha','Puducherry','Punjab',
 'Rajasthan','Sikkim','Tamil Nadu','Telangana','Tripura','Uttar Pradesh',
 'Uttarakhand','West Bengal')

def first_party(url):
    p=urlparse(url)
    return p.scheme=='https' and p.hostname=='www.starhealth.in' and p.port is None and not p.username

class StarRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        if not first_party(newurl):raise RuntimeError('Non-Star redirect refused')
        return super().redirect_request(request,fp,code,msg,headers,newurl)

opener=build_opener(StarRedirect())

def fetch(url,method='GET',data=None,headers=None):
    if not first_party(url):raise RuntimeError('Non-Star source URL refused')
    h={'User-Agent':UA,'Accept':'text/html,application/javascript,*/*','Accept-Encoding':'identity'}
    if headers:h.update(headers)
    req=Request(url,data=data,method=method,headers=h)
    try:
        with opener.open(req,timeout=45) as res:
            payload=res.read(13_000_001)
            if len(payload)>13_000_000:raise RuntimeError('Source response size cap exceeded')
            return res.status,res.headers.get('content-type',''),payload.decode('utf-8','replace')
    except HTTPError as e:
        return e.code,e.headers.get('content-type',''),e.read(16_000).decode('utf-8','replace')

class Scripts(html.parser.HTMLParser):
    def __init__(self):super().__init__();self.urls=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script':
            src=dict(attrs).get('src')
            if src:self.urls.append(urljoin(PAGE,src))

def get_live_action_id():
    st,_,html=fetch(PAGE)
    if st!=200:raise RuntimeError('Official Star locator unavailable: HTTP '+str(st))
    parser=Scripts();parser.feed(html)
    scripts=[u for u in dict.fromkeys(parser.urls) if first_party(u) and urlparse(u).path.endswith('.js')]
    for url in scripts[:75]:
        st,_,js=fetch(url)
        if st!=200:continue
        match=re.search(r'createServerReference\)\("([a-f0-9]{40,64})",[^)]{0,190}?"getHospitalsData_nw"\)',js)
        if match:return match.group(1),url
    raise RuntimeError('Live getHospitalsData_nw action not found in current scripts')

def make_args(state='',city='',hospital='',category='Network',page=1,page_size=10,flag=''):
    if category not in CATEGORIES:raise ValueError('Unknown source category')
    if type(page) is not int or page<1 or type(page_size) is not int or not 1<=page_size<=10:raise ValueError('Invalid request pagination')
    return [state,city,hospital,category,flag,page,page_size]

def parse_flight(blob):
    values=[];errors=[]
    for line in blob.splitlines():
        m=re.match(r'^([0-9a-f]+):(.+)$',line)
        if not m:continue
        val=m.group(2).strip()
        if val.startswith('E{'):errors.append(val[:400]);continue
        if val[:1] not in ('{','['):continue
        try:values.append(json.loads(val))
        except json.JSONDecodeError:continue
    return values,errors

def first_obj(values):
    for value in reversed(values):
        if isinstance(value,dict) and any(k in value for k in ('details','count','totalpages','errorDesc','data')):return value
    return None
