"""Deterministic offline tests against simulated pagination and user-provided real first page."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from collect_indusind import check_page, collect, fingerprint, audit_records, PAGE_SIZE

class CollectorTests(unittest.TestCase):
    @staticmethod
    def rows_for(page,total=15229):
        n = min(PAGE_SIZE,total-(page-1)*PAGE_SIZE)
        return [{'hospitalId':None,'hospitalName':f'Hospital {(page-1)*PAGE_SIZE+i+1}', 'address':f'Address {page}-{i}', 'state':'HARYANA','city':None,'pinCode':'122001','latitude':0,'longitude':0,'type':'IPD','status':'1'} for i in range(n)]
    def fixture(self,page,tot=15229):
        return {'totalCount':tot,'items':self.rows_for(page,tot)}
    def test_real_user_searchlist_page_shape(self):
        p=Path('/mnt/data/IndusInd_Live_API_Result.zip')
        if not p.exists():self.skipTest('User supplied capture absent')
        with zipfile.ZipFile(p) as z: a=json.loads(z.read('indusind_searchlist_page1.json'))
        total,records=check_page(a,1)
        self.assertEqual((total,len(records)),(15229,200))
        self.assertTrue(all(r.get('hospitalId') is None for r in records))
    def test_valid_last_page(self):
        p=self.fixture(77)
        self.assertEqual(len(check_page(p,77)[1]),29)
    def test_reject_short_middle(self):
        p=self.fixture(8);p['items'].pop()
        with self.assertRaisesRegex(ValueError,'expected 200'):check_page(p,8)
    def test_reject_count_drift(self):
        with self.assertRaisesRegex(ValueError,'changed'):check_page(self.fixture(2,15230),2,15229)
    def test_full_capture_77_pages(self):
        with tempfile.TemporaryDirectory() as d:
            count=[]
            def f(page): count.append(page);return self.fixture(page)
            out=Path(d)/'result.zip';ok, audit=collect(Path(d)/'work',out,f,lambda x:None,0)
            self.assertTrue(ok)
            self.assertEqual(audit['totalCount_reported_by_API'],15229)
            self.assertEqual(audit['raw_records_collected'],15229)
            self.assertEqual(audit['pages_collected'],77)
            self.assertEqual(audit['unique_surrogate_fingerprints'],15229)
            self.assertEqual(count,list(range(1,78)))
            with zipfile.ZipFile(out) as z:
                p=json.loads(z.read('indusind_network_hospitals.json'))
                self.assertEqual(len(p['hospitals']),15229)
                self.assertFalse(p['metadata']['policy_cashless_verified'])
            count.clear()
            ok,_=collect(Path(d)/'work',out,f,lambda x:None,0)
            self.assertTrue(ok);self.assertEqual(count,[1])
    def test_stop_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d)/'work';out=Path(d)/'data.zip'
            def broken(page):
                if page==9:raise ValueError('source 500')
                return self.fixture(page)
            with self.assertRaisesRegex(ValueError,'source 500'):
                collect(work,out,broken,lambda x:None,0)
            self.assertFalse(out.exists());self.assertTrue((work/'pages'/'page_0008.json').exists())
            count=[]
            def success(page):count.append(page);return self.fixture(page)
            ok,_=collect(work,out,success,lambda x:None,0)
            self.assertTrue(ok);self.assertEqual(count,[1]+list(range(9,78)))
    def test_reject_page_repetition(self):
        with tempfile.TemporaryDirectory() as d:
            def f(page):
                if page==2:return self.fixture(1)
                return self.fixture(page)
            with self.assertRaisesRegex(ValueError,'duplicates another complete page'):
                collect(Path(d)/'work',Path(d)/'out.zip',f,lambda x:None,0)
    def test_no_fake_insurer_ids(self):
        r={'hospitalName':'Some hospital','address':'Address','state':'KARNATAKA','pinCode':'560001','hospitalId':None}
        self.assertEqual(fingerprint(r),fingerprint(dict(r,city='Bangalore')))
        report=audit_records([r,r],2,{'1':'xyz'})
        self.assertEqual(report['field_quality']['duplicate_fingerprint_surplus'],1)
        self.assertFalse(report['source_supplies_stable_hospital_ids'])

if __name__=='__main__':unittest.main(verbosity=2)
