#!/usr/bin/env python3
"""Recompute a published edition from stored signals, offline. Default is preview."""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'data-collect'))
from extract.scoring import score_probabilities, RULES_VERSION
from extract.publish import to_event

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=Path, default=ROOT / 'events.json')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    doc = json.loads(args.events.read_text())
    changed = 0
    for event in doc['events']:
        published = to_event({**event, 'kind': 'event'})
        if published and 'localityTier' not in event:
            event['localityTier'] = published['localityTier']
        detail = event.get('scoring', {})
        if not detail.get('signals'):
            continue
        score = score_probabilities(detail['signals'])
        changed += score != event.get('score')
        event['score'] = detail['score'] = score
        detail['rulesVersion'] = RULES_VERSION
    if args.write:
        from jsonschema import Draft202012Validator
        Draft202012Validator(json.loads((ROOT / 'schemas/events.schema.json').read_text())).validate(doc)
        temp = args.events.with_suffix('.json.tmp')
        temp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n')
        temp.replace(args.events)
    print(f'{changed} event scores changed' + ('; written' if args.write else '; preview only'))

if __name__ == '__main__':
    main()
