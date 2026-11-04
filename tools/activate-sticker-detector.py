"""Publish only a measured sticker configuration; preserve every screw asset."""
import json
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.video import file_hash
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
data=json.loads((OUT/'candidate-evaluation.json').read_text(encoding='utf-8'))
candidate=ROOT/'config/sticker-inspection-candidate.json'
config=json.loads(candidate.read_text(encoding='utf-8'))
controls={c['case']:c for c in data['controls']}
checks=dict(full_known_video_pass=data['compliance']['status']=='PASS' and data['sequence']['status']=='PASS',
    all_eight_native_present=all(v=='PRESENT' for v in controls['frame-48']['states'].values()),
    all_eight_camera_sized_present=all(v=='PRESENT' for v in controls['camera-sized-jpeg82-final']['states'].values()),
    early_bare_not_called_pasted=all(v!='PRESENT' for v in controls['frame-16']['states'].values()),
    blank_clear=controls['blank']['boxes']==0,
    template_removed_clear=all(controls[k]['boxes']==0 for k in ['frame-52','frame-60']),
    model_unchanged=file_hash(ROOT/config['model'])==data['model']['assets'][config['model']],
    source_unchanged=config['source_sha256']==data['source_sha256'])
if data.get('synthetic_missing_controls'):
    checks['synthetic_all_sites_missing_recognized']=all(c['state']=='EMPTY' for c in data['synthetic_missing_controls'])
camera_path=OUT/'candidate-evaluation-camera.json'
if camera_path.exists():
    camera=json.loads(camera_path.read_text(encoding='utf-8'))
    checks['camera_order_pass']=camera['compliance']['status']=='PASS' and camera['model']['assets']==data['model']['assets']
decision=dict(accepted=all(checks.values()),checks=checks,scope='known-video development gates; independent defect and station validation still required')
(OUT/'promotion.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(decision),flush=True)
if not decision['accepted']:raise SystemExit('Sticker gates failed; candidate was not activated')
active=ROOT/'config/sticker-inspection.json'
if active.exists():shutil.copy2(active,OUT/'config-before-activation.json')
temporary=active.with_suffix('.json.tmp')
temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
temporary.replace(active)
