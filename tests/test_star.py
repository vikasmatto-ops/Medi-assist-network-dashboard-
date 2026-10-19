import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch as mockpatch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import collect_star as s
import validate_star as v
import patch_star_dashboard as ui

SAMPLES=json.loads((ROOT/'tests/fixtures/real_star_page1_20261019.json').read_text())

def record(n,state='Delhi',category='Network'):
    return {'hospitalName':f'Sample Clinic {n}','address':f'{n} Main Road',
            'cityName':'New Delhi','stateName':state,'pinCode':'110001','networkType':'ANH',
            'excludedHospital':'NO','ayushHospital':'NO','isVsp':'NO',
            'isPratham':'YES','smartNetwork':'YES'}

class Parser(unittest.TestCase):
    def test_three_real_first_page_flight_responses(self):
        expected=[350,348,3468]
        for src,count in zip(SAMPLES,expected):
            rows,reported,pages=s.parse_hospital_response(src['raw_response_prefix'],10)
            self.assertEqual(reported,count)
            self.assertEqual(pages,(count+9)//10)
            self.assertEqual(len(rows),10)
            self.assertTrue(rows[0]['hospitalName'])
            self.assertIn('excludedHospital',rows[0])
    def test_first_page_contains_source_flag_fields(self):
        rows,_,_=s.parse_hospital_response(SAMPLES[0]['raw_response_prefix'],10)
        q=s.make_record(rows[0],'Network','Delhi')
        self.assertIs(q['star_flags']['excluded'],False)
        self.assertTrue(q['star_flags']['pratham'])
        self.assertFalse(q['ppn'])
        self.assertIn('Network',q['star_categories'])
    def test_invalid_and_error_frames_refused(self):
        for raw in ('1:{"errorDesc":"bad search"}', '1:{"count":"10","details":[],"responseCode":"999"}', '1:{"count":"-1","details":[]}'):
            with self.assertRaises(s.SourceError):s.parse_hospital_response(raw,10)
    def test_categories_include_ayush_and_excluded(self):
        self.assertEqual(len(s.CATEGORIES),6)
        for cat in s.CATEGORIES:self.assertEqual(s.base.make_args(category=cat)[3],cat)
        x=s.make_record(record(1),'nonpreferred','Delhi')
        self.assertTrue(x['star_flags']['excluded'])
        self.assertTrue(s.make_record(record(2),'ayush','Delhi')['star_flags']['ayush'])
    def test_source_state_mismatch_rejected(self):
        with self.assertRaises(s.SourceError):s.make_record(record(1,state='Haryana'),'Network','Delhi')

class Pagination(unittest.TestCase):
    def test_page_two_must_be_distinct(self):
        r1=[record(i) for i in range(1,11)]
        r2=[record(i) for i in range(11,21)]
        def fake(action,cat,state,p,size):return (r1 if p==1 else r2),20,2
        with tempfile.TemporaryDirectory() as t:
            with mockpatch.object(s,'page',side_effect=fake):
                records,meta=s.collect_query('fakeaction','Network','Delhi',Path(t),delay=.0)
            self.assertEqual((len(records),meta['distinct'],meta['captured']),(20,20,20))
            self.assertEqual(len(list((Path(t)/'checkpoints').rglob('page_*.json'))),2)
    def test_repeated_page_refused(self):
        rows=[record(i) for i in range(10)]
        with tempfile.TemporaryDirectory() as t:
            with mockpatch.object(s,'page',return_value=(rows,20,2)):
                with self.assertRaisesRegex(s.SourceError,'Repeated'):
                    s.collect_query('fakeaction','Network','Delhi',Path(t),delay=0)
    def test_missing_last_page_refused(self):
        def fake(action,cat,state,p,size):return ([record(i) for i in range(10)] if p==1 else [record(11)]),20,2
        with tempfile.TemporaryDirectory() as t:
            with mockpatch.object(s,'page',side_effect=fake):
                with self.assertRaisesRegex(s.SourceError,'Truncated'):
                    s.collect_query('fakeaction','Network','Delhi',Path(t),delay=0)
    def test_resumed_checkpoint_does_not_skip_refetched_first_page(self):
        calls=[]
        def fake(action,cat,state,p,size):
            calls.append(p);return [record(i+(p-1)*10) for i in range(10)],20,2
        with tempfile.TemporaryDirectory() as t:
            with mockpatch.object(s,'page',side_effect=fake):
                s.collect_query('fakeaction','Network','Delhi',Path(t),delay=0)
                s.collect_query('fakeaction','Network','Delhi',Path(t),delay=0)
            self.assertEqual(calls,[1,2,1])
    def test_merge_keeps_exclusion_and_both_categories(self):
        a=s.make_record(record(1),'Network','Delhi')
        b=s.make_record(record(1),'nonpreferred','Delhi')
        s.merge_records(a,b)
        self.assertTrue(a['star_flags']['excluded'])
        self.assertEqual(set(a['star_categories']),{'Network','nonpreferred'})

class Interface(unittest.TestCase):
    def test_patch_requires_live_structure_and_never_replaces_data(self):
        with self.assertRaisesRegex(ValueError,'Unsupported'):
            ui.patch('<html><body>blank</body></html>')
    def test_star_ui_index_patch_idempotent(self):
        # Literal anchors copied from public dashboard as of 19 Oct. No other source changed.
        t='''<html><head></head><body>
        NETWORKIQ_NIVA_FIFTH_SOURCE_KPI_V1_20261017
        <select class="ss" id="q-source" onchange="syncCityOptions();buildKPI();applyF()">
          <option value="Niva Bupa">Niva Bupa</option>
        </select>
        <select class="ss" id="q-ins" onchange="applyF()">
          <option value="Niva Bupa">Niva Bupa</option>
        </select>
        <select class="ss" id="q-state" onchange="onStateChange()">
        function niqValidateNiva(source){
        function niqSourceFootnote(r){
        if(r.source==='Niva Bupa')return '<div class="niq-care-footnote">Niva Bupa API listing · Policy-specific eligibility not verified</div>';
        function niqKpiShortLabel(n, allIndia, st){
        if(source==='Niva Bupa')return (allIndia?'Niva Bupa':st)+' · '+n.toLocaleString('en-IN')+' PPN';
        var cardHtml='<div class="hcard'+((r.source==='Care Health'||r.source==='Niva Bupa')?' niq-care-card':'')+(r.source==='Niva Bupa'?' niq-niva-card':'')+'">'+
        (r.source==='Care Health'?niqCareBadges(r):'')+
        ((r.source==='IndusInd General Insurance'||r.source==='Niva Bupa')&&r.network_type?
        (insTxt&&r.source!=='Care Health'&&r.source!=='Niva Bupa'?
        function buildKPI(){
        function applyF(){
        if(source&&r.source!==source)return false;
        document.getElementById('q-ppn').checked=false;
        function networkiqRows(rows,source){
        function loadCurrentNetwork(){
        return null;
    })
  ]).then(function(parts){
    var medi=parts[0], icici=parts[1], indus=parts[2], care=parts[3], niva=parts[4];
        DATA=networkiqRows(medi.hospitals,'Medi Assist').concat(networkiqRows(icici.hospitals,'ICICI Lombard'),networkiqRows(indus.hospitals,'IndusInd General Insurance'),networkiqRows(care.hospitals,'Care Health'),(niva?networkiqRows(niva.hospitals,'Niva Bupa'):[]));
        +' These are source records, not unique hospitals. Verify current policy-specific cashless eligibility with insurer/TPA.';
        </body></html>'''
        p=ui.patch(t)
        self.assertEqual(ui.patch(p),p)
        self.assertIn("fetch('./data/star_health.json'",p)
        self.assertIn('niqValidateStar',p)
        self.assertIn('niq-star-category',p)
        self.assertIn('Star official directory',p)
        self.assertIn("networkiqRows(medi.hospitals,'Medi Assist')",p)
        self.assertEqual(p.count('<option value="Niva Bupa">Niva Bupa</option>'),2)
        self.assertEqual(p.count('<option value="Star Health" disabled>'),1)
        # Do not introduce Star as an insurer in the unrelated q-ins selector.
        ins=p.split('id="q-ins"',1)[1].split('</select>',1)[0]
        self.assertNotIn('Star Health',ins)

if __name__=='__main__':unittest.main()

class NationwideSimulated(unittest.TestCase):
    def test_all_216_partitions_require_complete_review_and_exclusions_separate(self):
        pilot_result={k:{'count':350,'pages':35,'page1_rows':10,'page2_rows':10,'page2_distinct':True}
                      for k in ('delhi','new_delhi','maharashtra')}
        def fake_query(action,cat,state,out,page_size,delay):
            n=200 if cat=='Network' else 0
            source_rows=[]
            if cat=='Network':
                for i in range(n):source_rows.append(s.make_record(record(i,state=state),'Network',state))
            if cat in ('agreed','value_network','ayush') and state=='Delhi':
                source_rows.append(s.make_record(record(2,state=state),cat,state))
            if cat=='vsp' and state=='Delhi':
                x=record(0,state=state);x['isVsp']='YES'
                source_rows.append(s.make_record(x,'vsp',state))
            if cat=='nonpreferred' and state=='Delhi':
                source_rows.append(s.make_record(record(1,state=state),'nonpreferred',state))
            n=len(source_rows)
            return source_rows,{'count':n,'pages':(n+9)//10,'captured':n,'distinct':n}
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)
            with (mockpatch.object(s.base,'get_live_action_id',return_value=('123'*14,'https://www.starhealth.in/current.js')),
                  mockpatch.object(s,'pilot',return_value=pilot_result),
                  mockpatch.object(s,'collect_query',side_effect=fake_query)):
                result=s.collect(path,delay=0,page_size=10)
            self.assertTrue(result['complete'],result['errors'])
            self.assertEqual(len(result['queries']),216)
            active=json.loads((path/'star_health_candidate.json').read_text())
            excluded=json.loads((path/'star_excluded_candidate.json').read_text())
            self.assertEqual(len(active['hospitals']),7199)
            self.assertEqual(len(excluded['hospitals']),1)
            self.assertTrue(v.validate(active,excluded,result))
            self.assertEqual(result['status'],'ALL_216_QUERIES_STAGED_VALIDATED')
