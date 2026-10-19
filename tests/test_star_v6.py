"""Real Star 2012 zero-result contract, geographic coverage, and publish-gate checks."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_star as s
import validate_star as v

EMPTY='1:{"description":"Provider Details Not Available","count":"0","details":[],"responseCode":"2012"}'
STATES_ZERO={'Arunachal Pradesh','Ladakh','Lakshadweep','Mizoram','Nagaland'}


def sample(i, state):
    return dict(hospitalName=f'Hospital {i}',address=f'{i} Main Road',cityName='Sample City',
                stateName=state,pinCode='110001',excludedHospital='NO',isVsp='NO',ayushHospital='NO')

class ObservedZeroResult(unittest.TestCase):
    def test_verified_2012_empty_shape(self):
        self.assertEqual(s.parse_hospital_response(EMPTY,10),([],0,0))
    def test_2012_requires_strict_shape(self):
        for overrides in (
            {'count':'1'}, {'details':[sample(1,'Delhi')]}, {'description':'unknown'},
            {'responseCode':'2013'}, {'count':None}, {'details':None}
        ):
            d={'description':'Provider Details Not Available','count':'0','details':[],'responseCode':'2012'}
            d.update(overrides)
            with self.subTest(overrides=overrides),self.assertRaises(s.SourceError):
                s.parse_hospital_response('1:'+json.dumps(d),10)
    def test_unknown_2012_with_error_desc_not_accepted(self):
        d={'description':'Provider Details Not Available','count':'0','details':[],
           'responseCode':'2012','errorDesc':'upstream problem'}
        with self.assertRaisesRegex(s.SourceError,'errorDesc'):
            s.parse_hospital_response('1:'+json.dumps(d),10)
    def test_missing_geographic_search_never_sent(self):
        with patch.object(s.base,'fetch') as call:
            with self.assertRaisesRegex(s.SourceError,'requires hospital name, city or state'):
                s.source_action('action',s.base.make_args(),10)
            call.assert_not_called()
    def test_zero_query_is_checkpoint_free_but_audited(self):
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',return_value=([],0,0)):
            rows,audit=s.collect_state_with_aliases('action','Network','Nagaland',Path(td),delay=0)
        self.assertEqual(rows,[])
        self.assertEqual(audit['status'],'SOURCE_REPORTED_ZERO')
        self.assertEqual(audit['pages'],0)
    def test_known_state_alias_wins_over_zero_first_spelling(self):
        def f(action,cat,state,out,page_size,delay):
            if state=='Andaman & Nicobar Islands':
                return [s.make_record(sample(1,state),cat,state)],{'count':1,'captured':1,'distinct':1,'pages':1}
            return [],{'count':0,'captured':0,'distinct':0,'pages':0}
        with tempfile.TemporaryDirectory() as td,patch.object(s,'collect_query',side_effect=f):
            rows,audit=s.collect_state_with_aliases('a','Network','Andaman and Nicobar Islands',Path(td),delay=0)
        self.assertEqual(len(rows),1)
        self.assertEqual(audit['source_state_filter'],'Andaman & Nicobar Islands')
        self.assertEqual(audit['status'],'COMPLETE')
    def test_preflight_source_zero_still_covers_36_queries(self):
        def mock_page(action,category,state,p,size):
            return ([],0,0) if state in STATES_ZERO else ([sample(1,state)],1,1)
        with patch.object(s,'page',side_effect=mock_page),patch.object(s.time,'sleep'):
            a=s.verify_source_state_filters('action',delay=0)
        self.assertEqual(a['accepted_states'],36)
        self.assertEqual(a['unresolved'],{})
        self.assertEqual({x for x,item in a['accepted'].items() if item['status']=='SOURCE_REPORTED_ZERO'},STATES_ZERO)
    def test_failure_state_not_interpreted_as_empty(self):
        def mock_page(action,category,state,p,size):
            if state=='Nagaland':raise s.UnverifiedSourceStatus('bad filter')
            return [sample(1,state)],1,1
        with patch.object(s,'page',side_effect=mock_page),patch.object(s.time,'sleep'):
            a=s.verify_source_state_filters('action',delay=0)
        self.assertIn('Nagaland',a['unresolved'])
        self.assertEqual(a['accepted_states'],35)

class IntegratedNationwide(unittest.TestCase):
    @staticmethod
    def make_full():
        pilot={k:{'count':350,'pages':35,'page1_rows':10,'page2_distinct':True}
               for k in ('delhi','new_delhi','maharashtra')}
        preflight={st:{'status':'SOURCE_REPORTED_ZERO' if st in STATES_ZERO else 'FILTER_ACCEPTED',
                       'accepted':[{'name':st,'count':0 if st in STATES_ZERO else 200}]}
                   for st in s.STATES}
        check={'accepted':preflight,'unresolved':{},'accepted_states':36,'expected_states':36}
        def query(action,cat,state,out,page_size,delay):
            if state in STATES_ZERO:
                return [],{'count':0,'captured':0,'distinct':0,'pages':0,'status':'SOURCE_REPORTED_ZERO'}
            if cat=='Network': rows=[s.make_record(sample(i,state),cat,state) for i in range(200)]
            elif state=='Delhi':
                row=sample(1,state)
                if cat=='vsp':row['isVsp']='YES'
                rows=[s.make_record(row,cat,state)]
            else:rows=[]
            n=len(rows)
            return rows,{'count':n,'captured':n,'distinct':n,'pages':(n+9)//10,
                          'status':'COMPLETE' if n else 'SOURCE_REPORTED_ZERO'}
        with tempfile.TemporaryDirectory() as td:
            with (patch.object(s.base,'get_live_action_id',return_value=('a'*40,'https://www.starhealth.in/file.js')),
                  patch.object(s,'pilot',return_value=pilot),patch.object(s,'verify_source_state_filters',return_value=check),
                  patch.object(s,'collect_state_with_aliases',side_effect=query)):
                report=s.collect(Path(td),delay=0)
            if report['status']!='ALL_216_QUERIES_STAGED_VALIDATED':
                raise AssertionError(json.dumps(report['errors']))
            source=json.loads((Path(td)/'star_health_candidate.json').read_text())
            excluded=json.loads((Path(td)/'star_excluded_candidate.json').read_text())
        return source,excluded,report
    def test_all_216_36_states_and_five_zeros_source_flagged(self):
        source,excluded,report=self.make_full()
        self.assertEqual(len(source['hospitals']),6199)
        self.assertEqual(len(excluded['hospitals']),1)
        self.assertEqual(report['source_zero_states'],sorted(STATES_ZERO) if False else [x for x in s.STATES if x in STATES_ZERO])
        self.assertEqual(len(report['queries']),216)
        self.assertGreaterEqual(len(report['source_zero_queries']),30)
        self.assertEqual(v.validate(source,excluded,report)['queries'],216)
    def test_audit_source_zero_tamper_fails(self):
        source,excluded,report=self.make_full()
        row=report['queries']['Network|Nagaland']
        row['pages']=1
        with self.assertRaisesRegex(ValueError,'source-reported-zero'):
            v.validate(source,excluded,report)
    def test_unaudited_zero_fails(self):
        source,excluded,report=self.make_full()
        source['metadata']['source_reported_zero_queries']=[]
        with self.assertRaisesRegex(ValueError,'provenance'):
            v.validate(source,excluded,report)
    def test_other_error_status_fails(self):
        source,excluded,report=self.make_full()
        report['queries']['Network|Nagaland']['status']='UNVERIFIED_FILTER'
        with self.assertRaisesRegex(ValueError,'Unverified'):
            v.validate(source,excluded,report)

if __name__=='__main__':unittest.main()
