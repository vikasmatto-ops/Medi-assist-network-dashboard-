"""Offline tests; never masquerade as a successful live-source check."""
import json,sys,unittest,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import refresh_indusind as indus
import refresh_icici as icici
from care_network.quality import decode_page,reconcile
import refresh_care_merge as care

class RefreshGuards(unittest.TestCase):
    def test_indus_clinical_network_type_never_inferred(self):
        row={'hospitalName':'Example Hospital','address':'Valid road','type':'IPD','status':'1','state':'HARYANA','city':None,'pinCode':'122001'}
        x=indus.to_public(row,1)
        self.assertEqual((x['network_type'],x['city'],x['id'],x['ppn']),('IPD','','',False))
        with self.assertRaisesRegex(ValueError,'Unknown hospital network type'):
            indus.to_public(dict(row,type='unknown'),1)
        with self.assertRaisesRegex(ValueError,'Unexpected source network status'):
            indus.to_public(dict(row,status='2'),1)
    def test_indus_blocks_incomplete_and_dramatic_losses(self):
        one={'hospitalName':'Clinic','address':'Main Road','state':'HARYANA','type':'IPD','status':'1'}
        x={'hospitals':[one]*11000}
        with self.assertRaisesRegex(ValueError,'page pagination'):
            indus.build(x,{'totalCount_reported_by_API':11000,'pages_collected':50,'page_hashes':{}},{'hospitals':[{}]*12000})
    def test_icici_requires_all_six_verified_shards(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'1.json';a.write_text(json.dumps({'shard':0,'complete':False}))
            with self.assertRaisesRegex(ValueError,'Six complete'):
                icici.publish([a],Path(td)/'old.json',Path(td)/'new.json')
    def test_icici_refuses_zero_result_as_verified(self):
        from icici_nationwide_collector import check_records
        status,_=check_records([], 'Haryana','Gurgaon')
        self.assertEqual(status,'ZERO_UNVERIFIED')
    def test_care_requires_a_complete_non_anomalous_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old.json';out=root/'staging';out.mkdir()
            old.write_text(json.dumps({'metadata':{'source_name':'Care Health'},'hospitals':[{'id':i,'state':'Haryana'} for i in range(1200)]}))
            rows=[{'id':i,'hospital_name':f'Clinic {i}','address_line_1':'A Road','state':'Haryana','city':'Gurgaon','hospital_category':'NL_SMARTSELECT'} for i in range(100)]
            payload={'status':True,'statusCode':200,'response':{'pagination':{'currentPage':1,'totalPages':1,'totalRecords':100,'limit':100},'data':rows}}
            source=reconcile({1:decode_page(payload)},'Haryana')
            (out/'haryana.json').write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError,'No Care state fully validated'):
                care.merge(old,out)  # 200 -> 100 is held (source reduction >5%)
    def test_care_preserves_missing_city_and_code(self):
        r={'id':1,'hospital_name':'Clinic','address_line_1':'A Street','state':'Haryana','city':'','hospital_category':'NL_GRAMIN','services':['ENT']}
        x=care.publicize(r)
        self.assertEqual(x['hospital_category'],'NL_GRAMIN')
        self.assertEqual(x['city'],'')
        self.assertEqual(x['ppn'],False)
if __name__=='__main__':unittest.main()
