"""Generic dataset audit, train, explicit test evaluation and constrained selection."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algorithms.training import audit_dataset, evaluate_model, load_recipe, select_candidate, train_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'train'):
        command = commands.add_parser(name)
        command.add_argument('--recipe', required=True)
        if name == 'train':
            command.add_argument('--output', required=True, help='New candidate directory; must not exist')
    command = commands.add_parser('evaluate')
    command.add_argument('--run', required=True)
    command.add_argument('--output', required=True)
    command = commands.add_parser('select')
    command.add_argument('--candidates', required=True)
    command.add_argument('--min-recall', required=True, type=float)
    command.add_argument('--max-false-accept', required=True, type=float)
    command.add_argument('--max-p95-ms', required=True, type=float)
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            recipe, _ = load_recipe(args.recipe)
            audit = audit_dataset(recipe['dataset'], recipe['task'])
            result = {key: value for key, value in audit.items() if key not in ('dataset', 'root')}
        elif args.command == 'train':
            result = train_model(args.recipe, args.output)
        elif args.command == 'evaluate':
            result = evaluate_model(args.run, args.output)
        else:
            candidates = json.loads(Path(args.candidates).read_text(encoding='utf-8'))
            result = select_candidate(candidates, min_recall=args.min_recall,
                max_false_accept=args.max_false_accept, max_p95_ms=args.max_p95_ms)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if result.get('status') == 'no_qualified_candidate' else 0
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    sys.exit(main())
