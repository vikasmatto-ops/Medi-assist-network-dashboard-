#!/usr/bin/env python3
"""Idempotent, guarded Star-only addition to NetworkIQ's five-source index.html."""
import argparse
import re
from pathlib import Path
MARKER='<!-- NETWORKIQ_STAR_SIXTH_SOURCE_V3_20261019 -->'
HELPERS=r'''
// Star snapshot is optional; incomplete/missing snapshots never enter the dashboard.
function niqValidateStar(snapshot){
  if(!snapshot||!snapshot.metadata||!Array.isArray(snapshot.hospitals))throw Error('Star snapshot schema missing');
  var m=snapshot.metadata;
  if(m.source_name!=='Star Health'||m.complete!==true||m.policy_specific_cashless_eligibility_verified!==false||
     m.query_count!==216||m.expected_query_count!==216||m.state_count!==36||
     !Array.isArray(m.categories_queried)||m.categories_queried.length!==6||
     m.verified_page2_distinct!==true||snapshot.hospitals.length<5000||m.source_count!==snapshot.hospitals.length)
     throw Error('Star completeness proof missing');
  var ids=new Set();
  snapshot.hospitals.forEach(function(r){
    if(!r.id||!r.name||!r.address||!r.city||!r.state||!r.pin||ids.has(r.id)||
       !Array.isArray(r.star_categories)||!r.star_categories.length||!r.star_flags||
       r.star_flags.excluded!==false||r.ppn!==false)
       throw Error('Unsafe or duplicated Star listing');
    ids.add(r.id);
  });
  return snapshot;
}
function niqStarBadges(r){
  if(r.source!=='Star Health')return '';
  var f=r.star_flags||{},c=Array.isArray(r.star_categories)?r.star_categories:[];
  var types=[];
  if(c.indexOf('Network')>=0)types.push('Network');
  if(c.indexOf('agreed')>=0||r.network_type==='ANH')types.push('Agreed');
  if(c.indexOf('value_network')>=0)types.push('Value Network');
  if(f.vsp===true)types.push('VSP');
  if(f.ayush===true)types.push('AYUSH');
  if(f.pratham===true)types.push('Pratham');
  if(f.smart_network===true)types.push('Smart Network');
  return types.map(function(t){return '<span class="badge niq-star-badge">'+esc(t)+'</span>';}).join('');
}
'''

def replace_once(s,old,new,description):
    n=s.count(old)
    if n!=1:raise ValueError('Unexpected NetworkIQ index for '+description+': '+str(n)+' anchors. No modifications made.')
    return s.replace(old,new,1)

def add_star_to_source_dropdown(s):
    """Edit q-source only: Niva also appears in the insurer dropdown."""
    pattern = re.compile(r'''(<select\b[^>]*\bid=[\"']q-source[\"'][^>]*>)(.*?)(</select>)''', re.DOTALL)
    matches = list(pattern.finditer(s))
    if len(matches) != 1:
        raise ValueError('Expected exactly one q-source dropdown; no modifications made.')
    match = matches[0]
    option = '<option value="Niva Bupa">Niva Bupa</option>'
    body = match.group(2)
    if body.count(option) != 1 or 'value="Star Health"' in body:
        raise ValueError('Unexpected q-source options; no modifications made.')
    amended = body.replace(option, option + '\n        <option value="Star Health" disabled>Star Health (pending validation)</option>', 1)
    return s[:match.start()] + match.group(1) + amended + match.group(3) + s[match.end():]

