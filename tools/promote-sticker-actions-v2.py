"""Promote the v2 action candidate only after both reviewed videos pass."""
import json
import shutil
import sys
from datetime import datetime,timezone
from pathlib import Path

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from algorithms.sticker_actions import StickerActionDetector,StickerActionSequence,action_compliance
from algorithms.video import file_hash

OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
CANDIDATE=ROOT/'config/sticker-action-candidate.json'
ACTIVE=ROOT/'config/sticker-inspection.json'
FRAMES={'original':OUT/'action-frames','new':OUT/'new-video-action-frames-v2'}
SAMPLES={'original':OUT/'action-samples.json','new':OUT/'new-video-action-samples-v2.json'}


def records(name):
    return [dict(folder=FRAMES[name],**r) for r in json.loads(SAMPLES[name].read_text(encoding='utf-8'))]


def replay(detector,cfg,items):
    sequence=StickerActionSequence(cfg['targets'],cfg['action'])
    for item in items:
        image=cv2.imread(str(item['folder']/item['file']))
        if image is None:raise SystemExit('missing action frame: '+item['file'])
        sequence.update(item['time'],detector.inspect(image))
    targets=sequence.aggregate(final=True);process=sequence.snapshot()
    return dict(targets=targets,sequence=process,compliance=action_compliance(targets,process,final=True))


def remap(items,start=0.0,step=.25):
    return [dict(item,time=start+index*step) for index,item in enumerate(items)]


def main():
    cfg=json.loads(CANDIDATE.read_text(encoding='utf-8'));training=json.loads((OUT/'action-training-result-v2.json').read_text(encoding='utf-8'))
    detector=StickerActionDetector({'config':cfg});original=records('original');new=records('new')
    eight_original=replay(detector,cfg,original);eight_new=replay(detector,cfg,new)
    four_original=replay(detector,cfg,original[::2]);four_new=replay(detector,cfg,new[::2])
    still_image=cv2.imread(str(FRAMES['new']/next(r['file'] for r in new if abs(r['time']-40.0)<.01)))
    still=StickerActionSequence(cfg['targets'],cfg['action'])
    for index in range(3):still.update(index*.25,detector.inspect(still_image))
    still_targets=still.aggregate(final=True);still_process=still.snapshot()
    still_decision=action_compliance(still_targets,still_process,final=True)
    blank=StickerActionSequence(cfg['targets'],cfg['action'])
    for index in range(3):blank.update(index*.25,detector.inspect(np.zeros((1920,1080,3),np.uint8)))
    blank_targets=blank.aggregate(final=True);blank_process=blank.snapshot()
    blank_decision=action_compliance(blank_targets,blank_process,final=True)
    clear=[r for r in new if 13.0<=r['time']<=13.75]
    r01=[r for r in new if 25.5<=r['time']<=27.5]
    t02=[r for r in new if 14.75<=r['time']<=16.0]
    t01=[r for r in new if 18.25<=r['time']<=19.0]
    wrong=replay(detector,cfg,remap(clear+r01+[next(r for r in new if abs(r['time']-28.0)<.01)]+t02+t01))
    expected={t['id'] for t in cfg['targets']}
    checks={
        'original_8fps_pass':eight_original['compliance']['status']=='PASS' and {e['id'] for e in eight_original['sequence']['events']}==expected,
        'new_8fps_pass':eight_new['compliance']['status']=='PASS' and {e['id'] for e in eight_new['sequence']['events']}==expected,
        'original_4fps_pass':four_original['compliance']['status']=='PASS' and {e['id'] for e in four_original['sequence']['events']}==expected,
        'new_4fps_pass':four_new['compliance']['status']=='PASS' and {e['id'] for e in four_new['sequence']['events']}==expected,
        'triangles_before_rectangles':all(r['sequence']['trianglesCompleteAt'] is not None and
            r['sequence']['trianglesCompleteAt']<r['sequence']['firstRectangleAt'] for r in [eight_original,eight_new,four_original,four_new]),
        'finished_product_without_actions_review':still_decision['status']=='REVIEW' and still_process['events']==[],
        'blank_review':blank_decision['status']=='REVIEW' and blank_process['events']==[],
        'rectangle_before_triangles_fails':wrong['compliance']['status']=='FAIL' and bool(wrong['sequence']['violations']),
        'model_unchanged':file_hash(ROOT/cfg['model'])==training['sha256'],
        'training_sources_unchanged':all(file_hash(Path(s['video']))==s['sha256'] for s in training['sources'])}
    accepted=all(checks.values())
    report=dict(accepted=accepted,checks=checks,eightFps=dict(original=eight_original,new=eight_new),
        fourFps=dict(original=four_original,new=four_new),still=dict(sequence=still_process,compliance=still_decision),
        blank=dict(sequence=blank_process,compliance=blank_decision),wrongOrder=dict(sequence=wrong['sequence'],compliance=wrong['compliance']),
        generatedAt=datetime.now(timezone.utc).isoformat(),
        scope='两条正确视频的 8FPS/4FPS 回放、成品静置、空白和错序检查；不是独立工位准确率')
    (OUT/'action-promotion-v2.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=accepted,checks=checks),ensure_ascii=False,indent=2))
    if not accepted:raise SystemExit('Sticker v2 promotion gates failed; active config was not changed')
    if ACTIVE.exists():shutil.copy2(ACTIVE,OUT/'config-before-v2-activation.json')
    temporary=ACTIVE.with_suffix('.json.tmp');temporary.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(ACTIVE)


if __name__=='__main__':main()
