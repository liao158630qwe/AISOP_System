"""Interactive algorithm tests; no industrial quality or grasp-force claims."""
from .hands import HAND_MODEL_FILENAME, HandLandmarker
from .skeleton import RELIABLE_VISIBILITY

NAMES = {'raise':'举手', 'grip':'握拳／抓握候选', 'objects':'通用目标识别'}


def raised_hands(people):
    found=[]
    for person in people:
        k=person['keypoints']
        for shoulder,wrist,side in [(5,9,'左'),(6,10,'右')]:
            if min(k[shoulder][2],k[wrist][2])>=RELIABLE_VISIBILITY and k[wrist][1]<k[shoulder][1]-.04:
                found.append({'person':person['trackId'],'side':side})
    return found


class AlgorithmLab:
    def __init__(self, model_dir):
        self.model_dir=model_dir
        self.detector=None
        self.hands=HandLandmarker(model_dir/HAND_MODEL_FILENAME)

    def run(self,frame,people,algorithms):
        checks=[];objects=[];hands=[]
        if 'raise' in algorithms:
            evidence=raised_hands(people)
            checks.append({'id':'raise','name':NAMES['raise'],'state':'DETECTED' if evidence else 'NOT_OBSERVED',
                           'detail':f'检出{len(evidence)}只举起的手臂；腕点高于肩点且关键点可靠'})
        if 'objects' in algorithms:
            path=self.model_dir/'yolo11n.pt'
            if not path.is_file():
                checks.append({'id':'objects','name':NAMES['objects'],'state':'UNAVAILABLE','detail':'缺少yolo11n.pt通用检测权重'})
            else:
                if self.detector is None:
                    from ultralytics import YOLO
                    self.detector=YOLO(str(path))
                from .gpu import require_cuda
                result=self.detector.predict(frame,imgsz=640,conf=.4,max_det=30,device=require_cuda(),verbose=False)[0]
                h,w=frame.shape[:2]
                for box,cls,score in zip(result.boxes.xyxy.tolist(),result.boxes.cls.tolist(),result.boxes.conf.tolist()):
                    objects.append({'label':self.detector.names[int(cls)],'confidence':round(score,3),
                                    'bbox':[round(x/s,5) for x,s in zip(box,[w,h,w,h])]})
                checks.append({'id':'objects','name':NAMES['objects'],'state':'DETECTED' if objects else 'NOT_OBSERVED',
                               'detail':f'COCO通用类别检出{len(objects)}个目标；不包含灯具专用零件'})
        if 'grip' in algorithms:
            try:
                hands=self.hands.detect(frame)
                checks.append({'id':'grip','name':NAMES['grip'],'state':'CANDIDATE' if any(h['shape']=='CLOSED' for h in hands) else 'NOT_OBSERVED',
                               'detail':f'检出{len(hands)}只手；21点骨架与指关节角度，不确认真实持物或抓牢'})
            except (ImportError,FileNotFoundError):
                checks.append({'id':'grip','name':NAMES['grip'],'state':'UNAVAILABLE','detail':'缺少MediaPipe或hand_landmarker.task'})
        return {'mode':'ALGORITHM_TEST','checks':checks,'objects':objects,'hands':hands,'verdict':'TEST_ONLY'}
