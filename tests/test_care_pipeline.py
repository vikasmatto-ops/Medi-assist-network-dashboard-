import json,tempfile,unittest
from pathlib import Path
from scripts.care_network.quality import STATES,slug,state_matches,decode_page,reconcile,ValidationError
from scripts.care_network.collect import chosen_states
from scripts.care_network.browser_helpers import FALLBACK_LOCATOR_URL,LOCATOR_URL,diagnostic_js
from scripts.care_network.publish import plan,apply,inspect_state


def payload(page,pages,total,limit,rows):
    return {'status':True,'statusCode':200,'response':{'pagination':{
        'currentPage':page,'totalPages':pages,'totalRecords':total,'limit':limit},'data':rows}}

def record(i,state='haryana'):
    return {'id':i,'hospital_code':str(i),'hospital_name':f'Hospital {i}',
            'state':state,'city':'gurugram','hospital_category':'NL_SMARTSELECT'}

class EndToEnd(unittest.TestCase):
    def test_official_care_locator_fallback_is_first_party(self):
        self.assertEqual(LOCATOR_URL,'https://www.careinsurance.com/')
        self.assertEqual(FALLBACK_LOCATOR_URL,'https://www.careinsurance.com/health-plan-network-hospitals.html')
        self.assertIn('page_inputs',diagnostic_js())
    def test_state_coverage_and_shards(self):
        self.assertEqual(len(STATES),36)
        a=type('Args',(),{'all_states':True,'states':None,'shard_index':0,'shard_count':6})()
        seen=[]
        for i in range(6):
            a.shard_index=i; seen+=chosen_states(a)
        self.assertEqual(sorted(seen),sorted(STATES))
        self.assertEqual(len(seen),len(set(seen)))
    def test_validator_keeps_mixed_source_page_quarantined(self):
        rows=[record(i) for i in range(1,10)]+[record(10,'rajasthan')]
        p=decode_page(payload(1,1,10,10,rows))
        d=reconcile({1:p},'Haryana')
        self.assertTrue(d['metadata']['source_pages_complete'])
        self.assertFalse(d['metadata']['publishable'])
        self.assertEqual(d['metadata']['matching_state_records'],9)
        self.assertEqual(d['quarantined_hospitals'][0]['hospital']['state'],'rajasthan')
    def test_distinct_id_and_missing_page(self):
        p1=decode_page(payload(1,2,3,2,[record(1),record(2)]))
        p2=decode_page(payload(2,2,3,2,[record(2)]))
        self.assertFalse(reconcile({1:p1,2:p2},'Haryana')['metadata']['source_pages_complete'])
        self.assertFalse(reconcile({1:p1},'Haryana')['metadata']['source_pages_complete'])
    def test_bad_response(self):
        with self.assertRaises(ValidationError):decode_page(payload(1,2,12,10,[record(1)]))
    def test_alias(self):
        self.assertTrue(state_matches('Orissa','Odisha'))
        self.assertFalse(state_matches('Rajasthan','Haryana'))
    def test_guarded_publish_and_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); staging=root/'stage';staging.mkdir()
            doc=reconcile({1:decode_page(payload(1,1,20,20,[record(i) for i in range(1,21)]))},'Haryana')
            (staging/'haryana.json').write_text(json.dumps(doc))
            result,approved=plan(staging,root)
            self.assertEqual(result['approved'],['Haryana'])
            self.assertFalse((root/'data').exists())
            apply(result,approved,root)
            old_path=root/'data/care_health_states/haryana.json'
            self.assertEqual(len(json.loads(old_path.read_text())['hospitals']),20)
            # Complete smaller run must be held if drop >5%; old stays.
            new=reconcile({1:decode_page(payload(1,1,10,10,[record(i) for i in range(1,11)]))},'Haryana')
            (staging/'haryana.json').write_text(json.dumps(new))
            result,approved=plan(staging,root)
            self.assertFalse(approved)
            self.assertIn('reduction',result['held']['Haryana'])
            apply(result,approved,root)
            self.assertEqual(len(json.loads(old_path.read_text())['hospitals']),20)
    def test_cross_state_collision_prevents_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);stage=root/'stage';stage.mkdir()
            h=reconcile({1:decode_page(payload(1,1,2,2,[record(1),record(2)]))},'Haryana')
            r=reconcile({1:decode_page(payload(1,1,2,2,[record(2,'rajasthan'),record(3,'rajasthan')]))},'Rajasthan')
            (stage/'haryana.json').write_text(json.dumps(h))
            (stage/'rajasthan.json').write_text(json.dumps(r))
            report,selected=plan(stage,root)
            self.assertEqual(selected,{})
            self.assertIn('overlap',report['held']['Haryana'])
            self.assertIn('overlap',report['held']['Rajasthan'])

    def test_partial_never_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);stage=root/'stage';stage.mkdir()
            doc=reconcile({1:decode_page(payload(1,2,3,2,[record(1),record(2)]))},'Haryana')
            (stage/'haryana.json').write_text(json.dumps(doc))
            result,selected=plan(stage,root)
            self.assertEqual(selected,{})
            self.assertIn('Haryana',result['held'])
    def test_existing_haryana_v7_not_publishable(self):
        p=Path('/mnt/data/CareHealth_Haryana_InnerScroll_v7_Result.json')
        if not p.exists():self.skipTest('External fixture not mounted')
        obj=json.loads(p.read_text())
        self.assertEqual(obj['metadata']['records_collected'],417)
        self.assertNotIn(21,obj['metadata']['pages_captured'])
        self.assertEqual(len(set(str(v['id']) for v in obj['hospitals'])),417)

if __name__=='__main__':unittest.main()

# Dedicated first-party locator fallback is exercised without networking.
import asyncio
from unittest.mock import AsyncMock, patch
from scripts.care_network import browser_helpers as bh

class BrowserLocatorFallback(unittest.IsolatedAsyncioTestCase):
    async def test_official_dedicated_fallback_is_used_only_if_home_field_missing(self):
        class Input:
            async def fill(self, value, **kwargs): self.value=value
            async def input_value(self): return self.value
            async def press(self, value, **kwargs): pass
        class Page:
            def __init__(self): self.urls=[]
            async def goto(self,url,**kwargs): self.urls.append(url)
            async def wait_for_load_state(self,*args,**kwargs): return None
        p=Page(); inp=Input(); log=[]
        with patch.object(bh,'modern_input',new=AsyncMock(side_effect=[None,inp])) as mi, \
             patch.object(bh,'click_search',new=AsyncMock(return_value=True)) as click:
            self.assertTrue(await bh.search_form(p,'Haryana',log.append))
            self.assertEqual(p.urls,[bh.FALLBACK_LOCATOR_URL])
            self.assertEqual(mi.await_count,2)
            self.assertEqual(inp.value,'Haryana')
            click.assert_awaited_once()
