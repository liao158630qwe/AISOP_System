"""Activate measured weights/calibrations, preserving the previous config."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import snapshot,verify
OUT=ROOT/'outputs/screw-adaptation-v4-2026-10-17'

comparison=json.loads((OUT/'comparison.json').read_text(encoding='utf-8'))
if not comparison['accepted']:raise SystemExit('Development gates failed; existing service configuration retained')
completion=json.loads((OUT/'completion-4fps.json').read_text(encoding='utf-8'))
if completion['compliance']['status']!='PASS':
    raise SystemExit('Final recording window still lacks evidence; retain the existing configuration')
previous=OUT/'activation.json'
before=json.loads(previous.read_text(encoding='utf-8'))['active'] if previous.exists() else json.loads((OUT/'config-before.json').read_text(encoding='utf-8'))
if snapshot()['config_hash']!=before['config_hash']:
    raise SystemExit('Active configuration changed during training; review before replacing it')
candidate=json.loads((OUT/'candidate.json').read_text(encoding='utf-8'));verify(candidate)
(ROOT/'config/screw-inspection.json').write_text(json.dumps(candidate['config'],ensure_ascii=False,indent=2),encoding='utf-8')
active=snapshot()
(OUT/'activation.json').write_text(json.dumps(dict(active=active,checks=comparison['checks'],
    baseline='outputs/screw-validation-lv-141529-2026-10-17',
    scope='Known-video development gates; original failed independent evaluation retained'),ensure_ascii=False,indent=2),encoding='utf-8')
print(active['config']['model_name'])
