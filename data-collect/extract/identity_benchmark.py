"""Evaluate locally labeled pairs; source fixtures belong in ignored gold/.

python -m extract.identity_benchmark --pairs gold/identity/pairs-v1.json [--fuzzy]
Labels describe identity (match or distinct); a review is an abstention.
"""
import argparse
import json
import math
from pathlib import Path
from . import identity


def evaluate(pairs, fuzzy=False):
    tp = fp = fn = reviews = 0
    results = []
    for pair in pairs:
        decision, reason = identity.compare(identity.prepare(pair['left']), identity.prepare(pair['right']), fuzzy)
        positive = pair['label'] == 'match'
        tp += decision == 'match' and positive
        fp += decision == 'match' and not positive
        fn += decision != 'match' and positive
        reviews += decision == 'review'
        results.append({'name': pair['name'], 'expected': pair['label'], 'actual': decision, 'reason': reason})
    n = tp + fp
    precision = tp / n if n else None
    interval = None
    if n:
        z = 1.96
        center = (precision + z*z/(2*n))/(1+z*z/n)
        half = z*math.sqrt(precision*(1-precision)/n+z*z/(4*n*n))/(1+z*z/n)
        interval = [max(0, center-half), min(1, center+half)]
    return {'ruleVersion': identity.VERSION, 'pairs': len(pairs), 'automaticMatches': n,
            'truePositives': tp, 'falsePositives': fp, 'missedMatches': fn, 'reviews': reviews,
            'precision': precision, 'recall': tp/(tp+fn) if tp+fn else None,
            'precisionWilson95': interval, 'results': results,
            'limitation': 'A small, selected benchmark cannot establish production-wide 99% precision.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pairs', required=True, type=Path)
    p.add_argument('--fuzzy', action='store_true')
    p.add_argument('--out', type=Path)
    args = p.parse_args()
    report = evaluate(json.loads(args.pairs.read_text())['pairs'], args.fuzzy)
    if args.out:
        identity.atomic_json(args.out, report)
    print(json.dumps(report, indent=2))
    return int(report['falsePositives'] > 0)

if __name__ == '__main__':
    raise SystemExit(main())
