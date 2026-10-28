"""Care public-site browser interaction helpers, based on the live-tested v7 Haryana pilot.
Only Care website native interactions and response observation; no direct API calls.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


LOCATOR_URL='https://www.careinsurance.com/'
FALLBACK_LOCATOR_URL='https://www.careinsurance.com/health-plan-network-hospitals.html'
API_PATTERN=re.compile(r'^https://ser\.careinsurance\.com/api/networkHospitalDetails(?:ByCityState)?(?:\?|$)',re.I)

def match_api(u):return bool(API_PATTERN.match(u))
def safe_path(u):return urlsplit(u).path

def diagnostic_js():
    return r'''() => {
      const vis = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
      const fields=[...document.querySelectorAll('input')].filter(vis)
        .filter(e=>/city|state|pincode|hospital/i.test([e.id,e.name,e.placeholder].join(' ')))
        .slice(0,14).map(e=>({id:e.id||'',name:e.name||'',placeholder:e.placeholder||'',type:e.type||''}));
      const buttons=[...document.querySelectorAll('button,input[type=submit],input[type=button],a,[role=button]')]
        .filter(vis).filter(e=>/search|submit/i.test((e.innerText||e.value||e.getAttribute('aria-label')||'').trim()))
        .slice(0,15).map(e=>({tag:e.tagName,id:e.id||'',text:(e.innerText||e.value||'').trim().slice(0,60)}));
      return {url_path:location.pathname,ready_state:document.readyState,
        title:document.title.slice(0,130),modern_field:fields.some(e=>/search by city|pincode, hospital/i.test(e.placeholder)),
        fields,search_buttons:buttons,body_has_hospital_locator:/hospital locator/i.test(document.body?.innerText||''),
        body_has_results:/Found\s+\d+\s+Hospitals/i.test(document.body?.innerText||''),
        page_inputs:document.querySelectorAll('input').length,
        body_excerpt:(document.body?.innerText||'').slice(0,300),
        iframe_count:document.querySelectorAll('iframe').length,
        jquery_present:typeof window.jQuery==='function'};
    }'''

async def modern_input(page,emit,max_wait=11):
    # Verified in the actual v5 live report: id=city_name_hospital, name=city_name_hospital,
    # placeholder=' '. Do NOT rely on the visible placeholder text, which is rendered separately.
    selectors=['#city_name_hospital','input[name="city_name_hospital"]',
               'input[placeholder*="search by city" i]',
               'input[placeholder*="either city or state name" i]']
    # The homepage locator can load only after its section becomes visible.
    # Scroll to a confirmed site label before deciding the input has vanished.
    try:
        await page.evaluate(r'''() => {
          const nodes=[...document.querySelectorAll('h1,h2,h3,h4,div,section,span')];
          const el=nodes.find(e=>e.children.length<4 &&
             /^hospital locator$/i.test((e.textContent||'').trim()) &&
             e.getBoundingClientRect().height>0);
          if(el)el.scrollIntoView({block:'center'});
        }''')
    except Exception as ex:emit('HOSPITAL SECTION SCROLL: '+str(ex).splitlines()[0][:100])
    for trial in range(max_wait):
        for selector in selectors:
            q=page.locator(selector)
            for i in range(min(await q.count(),3)):
                item=q.nth(i)
                try:
                    if await item.is_visible(timeout=500) and await item.is_enabled(timeout=500):
                        emit('CONFIRMED HOSPITAL FIELD: '+selector)
                        return item
                except Exception: pass
        if trial in (0,max_wait-1):
            try:
                d=await page.evaluate(diagnostic_js())
                emit('FIELD WAIT '+str(trial)+'s: '+str({k:d.get(k) for k in ('url_path','title','ready_state','page_inputs','iframe_count','body_has_hospital_locator','fields')})[:350])
            except Exception as e: emit('FIELD CHECK pending navigation: '+str(e).splitlines()[0][:110])
        await asyncio.sleep(1)
    emit('FAILED: Locator hospital input missing; check page diagnostics')
    return None

async def click_search(page,target,emit):
    # Verified in v5 report: BUTTON#find_location labelled Search. Explicitly click THAT button;
    # only use geometrical fallback if Care renames it in a later release.
    for selector in ('#find_location','button[id="find_location"]'):
        b=page.locator(selector)
        try:
            if await b.count() and await b.first.is_visible(timeout=800) and await b.first.is_enabled(timeout=800):
                emit('CONFIRMED SEARCH BUTTON: '+selector)
                await b.first.click(timeout=16000)
                emit('CLICKED Care homepage hospital Search')
                return True
        except Exception as e:
            emit('BUTTON '+selector+' click issue: '+str(e).splitlines()[0][:190])
    # Guarded fallback is the button closest to the known field, NOT a random Search link.
    data=await target.evaluate(r'''input => {
       document.querySelectorAll('[data-ni-search]').forEach(e=>e.removeAttribute('data-ni-search'));
       const rect=input.getBoundingClientRect();
       const bs=[...document.querySelectorAll('button,input[type=submit],input[type=button]')]
         .filter(e=>/search/i.test((e.innerText||e.value||'').trim()));
       const candidates=bs.map(e=>({e,r:e.getBoundingClientRect()}))
         .filter(({r})=>r.width>12&&r.height>12)
         .map(({e,r})=>({e,d:Math.hypot((r.left+r.right-rect.left-rect.right)/2,
                                           (r.top+r.bottom-rect.top-rect.bottom)/2)}))
         .sort((a,b)=>a.d-b.d);
       if(!candidates.length || candidates[0].d>650)return {found:false,button_count:bs.length};
       candidates[0].e.setAttribute('data-ni-search','yes');
       return {found:true,distance:Math.round(candidates[0].d)};
    }''')
    emit('BUTTON FALLBACK '+json.dumps(data))
    if data.get('found'):
        try:
            await page.locator('[data-ni-search="yes"]').click(timeout=10000)
            emit('CLICKED nearest actual button')
            return True
        except Exception as e:emit('Fallback click issue: '+str(e).splitlines()[0][:170])
    return False

async def search_form(page,state,emit):
    target=await modern_input(page,emit)
    if target is None:
        # This is the insurer's official dedicated network-hospital locator,
        # not a guessed third-party proxy or direct API. We still require a
        # real website-generated, validated networkHospitalDetails response.
        emit('OFFICIAL LOCATOR FALLBACK: navigating to dedicated hospital-list page')
        try:
            await page.goto(FALLBACK_LOCATOR_URL,wait_until='domcontentloaded',timeout=60000)
            try:await page.wait_for_load_state('load',timeout=16000)
            except Exception:pass
            target=await modern_input(page,emit,max_wait=9)
        except Exception as ex:
            emit('DEDICATED LOCATOR FAILED: '+str(ex).splitlines()[0][:230])
        if target is None:return False
    try:
        await target.fill(state,timeout=12000)
        actual=await target.input_value()
        if actual.strip().casefold()!=state.strip().casefold():
            emit('ERROR: Hospital query field did not retain state');return False
        emit('SEARCH FIELD VALUE VERIFIED: '+state)
        # Normal website action: fill known field, click button; no independent API call.
        await target.press('Tab',timeout=4000)
        await asyncio.sleep(0.8)
        return await click_search(page,target,emit)
    except Exception as e:
        emit('HOSPITAL SEARCH FAILURE: '+str(e).splitlines()[0][:190]);return False

async def wheel_hospital_list(page, cycle=0):
    """Scroll the actual Care hospital-list widget with user-like wheel events.

    Important: v6 scrolled window + inner list every round, so the list moved out
    of view and reached scrollTop=304 without page 3 loading. v7 keeps a list
    container visible, and sends a real Chrome wheel event over its centre.
    """
    before = await page.evaluate(r'''() => {
        document.querySelectorAll('[data-ni-hospital-scroll]').forEach(e=>e.removeAttribute('data-ni-hospital-scroll'));
        const vis=e=>{
            const r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return r.width>=210 && r.height>=110 && s.display!=='none' && s.visibility!=='hidden';
        };
        const nodes=[...document.querySelectorAll('div,section,ul,main,article')];
        const candidates=nodes.filter(e=>{
            const s=getComputedStyle(e);
            return vis(e) && e.scrollHeight>e.clientHeight+35 && /auto|scroll/.test(s.overflowY);
        }).map(e=>{
            const r=e.getBoundingClientRect(), txt=(e.innerText||'').slice(0,3500);
            const signals=(/doctors?\s*[•·]|doctors?\s*:/i.test(txt)?2:0)+
                          (/beds?\s*[•·]|beds?\s*:/i.test(txt)?2:0)+
                          (/hospital/i.test(txt)?1:0)+(/\bFound\s+\d+\s+Hospitals/i.test(txt)?3:0);
            const distance=Math.abs((r.top+r.bottom)/2-innerHeight/2);
            const score=signals*15 + Math.min(10,(e.scrollHeight-e.clientHeight)/150)-distance/150;
            return {e,r,score,signals,top:e.scrollTop,max:e.scrollHeight-e.clientHeight,
                    height:e.clientHeight,scrollHeight:e.scrollHeight};
        }).sort((a,b)=>b.score-a.score);
        const best=candidates[0];
        const describe=(v)=>({top:Math.round(v.top),max:Math.round(v.max),
             view:Math.round(v.height),height:Math.round(v.scrollHeight),
             score:+v.score.toFixed(1),signals:v.signals,
             tag:v.e.tagName,id:v.e.id||'',classes:(''+v.e.className).slice(0,75)});
        if(!best) return {found:false,candidate_count:0,url_path:location.pathname};
        const el=best.e;
        el.setAttribute('data-ni-hospital-scroll','yes');
        el.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
        const r=el.getBoundingClientRect();
        const x=Math.max(6,Math.min(innerWidth-6,r.left+Math.min(r.width*.56,320)));
        const y=Math.max(6,Math.min(innerHeight-6,r.top+Math.min(r.height*.60,260)));
        return {found:true,count:candidates.length,target:describe(best),
           others:candidates.slice(1,3).map(describe),x:Math.round(x),y:Math.round(y)};
    }''')
    if not before.get('found'):
        # No independently generated HTTP calls. Diagnose page, do not advance window
        # indiscriminately because scrolling away was the confirmed v6 weakness.
        return {'mode':'no_hospital_scroll_element', 'before':before}
    x,y=before['x'],before['y']
    await page.mouse.move(x,y)
    # Wheel input goes through Chrome's regular scroll/event pipeline. Site should
    # see the same events that it sees from a human scrolling its results pane.
    await page.mouse.wheel(0,550 + (cycle%3)*85)
    await asyncio.sleep(0.65)
    after=await page.evaluate(r'''() => {
        const e=document.querySelector('[data-ni-hospital-scroll="yes"]');
        if(!e)return {target_missing:true};
        return {top:Math.round(e.scrollTop),max:Math.round(e.scrollHeight-e.clientHeight),
            view:Math.round(e.clientHeight),height:Math.round(e.scrollHeight)};
    }''')
    return {'mode':'mouse_wheel_in_results', 'before':before, 'after':after}


async def inspect_list(page):
    return await page.evaluate(r'''() => {
        const vis=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
        const texts=[...document.querySelectorAll('button,a,[role=button],input[type=button]')]
          .filter(vis).map(e=>({tag:e.tagName,id:e.id||'',name:(e.innerText||e.value||'').trim().slice(0,44)}))
          .filter(v=>/^(load more|show more|next|more hospitals|view more)$/i.test(v.name))
          .slice(0,15);
        const f=(document.body?.innerText||'').match(/Found\s+\d+\s+Hospitals[^\n]*/i);
        return {url_path:location.pathname, found_text:f?f[0].slice(0,70):null,
                possible_pagination_controls:texts,
                chosen_scroll:!!document.querySelector('[data-ni-hospital-scroll]')};
    }''')
