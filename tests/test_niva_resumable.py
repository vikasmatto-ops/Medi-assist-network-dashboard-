"""Test Niva bounded checkpoint collection without requesting public source."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_niva_resumable as c

BASE=c.base.INITIAL

class FakeNiva:
    def __init__(self,total_pages=40,items_per_page=250):
        self.total_pages=total_pages
        self.per=items_per_page
        self.first_addition=0
        self.calls=[]
        self.count_override={}
    def fetch(self,url):
        n=1 if url==BASE else int(url.rsplit('=',1)[-1])
        self.calls.append(n)
        count=self.total_pages*self.per + self.count_override.get(n,0)
        rows=[]
        for i in range(self.per):
            rid=(n-1)*self.per+i + self.first_addition*(n==1)
            rows.append({'id':rid,'provider_id':str(rid),'name':'Hospital '+str(rid),
                         'provider_address':'Street '+str(rid),'provider_city':'Jaipur',
                         'provider_state':'Rajasthan','provider_pincode':'302001',
                         'is_ppn':False})
        nxt=BASE+'?page='+str(n+1) if n<self.total_pages else None
        return {'success':True,'results':rows,'links':{'next':nxt},
                'count':count,'total_pages':self.total_pages}

def fake_writer(output,rows,meta):
    path=output.parent/'NetworkIQ_Niva_DirectAPI_COMPLETE.zip'
    path.write_text('fake zip')
    return path

class ResumeAndPublish(unittest.TestCase):
    def test_short_sessions_resume_complete_without_repeating_old_pages(self):
        fake=FakeNiva()
        with tempfile.TemporaryDirectory() as td,patch.object(c.base,'write_result',side_effect=fake_writer):
            out=Path(td)/'niva'
            one=c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=5,progress=lambda s:None)
            self.assertFalse(one['complete']);self.assertEqual(one['captured_pages'],6)
            self.assertFalse((out.parent/'NetworkIQ_Niva_DirectAPI_COMPLETE.zip').exists())
            two=c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=15,progress=lambda s:None)
            self.assertFalse(two['complete']);self.assertEqual(two['captured_pages'],21)
            three=c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,progress=lambda s:None)
            self.assertTrue(three['complete']);self.assertEqual(three['captured_rows'],10000)
            self.assertTrue((out/'checkpoint'/'completed.json').is_file())
            self.assertFalse((out/'checkpoint'/'state.json').exists())
            self.assertEqual(fake.calls.count(1),4) # first checks + final distributed proof
            self.assertEqual(len(set(fake.calls)),40)
            new=c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=1,progress=lambda s:None)
            self.assertFalse(new['complete']);self.assertEqual(new['captured_pages'],2)
    def test_count_change_rejects_and_clears(self):
        fake=FakeNiva()
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=3,progress=lambda s:None)
            fake.count_override[5]=1
            with self.assertRaises(c.SourceEpochChanged):
                c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=2,progress=lambda s:None)
            self.assertFalse((out/'checkpoint'/'state.json').exists())
    def test_first_page_change_resets_bad_checkpoint(self):
        fake=FakeNiva()
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=6,progress=lambda s:None)
            fake.first_addition=200000
            r=c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=1,progress=lambda s:None)
            self.assertEqual(r['captured_pages'],2)
    def test_same_count_changed_page_prevents_publication(self):
        fake=FakeNiva()
        with tempfile.TemporaryDirectory() as td,patch.object(c.base,'write_result',side_effect=fake_writer):
            out=Path(td)
            orig=fake.fetch
            def flip_midway(url):
                result=orig(url)
                if fake.calls.count(1)>1 and url.endswith('?page=40'):
                    result['results'][0]['name']='Changed Hospital'
                return result
            with self.assertRaises(c.SourceEpochChanged):
                c.collect_session(out,seconds=100,delay=0,fetch=flip_midway,progress=lambda s:None)
            self.assertFalse((out.parent/'NetworkIQ_Niva_DirectAPI_COMPLETE.zip').exists())
    def test_stale_cache_is_invalidated(self):
        fake=FakeNiva()
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=2,progress=lambda s:None)
            path=out/'checkpoint'/'state.json'
            obj=json.loads(path.read_text());obj['started_utc']='2025-01-01T00:00:00+00:00'
            c.save(path,obj)
            r=c.collect_session(out,seconds=100,delay=0,fetch=fake.fetch,max_new_pages=1,progress=lambda s:None)
            self.assertEqual(r['captured_pages'],2)
    def test_only_explicit_safe_source_url(self):
        with self.assertRaises(c.base.SourceError):
            c.page_data('http://bad.example/api',1,fetch=lambda u: {'results':[]})

if __name__=='__main__':unittest.main()
