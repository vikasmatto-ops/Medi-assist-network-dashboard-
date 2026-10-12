import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).parent))
from refresh_network import assign_city,clean_one,build,CITIES

class MappingTests(unittest.TestCase):
 def h(self,state,city,district,pin,address='Test hospital in area'):
  return {'id':123,'name':'Sample Hospital','address':address,'state':state,'city':city,'district':district,'pinCode':pin}
 def test_delhi_district_label(self):
  self.assertEqual(assign_city(self.h('Delhi','WEST','WEST','110015'))[0],'Delhi')
 def test_faridabad_palwal_not_included(self):
  self.assertIsNone(assign_city(self.h('Haryana','Palwal','Faridabad','121102'))[0])
 def test_gurugram_alias(self):
  self.assertEqual(assign_city(self.h('Haryana','GURUGRAM','GURUGRAM','122001'))[0],'Gurgaon')
 def test_noida_excludes_greater_noida(self):
  self.assertIsNone(assign_city(self.h('Uttar Pradesh','Gautam Buddha Nagar','Gautam Buddha Nagar','201310','Kasna Greater Noida'))[0])
 def test_noida_includes_sector(self):
  self.assertEqual(assign_city(self.h('Uttar Pradesh','Gautam Buddha Nagar','Gautam Buddha Nagar','201301','Noida sector 18'))[0],'Noida')
 def test_thane_not_navi_mumbai(self):
  self.assertIsNone(assign_city(self.h('Maharashtra','Thane','Thane','400601','Thane West'))[0])
 def test_vashi_is_navi_mumbai(self):
  self.assertEqual(assign_city(self.h('Maharashtra','Thane','Thane','400703','Navi Mumbai Vashi'))[0],'Navi Mumbai')
 def test_bad_pin_false_navi(self):
  self.assertIsNone(assign_city(self.h('Maharashtra','Nagpur','Nagpur','410206','Mahalaxmi Complex Nagpur'))[0])
 def test_hyderabad_metro(self):
  self.assertEqual(assign_city(self.h('Telangana','Medchal-Malkajgiri','Medchal-Malkajgiri','500092'))[0],'Hyderabad')
 def test_ahmedabad_not_gandhinagar(self):
  self.assertIsNone(assign_city(self.h('Gujarat','GANDHINAGAR','Gandhinagar','382006','Gandhinagar'))[0])
 def test_insurer_specific_ppn_preserved(self):
  h=self.h('Delhi','WEST','WEST','110001');h['associatedInsuranceCompanies']=[{'shortName':'NIC','isPPN':True},{'shortName':'Acko','isPPN':False}];h['isPPN']=False
  r=clean_one(h,'Delhi','test')
  self.assertFalse(r['ppn']);self.assertEqual(r['ppn_insurers'],['NIC'])
  self.assertEqual(r['ins_count'],2)

class DatasetTests(unittest.TestCase):
 def test_snapshot_contract(self):
  p=Path(__file__).parents[1]/'data/hospitals.json'
  obj=json.loads(p.read_text(encoding='utf-8'))
  self.assertEqual(set(obj['metadata']['city_counts']),set(CITIES))
  self.assertEqual(len(obj['hospitals']),obj['metadata']['selected_city_count'])
  self.assertEqual(len({x['id'] for x in obj['hospitals']}),len(obj['hospitals']))
  self.assertFalse(any(x['blacklisted'] for x in obj['hospitals']))
  self.assertTrue(all(x['city'] in CITIES for x in obj['hospitals']))

if __name__=='__main__':unittest.main()
