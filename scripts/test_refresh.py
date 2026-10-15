"""NetworkIQ nationwide refresh contract tests. No network calls."""
import json
import tempfile
import unittest
from pathlib import Path
from refresh_network import build, clean_one, safety_gate


def sample(n=25010):
    hospitals=[]
    for i in range(n):
        hospitals.append({'id':i+1,'name':'Hospital '+str(i+1),'address':str(i+1)+' Main Road',
                          'state':'State '+str(i%25),'city':'City '+str(i%140),
                          'district':'District '+str(i%30),'pinCode':'110001',
                          'associatedInsuranceCompanies':[{'shortName':'Insurer A','isPPN':i%2==0}],
                          'isPPN':i%3==0,'blacklisted':False})
    return {'isSuccess':True,'hospitals':hospitals}


class NationwideContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=sample()
        cls.built,cls.audit=build(cls.source,'2026-10-15T00:00:00Z')

    def test_every_state_and_city_included(self):
        m=self.built['metadata']
        self.assertEqual(m['scope'],'nationwide')
        self.assertEqual(m['network_source_count'],25010)
        self.assertEqual(m['published_hospital_count'],25010)
        self.assertEqual(m['state_count'],25)
        self.assertGreater(len(m['city_counts']),11)
        self.assertTrue(all(x.get('state') for x in self.built['hospitals']))

    def test_insurer_associations_and_ppn_retained(self):
        h=self.built['hospitals'][0]
        self.assertEqual(h['ins_count'],1)
        self.assertEqual(h['ins'],'Insurer A')
        self.assertIsInstance(h['ppn_insurers'],list)

    def test_blacklisted_only_excluded_from_active(self):
        src=sample()
        src['hospitals'][0]['blacklisted']=True
        records,audit=build(src,'2026-10-15T00:00:00Z')
        self.assertEqual(len(records['hospitals']),25009)
        self.assertEqual(audit['excluded_reasons']['blacklisted_in_source'],1)
        self.assertIsNone(audit['flagged_records'][0]['effective_date'])

    def test_no_city_filter_and_no_valid_pin_requirement(self):
        src=sample()
        src['hospitals'][0]['city']='Tiny Remote Town'
        src['hospitals'][0]['pinCode']=''
        records,_=build(src,'2026-10-15T00:00:00Z')
        self.assertTrue(any(x['city']=='Tiny Remote Town' for x in records['hospitals']))

    def test_migration_gate_compares_source_counts(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'old.json'
            p.write_text(json.dumps({'metadata':{'network_source_count':25010,'city_counts':{'Delhi':100}},'hospitals':[{}]*100}))
            safety_gate(self.built,p)
            newer=json.loads(json.dumps(self.built))
            newer['metadata']['network_source_count']=15000
            with self.assertRaises(RuntimeError): safety_gate(newer,p)

    def test_full_country_source_contract_rejects_small(self):
        with self.assertRaises(RuntimeError):build({'isSuccess':True,'hospitals':[{'id':1}]},'2026-10-15T00:00:00Z')

if __name__=='__main__': unittest.main()