def patch(s):
    if MARKER in s:return s
    for needle in ('function niqValidateNiva(source)','function loadCurrentNetwork(){',
                   'function niqSourceFootnote(r){','function networkiqRows(rows,source){',
                   'function applyF(){','function buildKPI(){',
                   'NETWORKIQ_NIVA_FIFTH_SOURCE_KPI_V1_20261017'):
        if needle not in s:raise ValueError('Unsupported live dashboard revision: '+needle)
    out=s
    out=replace_once(out,'</head>',MARKER+'\n<link rel="stylesheet" href="./star_health.css?v=3">\n</head>','Star stylesheet')
    out=add_star_to_source_dropdown(out)
    out=replace_once(out,'<select class="ss" id="q-state" onchange="onStateChange()">',
        '<select class="ss" id="niq-star-category" onchange="applyF()" aria-label="Star hospital classification" hidden disabled>'+
        '<option value="">All Star categories</option><option value="Network">Network</option>'+
        '<option value="agreed">Agreed</option><option value="value_network">Value Network</option>'+
        '<option value="vsp">VSP</option><option value="ayush">AYUSH</option></select>\n      '+
        '<select class="ss" id="q-state" onchange="onStateChange()">','Star category filter')
    out=replace_once(out,'function niqValidateNiva(source){',HELPERS+'\nfunction niqValidateNiva(source){','Star validation helpers')
    out=replace_once(out,"if(source==='Niva Bupa')return (allIndia?'Niva Bupa':st)+' · '+n.toLocaleString('en-IN')+' PPN';",
        "if(source==='Star Health')return 'Star public-source listings · eligibility unverified';\n  if(source==='Niva Bupa')return (allIndia?'Niva Bupa':st)+' · '+n.toLocaleString('en-IN')+' PPN';",'Star KPI footnote')
    out=replace_once(out,"if(r.source==='Niva Bupa')return '<div class=\"niq-care-footnote\">Niva Bupa API listing · Policy-specific eligibility not verified</div>';",
        "if(r.source==='Star Health')return '<div class=\"niq-care-footnote\">Star official directory · Listing is not policy-specific cashless approval</div>';\n  if(r.source==='Niva Bupa')return '<div class=\"niq-care-footnote\">Niva Bupa API listing · Policy-specific eligibility not verified</div>';",'Star eligibility disclaimer')
    out=replace_once(out,"((r.source==='Care Health'||r.source==='Niva Bupa')?' niq-care-card':'')",
        "((r.source==='Care Health'||r.source==='Niva Bupa'||r.source==='Star Health')?' niq-care-card':'')",'Star card class')
    out=replace_once(out,"(r.source==='Care Health'?niqCareBadges(r):'')+",
        "(r.source==='Care Health'?niqCareBadges(r):'')+\n       niqStarBadges(r)+",'Star source badges')
    out=replace_once(out,"(insTxt&&r.source!=='Care Health'&&r.source!=='Niva Bupa'?",
        "(insTxt&&r.source!=='Care Health'&&r.source!=='Niva Bupa'&&r.source!=='Star Health'?",'No invented Star insurer panel')
    out=replace_once(out,"((r.source==='IndusInd General Insurance'||r.source==='Niva Bupa')&&r.network_type?",
        "((r.source==='IndusInd General Insurance'||r.source==='Niva Bupa'||r.source==='Star Health')&&r.network_type?",'Star network code badge')
    out=replace_once(out,'function applyF(){',
        "function applyF(){\n  var starChoice=document.getElementById('niq-star-category');\n  var isStar=document.getElementById('q-source').value==='Star Health';\n  starChoice.hidden=!isStar; starChoice.disabled=!isStar;",
        'Star filter visibility')
    out=replace_once(out,"if(source&&r.source!==source)return false;",
        "if(source&&r.source!==source)return false;\n    if(source==='Star Health'&&starChoice.value&&r.star_categories.indexOf(starChoice.value)<0)return false;",
        'Star classification filter')
    out=replace_once(out,"document.getElementById('q-ppn').checked=false;",
        "document.getElementById('q-ppn').checked=false; document.getElementById('niq-star-category').value='';",
        'Reset Star category')
    out=replace_once(out,"fetch('./data/niva_bupa.json',{cache:'no-store'}).then(function(res){",
        "fetch('./data/niva_bupa.json',{cache:'no-store'}).then(function(res){",'Niva unmodified anchor') if False else out
    # Isolate Star fetch: all five original promises remain untouched.
    old="""      return null;
    })
  ]).then(function(parts){
    var medi=parts[0], icici=parts[1], indus=parts[2], care=parts[3], niva=parts[4];"""
    new="""      return null;
    }),
    fetch('./data/star_health.json',{cache:'no-store'}).then(function(res){
      if(!res.ok)throw new Error('Star file HTTP '+res.status);return res.json();
    }).then(niqValidateStar).catch(function(err){
      console.warn('Star Health not loaded; keeping five existing sources untouched',err);
      return null;
    })
  ]).then(function(parts){
    var medi=parts[0], icici=parts[1], indus=parts[2], care=parts[3], niva=parts[4], star=parts[5];"""
    out=replace_once(out,old,new,'optional sixth fetch')
    out=replace_once(out,"(niva?networkiqRows(niva.hospitals,'Niva Bupa'):[]));",
        "(niva?networkiqRows(niva.hospitals,'Niva Bupa'):[]),(star?networkiqRows(star.hospitals,'Star Health'):[]));\n    var starOption=document.querySelector('#q-source option[value=\"Star Health\"]');\n    if(starOption){starOption.disabled=!star;starOption.textContent=star?'Star Health':'Star Health (pending validation)';}",
        'Star separated data rows')
    out=replace_once(out,"+' These are source records, not unique hospitals. Verify current policy-specific cashless eligibility with insurer/TPA.';",
        "+(star?' Star Health '+star.hospitals.length.toLocaleString('en-IN')+' verified-page official-source listings ('+networkiqDate(star.metadata.captured_utc)+') plus '+star.metadata.excluded_count.toLocaleString('en-IN')+' excluded providers stored separately.':' Star Health awaiting validated nationwide snapshot.')+' These are source records, not unique hospitals. Verify current policy-specific cashless eligibility with insurer/TPA.';",
        'Star publication disclosure')
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument('index',type=Path);args=ap.parse_args()
    original=args.index.read_text(encoding='utf-8');updated=patch(original)
    if updated==original:print('Star patch already applied; no change');return
    args.index.write_text(updated,encoding='utf-8');print('Star UI staged without changing existing source data')
if __name__=='__main__':main()
