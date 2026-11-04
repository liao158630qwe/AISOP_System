"""Promote a measured candidate only after explicit non-regression checks."""
import argparse
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',default='yolo26m');args=parser.parse_args()
    out=ROOT/'outputs/screw-inspection-2026-10-17/next-models'/args.model
    comparison=json.loads((out/'comparison.json').read_text(encoding='utf-8'))
    baseline=comparison['summary']['yolo11s-v2'];candidate=comparison['summary'][args.model+'-v3']
    source=candidate['native'];previous=baseline['native']
    checks={
        'native_heads_not_worse':source['visible_heads_found']>=previous['visible_heads_found'],
        'native_wrong_not_worse':source['wrong']<=previous['wrong'],
        'native_product_removed_clear':all(c['heads']==0 for c in source['removed_product_controls']),
        'stress_heads_not_worse_in_total':sum(v['visible_heads_found'] for v in candidate.values())>=sum(v['visible_heads_found'] for v in baseline.values()),
        'stress_wrong_not_worse_in_total':sum(v['wrong'] for v in candidate.values())<=sum(v['wrong'] for v in baseline.values()),
        'core_p95_under_500ms':all(v['p95_ms']<500 for v in candidate.values()),
    }
    decision={'candidate':args.model,'checks':checks,'accepted':all(checks.values()),
        'scope':'development non-regression gate; independent field/defect validation still absent'}
    (out/'promotion.json').write_text(json.dumps(decision,indent=2),encoding='utf-8')
    print(json.dumps(decision),flush=True)
    if not decision['accepted']:raise SystemExit('Candidate was not promoted; keep the existing service model')
    config_path=ROOT/'config/screw-inspection.json'
    shutil.copy2(config_path,out/'config-before-upgrade.json')
    config=json.loads(config_path.read_text(encoding='utf-8'))
    config.update(version='screw-video-v3-'+args.model,model='models/screw-head-v3-'+args.model+'.pt',detector_input=320,
        training_result=(out/'training-result.json').relative_to(ROOT).as_posix())
    # Preserve conventional model scale spelling in the UI.
    config['model_name']='YOLO'+args.model.removeprefix('yolo')+' 螺丝增强 v3'
    config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
