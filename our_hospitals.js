/* NetworkIQ "Our Hospitals". Public, CURATED feed. No embedded staff, rate or comment data. */
(function(){'use strict';
const panel=document.getElementById('nihexa-panel');
if(!panel)return;
const $=(id)=>document.getElementById(id);
const es=(v)=>String(v==null?'':v).replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const showLabel={ACTIVE:'Active',INACTIVE:'Inactive',ON_HOLD:'On Hold',UNKNOWN:'Unknown',REVIEW:'Needs review',YES:'Yes',NO:'No',CONDITIONAL:'Conditional'};
let feed=null,shown=30,paneOpen=false,lastFetch=0;
function countFormat(n){return Number(n||0).toLocaleString('en-IN')}
function norm(v){return String(v||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim()}
function normalName(v){return norm(v).split(' ').filter(x=>!['hospital','hospitals','pvt','ltd','private','limited','the','a','unit','of'].includes(x)).join(' ')}
function code(status){return '<span class="nh-pill '+es(status)+'">'+es(showLabel[status]||status||'Unknown')+'</span>'}
function miniStats(h){let c={YES:0,NO:0,CONDITIONAL:0,REVIEW:0};Object.values(h.empanelment||{}).forEach(s=>{if(c[s]!==undefined)c[s]++});return c}
function officialComparison(h,provider){
  // This is ONLY a matching search aid, NOT a verification of a policy's cashless entitlement.
  let src='';let p=norm(provider);
  if(p.includes('care health'))src='Care Health';
  else if(p.includes('icici lombard'))src='ICICI Lombard';
  else if(p.includes('medi assist')||p.includes('mediassist'))src='Medi Assist';
  else if(p.includes('indusind general'))src='IndusInd General Insurance';
  if(!src)return 'Official comparison: not supported for this provider';
  if(typeof DATA==='undefined'||!Array.isArray(DATA)||!DATA.length)return 'Official comparison: source feed not loaded';
  const name=normalName(h.name),pin=String(h.pin||'').trim();
  const r=DATA.find(r=>r.source===src&&normalName(r.name)===name&&pin&&String(r.pin||'').trim()===pin);
  return r?'Official '+src+' directory: matching listing found (policy eligibility unverified)':'Official '+src+' directory: exact name + PIN match not found in captured data';
}
function valid(x){return x&&x.metadata&&x.metadata.schema==='networkiq.hexa.public.v1'&&Array.isArray(x.hospitals)&&x.hospitals.length>=50&&x.hospitals.length===x.metadata.record_count&&Array.isArray(x.providers)&&x.providers.length>=35}
function fillSelections(){
 let city=$('nihexa-city'),val=city.value;let cities=Array.from(new Set(feed.hospitals.map(h=>h.city).filter(Boolean))).sort((a,b)=>a.localeCompare(b));
 city.innerHTML='<option value="">All cities</option>'+cities.map(c=>'<option value="'+es(c)+'">'+es(c)+' ('+countFormat(feed.hospitals.filter(h=>h.city===c).length)+')</option>').join('');city.value=cities.includes(val)?val:'';
 const sel=$('nihexa-provider'),v=sel.value;
 sel.innerHTML='<option value="">All insurers & TPAs</option>'+feed.providers.map(x=>'<option value="'+es(x)+'">'+es(x)+'</option>').join('');sel.value=feed.providers.includes(v)?v:'';
}
function summarize(){
 const s=feed.metadata.status_counts||{};
 $('nihexa-kpis').innerHTML=[['Hospitals',feed.hospitals.length],['Active',s.ACTIVE],['On Hold',s.ON_HOLD],['Inactive',s.INACTIVE],['Needs review',feed.hospitals.filter(h=>h.conflict||h.status==='REVIEW').length]].map(z=>'<div class="nh-kpi"><div class="nh-kpi-label">'+es(z[0])+'</div><div class="nh-kpi-num">'+countFormat(z[1])+'</div></div>').join('');
 const when=new Date(feed.metadata.as_of_utc);
 $('nihexa-updated').textContent='Sheet snapshot: '+(isNaN(when.getTime())?'unknown':when.toLocaleString('en-IN',{dateStyle:'medium',timeStyle:'short'}))+' · '+countFormat(feed.metadata.rows_in_master)+' source rows · '+countFormat(feed.metadata.duplicates)+' duplicate groups';
 let changes=feed.recent_changes||[];
 $('nihexa-changes').innerHTML='<h2>Recent changes in the published master</h2>'+(changes.length?changes.slice(0,15).map(ch=>'<p><strong>'+es(ch.change.replace(/_/g,' '))+'</strong> · '+es(ch.name)+' · '+es(ch.city)+(ch.to?' → '+es(ch.to):'')+'</p>').join(''):'<p>No changes recorded since the last published snapshot.</p>');
}
function filtered(){let q=norm($('nihexa-search').value),city=$('nihexa-city').value,stat=$('nihexa-status').value,p=$('nihexa-provider').value;
 return feed.hospitals.filter(h=>(!q||norm(h.name+' '+h.city+' '+h.pin).includes(q))&&(!city||h.city===city)&&(!stat||h.status===stat)&&(!p||h.empanelment[p]!==undefined));
}
function render(){if(!feed)return;const matched=filtered(),provider=$('nihexa-provider').value;
 $('nihexa-found').textContent=countFormat(matched.length)+' hospitals'+(provider?' · '+provider:'');
 $('nihexa-hospitals').innerHTML=matched.slice(0,shown).map(h=>{
  let summary=miniStats(h);let pstat=provider?(h.empanelment[provider]||'UNKNOWN'):null;
  let info=provider?'Hexa empanelment: '+code(pstat):'Yes: '+summary.YES+' · No: '+summary.NO+' · Conditional: '+summary.CONDITIONAL;
  let compare=provider?'<div class="nh-line">'+es(officialComparison(h,provider))+'</div>':'';
  let matrix=Object.entries(h.empanelment||{}).map(([k,v])=>'<div><span>'+es(k)+'</span>'+code(v)+'</div>').join('');
  return '<article class="nh-card"><div class="nh-card-head"><div><div class="nh-name">'+es(h.name)+'</div><div class="nh-where">'+es(h.city)+(h.pin?' · PIN '+es(h.pin):'')+'</div></div><div class="nh-pills">'+code(h.status)+(h.conflict?'<span class="nh-pill REVIEW">Conflicting duplicate</span>':(h.duplicate_rows>1?'<span class="nh-pill REVIEW">Repeated master entry</span>':''))+'</div></div><div class="nh-line">'+info+'</div>'+compare+'<details class="nh-detail"><summary>View all '+Object.keys(h.empanelment).length+' insurer / TPA statuses</summary><div class="nh-matrix">'+matrix+'</div><p>As recorded by Hexa; not independent proof of cashless eligibility. Unknown and Needs review must be confirmed.</p></details></article>';}).join('')||'<div class="nh-empty">No hospitals match these filters.</div>';
 $('nihexa-more').hidden=matched.length<=shown;
}
async function load(force){if(!force&&feed&&Date.now()-lastFetch<5*60*1000)return;
 $('nihexa-error').textContent='Checking the published Hexa network snapshot…';
 try{const r=await fetch('./data/hexa_hospitals.json?t='+Math.floor(Date.now()/300000),{cache:'no-store'});
  if(!r.ok)throw Error('Curated hospital feed not published yet ('+r.status+')');
  const x=await r.json();if(!valid(x))throw Error('Public Hexa data failed integrity checks');
  feed=x;lastFetch=Date.now();$('nihexa-error').textContent='';fillSelections();summarize();render();
 }catch(e){$('nihexa-error').textContent='Our Hospitals data is not yet available: '+e.message+'. The main insurer directory remains available.';if(!feed)$('nihexa-hospitals').innerHTML='';}
}
$('nihexa-launch').addEventListener('click',()=>{paneOpen=true;panel.style.display='block';document.body.style.overflow='hidden';load(false)});
$('nihexa-close').addEventListener('click',()=>{paneOpen=false;panel.style.display='none';document.body.style.overflow=''});
['nihexa-search','nihexa-city','nihexa-status','nihexa-provider'].forEach(id=>$(id).addEventListener(id==='nihexa-search'?'input':'change',()=>{shown=30;render()}));
$('nihexa-more-button').addEventListener('click',()=>{shown+=30;render()});
$('nihexa-export').addEventListener('click',()=>{if(!feed)return;
 const provider=$('nihexa-provider').value;
 function safe(s){s=String(s??'');if(/^[=+\-@\t\r]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"'}
 const cols=['Hospital','City','PIN','Status','Duplicate conflict',...(provider?[provider]:[])];
 let lines=[cols.map(safe).join(',')];for(const h of filtered())lines.push([h.name,h.city,h.pin,h.status,h.conflict?'Yes':'No',...(provider?[h.empanelment[provider]||'UNKNOWN']:[])].map(safe).join(','));
 const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8;'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='NetworkIQ_OurHospitals_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
});
setInterval(()=>{if(paneOpen)load(true)},30*60*1000);
})();
