"""Star V8: preserve exact source-blank city, never infer and never weaken other gates."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_star as s
import validate_star as v


def row(i,state='Delhi'):
    return {'hospitalName':'Facility '+str(i),'address':str(i)+' Main Road',
            'cityName':'Delhi','stateName':state,'pinCode':'110001',
            'latitude':'28.6139','longitude':'77.2090',
            'excludedHospital':'NO','isVsp':'NO','ayushHospital':'NO'}


def make_fixture(zero_city=False,other_gap=False,excluded_blank=False):
    zeros={'Arunachal Pradesh','Ladakh','Lakshadweep','Mizoram','Nagaland'}
    pilot={k:{'count':350,'pages':35,'page1_rows':10,'page2_distinct':True}
           for k in ('delhi','new_delhi','maharashtra')}
    acceptance={st:{'status':'SOURCE_REPORTED_ZERO' if st in zeros else 'FILTER_ACCEPTED',
                    'accepted':[{'name':st,'count':0 if st in zeros else 200}]}
                for st in s.STATES}
    preflight={'accepted':acceptance,'unresolved':{},'accepted_states':36,'expected_states':36}
    def query(action,cat,state,out,page_size,delay):
        if cat=='Network' and state not in zeros:
            rr=[row(i,state) for i in range(200)]
            if state=='Himachal Pradesh':
                if zero_city:rr[12]['cityName']=''
                if other_gap:rr[13]['pinCode']=''
            rr=[s.make_record(x,cat,state) for x in rr]
        elif cat!='Network' and state=='Delhi':
            a=row(0,state)
            if cat=='nonpreferred' and excluded_blank:
                a=row(9000,state);a['cityName']=''
            if cat=='vsp':a['isVsp']='YES'
            rr=[s.make_record(a,cat,state)]
        else:rr=[]
        n=len(rr)
        return rr,{'count':n,'pages':(n+9)//10,'captured':n,'distinct':n,
            'status':'COMPLETE' if n else 'SOURCE_REPORTED_ZERO'}
    return preflight,pilot,query

class CityMissingWithoutInference(unittest.TestCase):
    def test_source_city_blank_stays_blank_with_geographic_evidence(self):
        a=row(1,'Himachal Pradesh');a['cityName']=''
        record=s.make_record(a,'Network','Himachal Pradesh')
        self.assertTrue(s.source_city_missing_safe(record))
        self.assertEqual(record['city'],'')
        self.assertEqual(record['source_identity_issues'],['missing_cityName'])

    def test_missing_city_without_geographic_evidence_still_rejected(self):
        a=row(1,'Himachal Pradesh');a['cityName']='';a['latitude']=''
        self.assertFalse(s.source_city_missing_safe(s.make_record(a,'Network','Himachal Pradesh')))

    def test_other_missing_field_never_allowed(self):
        for key in ('hospitalName','address','pinCode','stateName'):
            a=row(1);a['cityName']='';a[key]=''
            x=s.make_record(a,'Network','Delhi') if key!='stateName' else s.make_record(a,'Network','Delhi')
            self.assertFalse(s.source_city_missing_safe(x))

    def test_live_shape_four_city_missing_sources_as_supplied_by_star(self):
        data=[('Tara Hospital','Himachal Pradesh','171001','31.1048001','77.1746678'),
              ('Varma Hospital RECT','Kerala','682301','9.9419012','76.3467089'),
              ('VijayaKumar Menon Hospital','Kerala','682301','9.9528026','76.3409525'),
              ('Jyotirmaya Eye and Dental Hospital','Maharashtra','443112','20.3444295','76.2608621')]
        for i,(name,state,pin,lat,lon) in enumerate(data):
            a=row(i,state);a.update(hospitalName=name,cityName='',pinCode=pin,latitude=lat,longitude=lon)
            record=s.make_record(a,'Network',state)
            self.assertTrue(s.source_city_missing_safe(record),name)
            self.assertEqual(record['city'],'')

    def run_snapshot(self,source_city,other_gap=False,excluded_blank=False):
        pre,pilot,query=make_fixture(source_city,other_gap,excluded_blank)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)
            with (patch.object(s.base,'get_live_action_id',return_value=('a'*40,'https://www.starhealth.in/v.js')),
                  patch.object(s,'pilot',return_value=pilot),
                  patch.object(s,'verify_source_state_filters',return_value=pre),
                  patch.object(s,'collect_state_with_aliases',side_effect=query)):
                report=s.collect(path,delay=0)
            return report, {p.name:json.loads(p.read_text()) for p in path.glob('*.json')}

    def test_complete_216_with_source_city_missing_is_publishable(self):
        report,docs=self.run_snapshot(True)
        self.assertEqual(report['status'],'ALL_216_QUERIES_STAGED_VALIDATED',report['errors'])
        self.assertEqual(len(report['queries']),216)
        self.assertEqual(report['source_city_missing_count'],1)
        a=docs['star_health_candidate.json'];x=docs['star_excluded_candidate.json']
        self.assertTrue(v.validate(a,x,report))
        blank=[r for r in a['hospitals'] if not r['city']]
        self.assertEqual(len(blank),1)
        self.assertTrue(blank[0]['source_city_missing'])
        self.assertEqual(blank[0]['source_identity_issues'],['missing_cityName'])
        self.assertNotIn('source_review_row',blank[0])
        self.assertTrue(docs['star_identity_review.json']['publication_permitted'])
        a['metadata']['source_city_missing_count']=0
        with self.assertRaisesRegex(ValueError,'provenance totals'):
            v.validate(a,x,report)

    def test_missing_city_in_excluded_rows_does_not_inflate_public_ui_count(self):
        report,docs=self.run_snapshot(True,excluded_blank=True)
        self.assertEqual(report['status'],'ALL_216_QUERIES_STAGED_VALIDATED',report['errors'])
        a=docs['star_health_candidate.json'];x=docs['star_excluded_candidate.json']
        self.assertEqual(a['metadata']['source_city_missing_count'],2)
        self.assertEqual(a['metadata']['source_city_missing_eligible_count'],1)
        self.assertEqual(a['metadata']['source_city_missing_excluded_count'],1)
        self.assertTrue(v.validate(a,x,report))

    def test_additional_missing_pin_still_blocks_publication(self):
        report,docs=self.run_snapshot(True,True)
        self.assertEqual(report['status'],'PAUSED_SOURCE_DATA_QUALITY')
        self.assertNotIn('star_health_candidate.json',docs)
        self.assertFalse(docs['star_identity_review.json']['publication_permitted'])

    def test_do_not_trust_blank_city_without_flag(self):
        report,docs=self.run_snapshot(True)
        a=docs['star_health_candidate.json'];x=docs['star_excluded_candidate.json']
        blank=next(r for r in a['hospitals'] if not r['city'])
        del blank['source_city_missing']
        with self.assertRaisesRegex(ValueError,'provenance'):
            v.validate(a,x,report)

if __name__=='__main__':unittest.main()
