"""Promote the action detector only after measured action and no-action checks."""
import json
import shutil
import sys
from datetime import datetime, timezone
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


def run_samples(detector,cfg,records):
    sequence=StickerActionSequence(cfg['targets'],cfg['action'])
    for record in records:
        image=cv2.imread(str(OUT/'action-frames'/record['file']))
        if image is None:raise SystemExit('missing action frame: '+record['file'])
        sequence.update(record['time'],detector.inspect(image))
    return sequence


def decision(sequence):
    targets=sequence.aggregate(final=True)
    return targets,sequence.snapshot(),action_compliance(targets,sequence.snapshot(),final=True)


def main():
    cfg=json.loads(CANDIDATE.read_text(encoding='utf-8'))
    if cfg.get('mode')!='action':raise SystemExit('action candidate is not in action mode')
    detector=StickerActionDetector({'config':cfg})
    samples=json.loads((OUT/'action-samples.json').read_text(encoding='utf-8'))

    full=run_samples(detector,cfg,samples)
    targets,process,compliance=decision(full)
    events=process['events']
    single=[event for event in events if event.get('evidence')=='single-high-confidence']

    final_still=cv2.imread(str(OUT/'aligned/048.0.jpg'))
    still=StickerActionSequence(cfg['targets'],cfg['action'])
    for index in range(3):still.update(index*.25,detector.inspect(final_still))
    _,still_process,still_decision=decision(still)

    blank=StickerActionSequence(cfg['targets'],cfg['action'])
    empty=np.zeros((1920,1080,3),np.uint8)
    for index in range(3):blank.update(index*.25,detector.inspect(empty))
    _,blank_process,blank_decision=decision(blank)

    early=run_samples(detector,cfg,samples[:8])
    _,early_process,early_decision=decision(early)

    training=json.loads((OUT/'action-training-result.json').read_text(encoding='utf-8'))
    source=Path.home()/'Downloads'/cfg['source_video']
    checks={
        'full_known_video_pass':compliance['status']=='PASS' and process['status']=='PASS',
        'all_eight_actions_confirmed':len(events)==len(cfg['targets']) and all(t['state']=='ACTION_DONE' for t in targets),
        'triangles_before_rectangles':process['trianglesCompleteAt'] is not None and process['firstRectangleAt'] is not None
            and process['trianglesCompleteAt']<process['firstRectangleAt'],
        'single_high_confidence_path_observed':bool(single),
        'finished_product_without_actions_review':still_decision['status']=='REVIEW' and still_process['events']==[],
        'blank_review':blank_decision['status']=='REVIEW' and blank_process['events']==[],
        'early_clear_frames_have_no_actions':early_decision['status']=='REVIEW' and early_process['events']==[],
        'model_unchanged':file_hash(ROOT/cfg['model'])==training['sha256'],
        'source_unchanged':source.is_file() and file_hash(source)==cfg['source_sha256'],
    }
    accepted=all(checks.values())
    report=dict(accepted=accepted,checks=checks,sequence=process,compliance=compliance,
        still=dict(sequence=still_process,compliance=still_decision),
        blank=dict(sequence=blank_process,compliance=blank_decision),
        early=dict(sequence=early_process,compliance=early_decision),
        scope='known-video action development gates; independent station and operator validation still required',
        generatedAt=datetime.now(timezone.utc).isoformat())
    (OUT/'action-promotion.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=accepted,checks=checks,events=len(events),compliance=compliance['label']),ensure_ascii=False,indent=2))
    if not accepted:raise SystemExit('Action promotion gates failed; active config was not changed')
    if ACTIVE.exists():
        previous=json.loads(ACTIVE.read_text(encoding='utf-8'))
        if previous.get('mode')!='action':
            shutil.copy2(ACTIVE,OUT/'config-before-action-activation.json')
    temporary=ACTIVE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(ACTIVE)


if __name__=='__main__':main()
