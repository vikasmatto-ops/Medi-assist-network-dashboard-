"""Star volatile offset-pagination safeguards: retain source truth, never mix epochs."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_star as s


def row(i):
    return {'hospitalName':f'Clinic {i}','address':f'{i} Sample Street',
        'cityName':'Mumbai','stateName':'Maharashtra','pinCode':'400001',
        'excludedHospital':'NO','isVsp':'NO'}

class DriftingPagination(unittest.TestCase):
    def test_count_change_restarts_only_partition_and_captures_new_count(self):
        calls=[]
        def api(action,cat,state,p,size):
            calls.append(p)
            if calls.count(1)==1:
                return [row(i) for i in range(10)],30,3
            if calls.count(1)==1:raise AssertionError('count changed but not restarted')
            n=31
            return [row(i) for i in range((p-1)*10,min(p*10,n))],n,4
        # First page is counted once; first page2 is the drifting response.
        def live(action,cat,state,p,size):
            if p==1:
                calls.append('start')
                if calls.count('start')==1:return [row(i) for i in range(10)],30,3
                return [row(i) for i in range(10)],31,4
            calls.append(p)
            if calls.count('start')==1:return [row(i) for i in range(10,20)],31,4
            return [row(i) for i in range((p-1)*10,min(p*10,31))],31,4
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',side_effect=live),patch.object(s.time,'sleep'):
            out=Path(td)
            other=out/'checkpoints'/s.checkpoint_key('Network','Delhi')/'page_0002.json'
            other.parent.mkdir(parents=True);other.write_text('unrelated')
            rec,a=s.collect_query('a'*40,'Network','Maharashtra',out,page_size=10,delay=0)
            self.assertEqual((len(rec),a['count'],a['captured'],a['distinct']),(31,31,31,31))
            self.assertEqual(calls.count('start'),2)
            self.assertTrue(other.exists())
            target=out/'checkpoints'/s.checkpoint_key('Network','Maharashtra')
            self.assertEqual(len(list(target.glob('page_*.json'))),4)
            self.assertEqual(json.loads((target/'_meta.json').read_text())['count'],31)

    def test_continuous_churn_exhausts_bounded_retries_no_publication(self):
        i={'attempt':0}
        def api(action,cat,state,p,size):
            if p==1:
                i['attempt']+=1
                return [row(n) for n in range(10)],30+i['attempt'],4
            return [row(n) for n in range(10,20)],100+i['attempt'],11
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',side_effect=api),patch.object(s.time,'sleep'):
            with self.assertRaisesRegex(s.SourceError,'Partition unstable after 3 complete attempts'):
                s.collect_query('a'*40,'Network','Maharashtra',Path(td),delay=0)
            self.assertEqual(i['attempt'],s.PARTITION_MAX_ATTEMPTS)
            self.assertFalse(list((Path(td)/'checkpoints'/s.checkpoint_key('Network','Maharashtra')).glob('page_*.json')))

    def test_two_day_old_middle_page_is_refetched(self):
        calls=[]
        def api(action,cat,state,p,size):
            calls.append(p)
            return [row(i) for i in range((p-1)*10,p*10)],20,2
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',side_effect=api),patch.object(s.time,'sleep'):
            out=Path(td)
            s.collect_query('a'*40,'Network','Maharashtra',out,delay=0)
            old=out/'checkpoints'/s.checkpoint_key('Network','Maharashtra')/'page_0002.json'
            os.utime(old,(time.time()-3*86400,time.time()-3*86400))
            s.collect_query('a'*40,'Network','Maharashtra',out,delay=0)
            self.assertEqual(calls,[1,2,1,2])

    def test_large_partition_verifies_bookends_and_middle(self):
        calls=[]
        def api(action,cat,state,p,size):
            calls.append(p)
            return [row(i) for i in range((p-1)*10,p*10)],200,20
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',side_effect=api),patch.object(s.time,'sleep'):
            rec,a=s.collect_query('a'*40,'Network','Maharashtra',Path(td),delay=0)
            self.assertEqual(len(rec),200)
            self.assertEqual(calls[:20],list(range(1,21)))
            self.assertEqual(calls[20:],[1,5,10,15,20])

    def test_stable_total_but_middle_membership_changes_is_retried(self):
        state={'starts':0}
        def api(action,cat,st,p,size):
            if p==1:state['starts']+=1
            ids=list(range((p-1)*10,p*10))
            # First attempt drifts after page 20 was captured, despite same count.
            if p==10 and api.calls.get(10,0)==1:ids[0]=5000
            api.calls[p]=api.calls.get(p,0)+1
            return [row(i) for i in ids],200,20
        api.calls={}
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',side_effect=api),patch.object(s.time,'sleep'):
            rec,a=s.collect_query('a'*40,'Network','Maharashtra',Path(td),delay=0)
            self.assertEqual(len(rec),200)
            self.assertGreaterEqual(state['starts'],3) # first capture, first recheck, retry capture
            self.assertEqual(a['count'],200)

if __name__=='__main__':unittest.main()
