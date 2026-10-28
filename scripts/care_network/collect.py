"""Collect Care Health IPD network by state using the site's own browser UI.

No direct API POST or request-header/cookie/token persistence. State failure is
isolated. Writes atomic per-state diagnostic JSON even if collection is partial.
"""
from __future__ import annotations
import argparse,asyncio,json,re,sys,time
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime,timezone
from playwright.async_api import async_playwright
try:
    from .browser_helpers import LOCATOR_URL,match_api,search_form,wheel_hospital_list,inspect_list,diagnostic_js
    from .quality import STATES,slug,state_matches,decode_page,reconcile,ValidationError
except ImportError:
    from browser_helpers import LOCATOR_URL,match_api,search_form,wheel_hospital_list,inspect_list,diagnostic_js
    from quality import STATES,slug,state_matches,decode_page,reconcile,ValidationError


def atomic_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')
    tmp.replace(path)

def safe_host_path(url):
    u=urlsplit(url)
    return {'host':u.hostname,'path':u.path}

async def capture_state(browser,state,output,timeout_seconds=650,headed=False):
    started=time.monotonic()
    pages={}; pending=set();events=[];failures=[];observed=[]; errors=[]; rejected=[]
    context=await browser.new_context(viewport={'width':1480,'height':960},accept_downloads=False)
    # Per-state fresh context isolates the previous query and any cached state.
    page=await context.new_page();search_armed=False
    def log(s):
        msg=str(s).replace('\n',' ')[:250]
        events.append(msg); events[:]=events[-100:]
        print(f'[{state}] {msg}',flush=True)
    def save():
        doc=reconcile(pages,state,errors)
        m=doc['metadata']
        m['events']=events[-90:]
        m['response_observations']=observed[-70:]
        m['rejected_responses']=rejected[-20:]
        m['request_failures']=failures[-20:]
        m['collector_version']='panindia-1.0'
        atomic_json(output,doc)
        return doc
    async def observe(response):
        if not match_api(response.url):return
        obs={**safe_host_path(response.url),'status':int(response.status)}
        observed.append(obs)
        if response.status!=200 or not search_armed: return
        try:
            decoded=decode_page(await response.json())
        except Exception as ex:
            rejected.append({'status':response.status,'reason':str(ex)[:190]});return
        rows=decoded['rows']; matches=sum(state_matches(r.get('state'),state) for r in rows)
        # Homepage might issue its own default Maharashtra response. Never
        # misclassify that as a successful search. One anomalous row is allowed
        # in source capture, and later blocks automatic publishing.
        if not rows or matches<max(1,len(rows)-2):
            rejected.append({'page':decoded['page'],'reason':'Response mostly from another state',
                             'state_counts':dict(__import__('collections').Counter(str(r.get('state')) for r in rows))})
            return
        if pages:
            first=next(iter(pages.values()))
            if tuple(decoded[k] for k in ('pages','total','limit'))!=tuple(first[k] for k in ('pages','total','limit')):
                errors.append(f'Source pagination changed on page {decoded["page"]}');return
        existing=pages.get(decoded['page'])
        if existing and existing['rows']!=decoded['rows']:
            errors.append(f'Source page {decoded["page"]} changed during run');return
        if not existing:
            pages[decoded['page']]=decoded
            log(f'CAPTURED page {decoded["page"]}/{decoded["pages"]}, {len(rows)} rows, {matches} state matches')
            save()
    def on_resp(response):
        task=asyncio.create_task(observe(response))
        pending.add(task);task.add_done_callback(pending.discard)
    context.on('response',on_resp)
    def on_fail(req):
        host=urlsplit(req.url).hostname or ''
        if host.endswith('careinsurance.com') and len(failures)<40:
            failures.append({**safe_host_path(req.url),'problem':str(req.failure)[:130]})
    context.on('requestfailed',on_fail)
    try:
        log('Opening official Care homepage and observing website requests')
        await page.goto(LOCATOR_URL,wait_until='domcontentloaded',timeout=85000)
        try: await page.wait_for_load_state('load',timeout=25000)
        except Exception: log('Page load not fully settled; trying official search controls')
        await asyncio.sleep(1)
        # Prevent pre-click default records from being counted; browser emits
        # its own background requests, but they are not our state query.
        search_armed=True
        clicked=await search_form(page,state,log)
        if not clicked:
            try:
                evidence=await page.evaluate(diagnostic_js())
                log('SITE_DIAGNOSTIC: '+json.dumps(evidence,ensure_ascii=False)[:220])
                observed.append({'page_diagnostic':evidence})
                await page.screenshot(path=str(output.with_name(output.stem+'-page.png')),timeout=9000)
            except Exception as ex:log('SITE_DIAGNOSTIC_ERROR: '+str(ex).splitlines()[0][:150])
            errors.append('Official Care state search controls could not be submitted')
            return save()
        for _ in range(45):
            if pages or time.monotonic()-started>timeout_seconds:break
            await asyncio.sleep(1)
        if not pages:
            errors.append('No valid matching state API page after official Search click')
            return save()
        idle=0;prior=len(pages)
        while time.monotonic()-started < timeout_seconds:
            report=reconcile(pages,state)
            if report['metadata']['source_pages_complete']:
                log(f'SOURCE COMPLETE {len(pages)}/{report["metadata"]["pages_expected"]}')
                return save()
            tab=next((t for t in reversed(context.pages) if not t.is_closed() and 'careinsurance.com' in t.url),page)
            try: scroll=await wheel_hospital_list(tab,idle)
            except Exception as exc: scroll={'mode':'exception','error':str(exc)[:110]}
            await asyncio.sleep(0.9)
            if len(pages)>prior: idle=0;prior=len(pages)
            else:idle+=1
            if idle in (8,19):
                log('Waiting for pages; '+str(scroll)[:160])
                try: log('SITE: '+str(await inspect_list(tab))[:160])
                except Exception:pass
                save()
            if idle in (10,20) and scroll.get('mode')=='mouse_wheel_in_results':
                try:
                    await tab.mouse.move(scroll['before']['x'],scroll['before']['y'])
                    await tab.mouse.wheel(0,-280)
                    await asyncio.sleep(0.55)
                    await tab.mouse.wheel(0,700)
                except Exception:pass
            if idle>=29:break
        if time.monotonic()-started >= timeout_seconds:
            errors.append(f'State collection exceeded {timeout_seconds}s safety limit')
        else:errors.append('Website scrolling stopped before all pages were returned')
        return save()
    except Exception as exc:
        errors.append('Browser/website error: '+str(exc)[:250])
        return save()
    finally:
        if pending:
            try: await asyncio.wait(pending,timeout=4)
            except Exception:pass
        save()
        await context.close()


