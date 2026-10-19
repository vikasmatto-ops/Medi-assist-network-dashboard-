"""Regression for actual October 19 Himachal source-data quality failure.
Never omit malformed public records or report source totals as published rows.
"""
from pathlib import Path
import json, tempfile, sys, unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_star as s


def hospital(i,state='Himachal Pradesh'):
    return {'hospitalName':f'Hospital {i}','address':f'Road {i}',
            'cityName':'Shimla','stateName':state,'pinCode':'171001',
            'excludedHospital':'NO','isVsp':'NO','ayushHospital':'NO'}

class HimachalDataQuality(unittest.TestCase):
    def test_missing_pin_or_city_stays_raw_not_invented(self):
        for field in ('pinCode','cityName','address','hospitalName','stateName'):
            with self.subTest(field=field):
                row=hospital(1);row[field]=''
                self.assertTrue(s.raw_identity(row).startswith('q_'))
                entry=s.make_record(row,'Network','Himachal Pradesh')
                self.assertIn('missing_'+field,entry['source_identity_issues'])
                self.assertEqual(entry['source_review_row'],row)
                self.assertEqual(entry['source_filter_for_review'],'Himachal Pradesh')
                self.assertEqual(entry[{'pinCode':'pin','cityName':'city','hospitalName':'name',
                    'address':'address','stateName':'state'}[field]],'')
                self.assertFalse(entry['ppn'])

    def test_unknown_exclusion_flag_is_quarantined(self):
        row=hospital(9)
        row.pop('excludedHospital')
        record=s.make_record(row,'Network','Himachal Pradesh')
        self.assertIn('missing_or_invalid_exclusion_flag',record['source_identity_issues'])
        self.assertFalse(record['ppn'])

    def test_source_state_mismatch_never_assigned_to_filter(self):
        row=hospital(1,'Haryana')
        with self.assertRaisesRegex(s.SourceError,'state mismatch'):
            s.make_record(row,'Network','Himachal Pradesh')

    def test_59_rows_source_count_with_missing_one_fully_counted(self):
        rows=[hospital(i) for i in range(59)]
        rows[12]['pinCode']=''
        def fake_page(action,cat,state,page,size):
            a=(page-1)*size
            return rows[a:a+size],59,6
        with tempfile.TemporaryDirectory() as td,patch.object(s,'page',side_effect=fake_page):
            found,audit=s.collect_query('a'*40,'Network','Himachal Pradesh',Path(td),delay=0)
            self.assertEqual((audit['count'],audit['captured'],audit['distinct']),(59,59,59))
            self.assertEqual(len(found),59)
            self.assertEqual(sum(bool(r.get('source_identity_issues')) for r in found),1)
            self.assertEqual(len(list((Path(td)/'checkpoints').rglob('page_*.json'))),6)

    def test_quarantine_blocks_publication_after_complete_216_partition_audit(self):
        source_zero={'Arunachal Pradesh','Ladakh','Lakshadweep','Mizoram','Nagaland'}
        pilot={k:{'count':350,'pages':35,'page1_rows':10,'page2_distinct':True}
               for k in ('delhi','new_delhi','maharashtra')}
        acceptance={st:{'status':'SOURCE_REPORTED_ZERO' if st in source_zero else 'FILTER_ACCEPTED',
                        'accepted':[{'name':st,'count':0 if st in source_zero else 200}]}
                    for st in s.STATES}
        preflight={'accepted':acceptance,'unresolved':{},'accepted_states':36,'expected_states':36}
        def query(action,cat,state,out,page_size,delay):
            if cat=='Network' and state not in source_zero:
                rows=[hospital(i,state) for i in range(200)]
                if state=='Himachal Pradesh':rows[12]['pinCode']=''
                rows=[s.make_record(x,cat,state) for x in rows]
            elif cat!='Network' and state=='Delhi':
                x=hospital(0,state)
                if cat=='vsp':x['isVsp']='YES'
                rows=[s.make_record(x,cat,state)]
            else:rows=[]
            n=len(rows)
            return rows,{'count':n,'pages':(n+9)//10,'captured':n,'distinct':n,
                'status':'COMPLETE' if n else 'SOURCE_REPORTED_ZERO'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with (patch.object(s.base,'get_live_action_id',return_value=('a'*40,'https://www.starhealth.in/first.js')),
                  patch.object(s,'pilot',return_value=pilot),
                  patch.object(s,'verify_source_state_filters',return_value=preflight),
                  patch.object(s,'collect_state_with_aliases',side_effect=query)):
                report=s.collect(root,delay=0)
            self.assertEqual(report['status'],'PAUSED_SOURCE_DATA_QUALITY',report.get('errors'))
            self.assertFalse(report['complete'])
            self.assertEqual(len(report['queries']),216)
            self.assertEqual(report['source_identity_review_count'],1)
            self.assertFalse((root/'star_health_candidate.json').exists())
            self.assertFalse((root/'star_excluded_candidate.json').exists())
            quality=json.loads((root/'star_identity_review.json').read_text())
            self.assertEqual(quality['source_record_issues'][0]['public_source_row']['pinCode'],'')
            self.assertFalse(quality['publication_permitted'])

if __name__=='__main__':unittest.main()
