"""Replay a JSON process trace against a versioned spec, without device side effects."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from algorithms.process_rules import evaluate

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('input',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    payload=json.loads(args.input.read_text(encoding='utf-8'))
    result=evaluate(**payload)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(result['verdict'])
