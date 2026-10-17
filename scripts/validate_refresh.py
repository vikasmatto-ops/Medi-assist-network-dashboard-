#!/usr/bin/env python3
"""Safely validate scheduled Niva refresh before overwriting last published version."""
from pathlib import Path
import argparse,json,shutil

def validate(old,new):
    om=old['metadata'];nm=new['metadata'];rs=new['hospitals']
    if nm.get('source_name')!='Niva Bupa' or not nm.get('complete') or nm.get('source_count')!=len(rs):
        raise ValueError('New Niva snapshot failed schema/record completeness')
    before=om['source_count'];after=len(rs)
    if not (0.85*before<=after<=1.15*before):
        raise ValueError(f'Niva source count changed unexpectedly: {before}->{after}; review required')
    if len({r['id'] for r in rs})!=after:
        raise ValueError('Duplicate IDs in refreshed snapshot')
    if nm['ppn_flags_count']!=sum(r['ppn'] for r in rs):
        raise ValueError('Niva PPN count mismatch')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('previous',type=Path);ap.add_argument('candidate',type=Path)
    args=ap.parse_args()
    old=json.loads(args.previous.read_text());new=json.loads(args.candidate.read_text())
    validate(old,new)
    shutil.copyfile(args.candidate,args.previous)
    print('Safe refresh published',len(new['hospitals']),'Niva listings')
if __name__=='__main__':main()
