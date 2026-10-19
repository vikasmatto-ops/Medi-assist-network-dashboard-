"""Additional regression coverage for 19 Oct 2026 first-state failure."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import collect_star as s
import validate_star as v


def fake_row(n,state='Delhi'):
    return {'hospitalName':f'Test Hospital {n}','address':f'{n} Main St',
            'cityName':'Port Blair','stateName':state,'pinCode':'744101',
            'excludedHospital':'NO','isVsp':'NO','ayushHospital':'NO'}

class SafeStarV4(unittest.TestCase):
    def test_failed_server_action_status_is_visible_not_zero(self):
        with self.assertRaisesRegex(s.UnverifiedSourceStatus,"responseCode='404'.*description='No hospital found'"):
            s.parse_hospital_response('1:{"description":"No hospital found","responseCode":"404","count":"0","details":[]}',10)

    def test_official_andaman_spelling_can_be_resolved(self):
        self.assertEqual(s.source_state_key('Andaman and Nicobar Islands'),s.source_state_key('Andaman & Nicobar Island'))
        calls=[]
        def mock_query(action,cat,state,out,page_size,delay):
            calls.append(state)
            if len(calls)==1:raise s.UnverifiedSourceStatus('responseCode=404')
            record=s.make_record(fake_row(1,'Andaman & Nicobar Island'),cat,state)
            return [record],{'count':1,'pages':1,'captured':1,'distinct':1}
        with tempfile.TemporaryDirectory() as t, patch.object(s,'collect_query',side_effect=mock_query):
            rows,meta=s.collect_state_with_aliases('action','Network','Andaman and Nicobar Islands',Path(t),delay=0)
        self.assertEqual(len(rows),1)
        self.assertEqual(calls,['Andaman and Nicobar Islands','Andaman & Nicobar Island'])
        self.assertEqual(meta['status'],'COMPLETE')
        self.assertEqual(meta['source_state_filter'],'Andaman & Nicobar Island')

    def test_no_alias_success_is_unverified_not_zero(self):
        with tempfile.TemporaryDirectory() as t, patch.object(s,'collect_query',side_effect=s.UnverifiedSourceStatus('responseCode=404')):
            rows,meta=s.collect_state_with_aliases('a','Network','Andaman and Nicobar Islands',Path(t))
        self.assertEqual(rows,[])
        self.assertEqual(meta['status'],'UNVERIFIED_FILTER')
        self.assertGreater(len(meta['attempts']),1)

    def test_blank_nationwide_filter_never_reaches_transport(self):
        with patch.object(s.base,'fetch',side_effect=AssertionError('Network must not be called')):
            with self.assertRaisesRegex(s.SourceError,'requires hospital name, city or state'):
                s.source_action('action',s.base.make_args(),10)

    def test_validator_rejects_forged_national_counts(self):
        # Verify the new gate is mandatory, even if an old report otherwise says complete.
        source={'metadata':{'source_name':'Star Health','complete':True,
                            'policy_specific_cashless_eligibility_verified':False,
                            'source_count':0,'excluded_count':0,'query_count':216,
                            'expected_query_count':216,'states_queried':list(s.STATES),
                            'categories_queried':list(s.CATEGORIES),
                            'verified_page2_distinct':True,
                            'raw_records_per_category':{k:0 for k in s.CATEGORIES}},'hospitals':[]}
        excluded={'metadata':{'record_type':'EXCLUDED_ONLY','source_count':0},'hospitals':[]}
        report={'status':'ALL_216_QUERIES_STAGED_VALIDATED','complete':True,'queries':{
            cat+'|'+state:{'count':0,'captured':0,'distinct':0,'pages':0}
            for cat in s.CATEGORIES for state in s.STATES}}
        with self.assertRaises(ValueError):v.validate(source,excluded,report)

if __name__=='__main__':unittest.main()

class SplitTerritories(unittest.TestCase):
    def test_dadra_and_daman_collected_separately_not_silently_omitted(self):
        calls=[]
        def q(action,cat,state,out,page_size,delay):
            calls.append(state)
            if state in ('Dadra and Nagar Haveli and Daman and Diu','Dadra & Nagar Haveli and Daman & Diu'):
                raise s.UnverifiedSourceStatus('not recognised')
            if state=='Dadra & Nagar Haveli':
                row=s.make_record(fake_row(1,state=state),cat,state)
            elif state=='Daman & Diu':
                row=s.make_record(fake_row(2,state=state),cat,state)
            else:
                raise AssertionError('Unexpected alias '+state)
            return [row],{'count':1,'pages':1,'captured':1,'distinct':1}
        with tempfile.TemporaryDirectory() as t, patch.object(s,'collect_query',side_effect=q):
            rows,audit=s.collect_state_with_aliases('a','Network','Dadra and Nagar Haveli and Daman and Diu',Path(t))
        self.assertEqual(len(rows),2)
        self.assertEqual(audit['count'],2)
        self.assertEqual(audit['source_state_filter'],['Dadra & Nagar Haveli','Daman & Diu'])

    def test_success_zero_checks_known_aliases(self):
        calls=[]
        def q(action,cat,state,out,page_size,delay):
            calls.append(state)
            if len(calls)==1:return [],{'count':0,'pages':0,'captured':0,'distinct':0}
            row=s.make_record(fake_row(1,state='Andaman & Nicobar Island'),cat,state)
            return [row],{'count':1,'pages':1,'captured':1,'distinct':1}
        with tempfile.TemporaryDirectory() as t, patch.object(s,'collect_query',side_effect=q):
            rows,audit=s.collect_state_with_aliases('a','Network','Andaman and Nicobar Islands',Path(t))
        self.assertEqual(len(rows),1)
        self.assertEqual(audit['source_state_filter'],'Andaman & Nicobar Island')

class NationwideMandatoryGeography(unittest.TestCase):
    def test_preflight_never_sends_empty_geographic_filter(self):
        calls=[]
        def fake(action,category,state,page_no,per_page=10):
            self.assertTrue(state)
            calls.append(state)
            return [fake_row(1,state=state)],1,1
        with patch.object(s,'page',side_effect=fake):
            audit=s.verify_source_state_filters('a',delay=0)
        self.assertEqual(audit['accepted_states'],36)
        self.assertFalse(audit['unresolved'])
        self.assertGreaterEqual(len(calls),36)

    def test_invalid_state_filters_halt_without_publishing(self):
        def fake(action,category,state,page_no,per_page=10):
            if state.lower().startswith('andaman'):
                raise s.UnverifiedSourceStatus('Unknown source state')
            return [fake_row(1,state=state)],1,1
        with patch.object(s,'page',side_effect=fake):
            audit=s.verify_source_state_filters('a',delay=0)
        self.assertIn('Andaman and Nicobar Islands',audit['unresolved'])
        self.assertEqual(audit['accepted_states'],35)

    def test_no_national_blank_query_in_collection(self):
        def reject_empty(action,category,state,page_no,per_page=10):
            if not state:raise AssertionError('Invalid blank state request')
            return [fake_row(1,state=state)],1,1
        with patch.object(s,'page',side_effect=reject_empty):
            a=s.verify_source_state_filters('a',delay=0)
        self.assertFalse(a['unresolved'])

class SplitCoverageGuard(unittest.TestCase):
    def test_one_split_territory_cannot_pass_preflight(self):
        def fake(action,category,state,page_no,per_page=10):
            if state in ('Dadra & Nagar Haveli','Dadra and Nagar Haveli'):
                return [fake_row(1,state=state)],1,1
            if state.lower().startswith('dadra') or state.lower().startswith('daman'):
                raise s.UnverifiedSourceStatus('Unrecognised')
            return [fake_row(1,state=state)],1,1
        with patch.object(s,'page',side_effect=fake):
            audit=s.verify_source_state_filters('a',delay=0)
        self.assertIn('Dadra and Nagar Haveli and Daman and Diu',audit['unresolved'])
