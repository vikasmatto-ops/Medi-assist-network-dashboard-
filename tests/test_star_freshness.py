"""Live-source check fails on count, first-page, or provenance mismatch."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import star_live_freshness as f
import collect_star as s

class StarFreshnessRegression(unittest.TestCase):
    def test_same_count_and_first_page_matches(self):
        self._case('match')

    def test_changed_count_blocks(self):
        self._case('count')

    def test_same_count_changed_first_page_blocks(self):
        self._case('firstpage')

    def test_missing_checkpoints_block(self):
        self._case('missing')

    def test_incomplete_report_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(f.FreshnessMismatch):
                f.check({'status':'RUNNING','complete':False},Path(td),'action',delay=0)

    def _case(self, what):
        row={'hospitalName':'A','address':'Street','cityName':'City','stateName':'Delhi','pinCode':'110001'}
        queries={f'Network|State {i}':{'count':1,'source_state_filter':f'State {i}'} for i in range(216)}
        audit={'status':'ALL_216_QUERIES_STAGED_VALIDATED','complete':True,'queries':queries}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for i in range(216):
                p=root/'checkpoints'/s.checkpoint_key('Network',f'State {i}')/'page_0001.json'
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(json.dumps({'page':1,'count':1,'rows':[row]}),encoding='utf-8')
            if what=='missing':
                (root/'checkpoints'/s.checkpoint_key('Network','State 0')/'page_0001.json').unlink()
            def source(a,cat,state,page,size):
                if what=='count' and state=='State 0':
                    return [row,dict(row,hospitalName='B')],2,1
                if what=='firstpage' and state=='State 0':
                    return [dict(row,hospitalName='B')],1,1
                return [row],1,1
            res=f.check(audit,root,'action',delay=0,page_reader=source)
            self.assertEqual(res['complete_queries'],216)
            self.assertEqual(res['matching'],what=='match')
            self.assertEqual(bool(res['mismatches']),what!='match')
