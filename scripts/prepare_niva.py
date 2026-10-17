#!/usr/bin/env python3
"""Convert verified official Niva Bupa API capture into the NetworkIQ public source schema.
No live network call and no mutation of the source ZIP. Fails closed on partial data.
"""
import argparse
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

SOURCE_NAME = 'Niva Bupa'
CANONICAL_STATES = ['Andaman and Nicobar Islands', 'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar',
 'Chandigarh', 'Chhattisgarh', 'Dadra and Nagar Haveli and Daman and Diu', 'Delhi', 'Goa', 'Gujarat',
 'Haryana', 'Himachal Pradesh', 'Jammu and Kashmir', 'Jharkhand', 'Karnataka', 'Kerala', 'Ladakh',
 'Lakshadweep', 'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland',
 'Odisha', 'Puducherry', 'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura',
 'Uttar Pradesh', 'Uttarakhand', 'West Bengal']
STATE_NAMES = {s.casefold(): s for s in CANONICAL_STATES}
STATE_NAMES.update({'orissa': 'Odisha', 'tamilnadu': 'Tamil Nadu'})

ENDPOINT = 'https://rules.nivabupa.com/api/v1/hospital_network/get_hospital_list/'


def public_record(r):
    speciality = str(r.get('provider_speciality') or '').strip().casefold()
    spec = {'multi': 'Multi-speciality', 'single': 'Single-speciality', 'allopathy': 'Allopathy'}.get(speciality, '')
    def val(field):
        v = r.get(field)
        return '' if v is None else str(v).strip()
    raw_state = val('provider_state')
    state = STATE_NAMES.get(raw_state.casefold(), 'State unverified')
    record = {
        'id': 'niva_' + val('id'),
        'reference_id': val('provider_id'),
        'name': val('name'),
        'address': val('provider_address'),
        'city': val('provider_city'),
        'state': state,
        'source_state': raw_state,
        'pin': val('provider_pincode'),
        'phone': val('provider_opd_number'),
        'spec': spec,
        'is_ppn': r.get('is_ppn') is True,
        'ppn': r.get('is_ppn') is True,
        'ppn_insurers': ['Niva Bupa'] if r.get('is_ppn') is True else [],
        'network_type': val('network_stream'),
        'ins': 'Niva Bupa',
        'ins_count': 1,
    }
    # Keep these only as non-essential source fields; never convert 0/blank into a fake rating.
    try:
        rating = float(r.get('provider_avgrating') or 0)
        if 0 < rating <= 5:
            record['rating'] = rating
    except (TypeError, ValueError):
        pass
    return record


def prepare(source):
    meta, rows = source.get('metadata', {}), source.get('hospitals')
    if not isinstance(rows, list) or meta.get('source_name') != SOURCE_NAME:
        raise ValueError('Not the expected Niva Bupa official JSON snapshot')
    if meta.get('source_endpoint') != ENDPOINT:
        raise ValueError('Unexpected source endpoint')
    if meta.get('complete') is not True or meta.get('verification') != 'All provider pages and IDs reconciled':
        raise ValueError('Collector did not confirm complete pagination')
    if meta.get('policy_specific_cashless_eligibility_verified') is not False:
        raise ValueError('Unexpected policy eligibility representation')
    if meta.get('source_count') != len(rows) or meta.get('records_captured') != len(rows):
        raise ValueError('Source count does not equal captured record count')
    if meta.get('source_pages') != meta.get('pages_captured') or not isinstance(meta.get('pages'), list) or len(meta['pages']) != meta.get('source_pages'):
        raise ValueError('Pagination not fully captured')
    if sum(p.get('row_count', -1) for p in meta['pages']) != len(rows):
        raise ValueError('Sum of page counts disagrees with record count')
    if len(rows) < 10000:
        raise ValueError('Too few rows for this nationwide candidate snapshot')
    ids = [str(r.get('id', '')).strip() for r in rows]
    provider_ids = [str(r.get('provider_id', '')).strip() for r in rows]
    if not all(ids) or len(set(ids)) != len(ids) or not all(provider_ids) or len(set(provider_ids)) != len(rows):
        raise ValueError('Duplicate or missing hospital/provider IDs')
    required = ('name','provider_address','provider_city','provider_state','provider_pincode')
    for field in required:
        bad = sum(not str(r.get(field) or '').strip() for r in rows)
        if bad:
            raise ValueError(f'{bad} missing values in required field: {field}')
    pins = [str(r['provider_pincode']).strip() for r in rows]
    if any(not re.fullmatch(r'\d{6}', p) for p in pins):
        raise ValueError('Invalid hospital PIN codes present')
    if any(type(r.get('is_ppn')) is not bool for r in rows):
        raise ValueError('Niva is_ppn contains unknown values; do not reinterpret')
    records = [public_record(r) for r in rows]
    counts = Counter(r['state'] for r in records)
    niva = {
        'metadata': {
            'source_name': SOURCE_NAME,
            'source_endpoint': ENDPOINT,
            'captured_utc': meta['captured_utc'],
            'source_count': len(records),
            'source_pages': meta['source_pages'],
            'pages_captured': meta['pages_captured'],
            'complete': True,
            'network_scope': 'API query scope (nationwide coverage not independently verified)',
            'policy_specific_cashless_eligibility_verified': False,
            'ppn_flags_count': sum(r['ppn'] for r in records),
            'record_type': 'Official Niva Bupa public hospital-network API listings',
            'not_unique_across_other_sources': True,
        },
        'hospitals': records,
    }
    audit = { 'record_count': len(records), 'unique_ids': len(set(ids)), 'unique_provider_ids': len(set(provider_ids)),
              'states': dict(sorted(counts.items())), 'ppn_count': niva['metadata']['ppn_flags_count'],
              'source_pages': meta['source_pages'], 'scope_note': niva['metadata']['network_scope'],
              'unverified_states': dict(Counter(r['source_state'] for r in records if r['state'] == 'State unverified'))}
    return niva, audit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('zip_path', type=Path)
    ap.add_argument('output', type=Path)
    ap.add_argument('--audit', type=Path)
    args=ap.parse_args()
    with zipfile.ZipFile(args.zip_path) as z:
        raw=json.loads(z.read('niva_hospitals.json'))
    data,audit=prepare(raw)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    if args.audit:
        args.audit.write_text(json.dumps(audit,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(audit,ensure_ascii=False))

if __name__=='__main__':main()
