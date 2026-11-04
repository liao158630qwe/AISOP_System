import sys,json
from pathlib import Path
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.sticker_actions import StickerActionDetector,StickerActionSequence,action_compliance
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
cfg=json.loads((ROOT/'config/sticker-action-candidate.json').read_text(encoding='utf8'))
detector=StickerActionDetector({'config':cfg});sequence=StickerActionSequence(cfg['targets'],cfg['action']);rows=[]
for entry in json.loads((OUT/'action-samples.json').read_text()):
    im=cv2.imread(str(OUT/'action-frames'/entry['file']));inspection=detector.inspect(im)
    process=sequence.update(entry['time'],inspection);rows.append(dict(time=entry['time'],inspection=inspection,sequence=process))
targets=sequence.aggregate(final=True);result=dict(sequence=sequence.snapshot(),targets=targets,compliance=action_compliance(targets,sequence.snapshot(),final=True),frames=rows)
(OUT/'action-candidate-evaluation.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf8')
print(json.dumps(dict(sequence=result['sequence'],compliance=result['compliance']),ensure_ascii=False,indent=2))