def chosen_states(args):
    if args.all_states:
        items=list(STATES)
    else:
        if not args.states: raise ValueError('Use --all-states or --states "Haryana,Delhi"')
        selected=[x.strip() for x in args.states.split(',') if x.strip()]
        ref={slug(s):s for s in STATES}
        items=[]
        for x in selected:
            name=ref.get(slug(x))
            if not name: raise ValueError(f'Unknown Indian state/UT: {x!r}')
            if name not in items: items.append(name)
    if args.shard_count < 1 or not 0<=args.shard_index<args.shard_count:
        raise ValueError('Invalid shard index/count')
    return [s for index,s in enumerate(items) if index%args.shard_count==args.shard_index]

async def run(args):
    states=chosen_states(args)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    if not states:print('No states assigned to this shard');return 0
    outcomes=[]
    blocked_all=True
    async with async_playwright() as p:
        kw={'headless':not args.headed}
        if args.browser_channel=='chrome':kw['channel']='chrome'
        browser=await p.chromium.launch(**kw)
        try:
            for state in states:
                print(f'\n=== {state}: website capture ===',flush=True)
                out=args.output_dir/(slug(state)+'.json')
                doc=await capture_state(browser,state,out,args.state_timeout,args.headed)
                meta=doc['metadata']
                outcomes.append({'state':state,'publishable':meta['publishable'],
                                 'source_complete':meta['source_pages_complete'],
                                 'observed':meta['source_records_observed'],
                                 'expected':meta['expected_source_records'],
                                 'off_state':meta['off_state_records'],
                                 'file':out.name})
                print(f'RESULT {state}: {meta["source_records_observed"]}/{meta["expected_source_records"]}, '+
                      f'publishable={meta["publishable"]}, off_state={meta["off_state_records"]}',flush=True)
                no_ui=('Official Care state search controls could not be submitted' in meta.get('errors',[])
                       and not meta['source_records_observed'])
                no_source_api=(not meta['source_records_observed']
                    and 'No valid matching state API page after official Search click' in meta.get('errors',[]))
                if not (no_ui or no_source_api):blocked_all=False
                elif no_ui and len(outcomes)==1:
                    # A missing form is a page-level blocker, not 36 independent
                    # state failures. Preserve a screenshot then stop this shard.
                    print('CARE_LOCATOR_UI_BLOCKED: stopped shard after first form failure',flush=True)
                    break
                elif no_source_api and len(outcomes)>=2 and blocked_all:
                    print('CARE_API_OBSERVATION_BLOCKED: two searches yielded no official source frames',flush=True)
                    break
        finally:await browser.close()
    summary={'generated_at_utc':datetime.now(timezone.utc).isoformat(),
             'collector':'care-panindia-1.0','shard_index':args.shard_index,
             'shard_count':args.shard_count,'results':outcomes,
             'blocked_all':blocked_all}
    atomic_json(args.output_dir/'_run_summary.json',summary)
    # Partial states remain reportable; an entirely blocked browser must NOT
    # be labelled a green successful hospital capture.
    if blocked_all:
        print('CARE_CAPTURE_FAILED_NO_VALID_SOURCE_PAGES: source unchanged',flush=True)
        return 2
    return 0

def cli():
    ap=argparse.ArgumentParser(description='Care Health pan-India state/UT collector')
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--all-states',action='store_true')
    g.add_argument('--states',type=str)
    ap.add_argument('--shard-count',type=int,default=1)
    ap.add_argument('--shard-index',type=int,default=0)
    ap.add_argument('--output-dir',type=Path,default=Path('care_staging'))
    ap.add_argument('--headed',action='store_true',help='Use visible browser on Windows')
    ap.add_argument('--browser-channel',choices=['chrome','chromium'],default='chromium')
    ap.add_argument('--state-timeout',type=int,default=650)
    return ap.parse_args()

if __name__=='__main__':
    try: sys.exit(asyncio.run(run(cli())))
    except (ValueError,RuntimeError) as exc:
        print('CONFIG ERROR:',exc,file=sys.stderr);sys.exit(2)
