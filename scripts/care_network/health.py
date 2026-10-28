"""Signal incomplete scheduled captures after the artifacts have been preserved."""
import argparse,json,sys
from pathlib import Path

def check(root):
    reports=list(root.rglob('_run_summary.json'))
    if not reports:
        print('ERROR: No run summary artifacts. Browser capture likely failed before collection.');return 1
    failures=[];count=0
    for f in reports:
        try:
            data=json.loads(f.read_text(encoding='utf-8'))
            for state in data['results']:
                count+=1
                if not state.get('publishable'):
                    failures.append(f"{state['state']} ({state['observed']}/{state['expected']}, off-state={state['off_state']})")
        except Exception as e:failures.append(f'{f}: invalid summary {e}')
    print(f'Care source audit: {count} state queries, {len(failures)} requiring review')
    if failures:
        for f in failures:print('REVIEW NEEDED:',f)
        return 1
    return 0

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--staging-dir',type=Path,required=True)
    sys.exit(check(a.parse_args().staging_dir))
