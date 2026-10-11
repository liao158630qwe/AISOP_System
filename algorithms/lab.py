"""Interactive algorithm tests; no industrial quality or grasp-force claims."""
import math

NAMES = {'raise':'举手', 'grip':'握拳／抓握候选', 'objects':'通用目标识别'}


def raised_hands(people):
    found=[]
    for person in people:
        k=person['keypoints']
        for shoulder,wrist,side in [(5,9,'左'),(6,10,'右')]:
            if min(k[shoulder][2],k[wrist][2])>=.5 and k[wrist][1]<k[shoulder][1]-.04:
                found.append({'person':person['trackId'],'side':side})
    return found


def hand_shape(points):
    # Use joint angles instead of camera-axis direction, excluding uncertain thumbs.
    def angle(a,b,c):
        u=[a[i]-b[i] for i in range(3)];v=[c[i]-b[i] for i in range(3)]
        norm=math.sqrt(sum(x*x for x in u)*sum(x*x for x in v))
        if norm<1e-8:return None
        return math.degrees(math.acos(max(-1,min(1,sum(x*y for x,y in zip(u,v))/norm))))
    angles=[angle(points[m],points[p],points[t]) for m,p,t in [(5,6,8),(9,10,12),(13,14,16),(17,18,20)]]
    if any(a is None for a in angles):return 'UNKNOWN'
    folded=sum(a<125 for a in angles)
    return 'CLOSED' if folded>=3 else 'OPEN' if all(a>155 for a in angles) else 'PARTIAL'


class AlgorithmLab:
    def __init__(self, model_dir):
        self.model_dir=model_dir
        self.detector=None
        self.hands=None

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
                result=self.detector.predict(frame,imgsz=640,conf=.4,max_det=30,verbose=False)[0]
                h,w=frame.shape[:2]
                for box,cls,score in zip(result.boxes.xyxy.tolist(),result.boxes.cls.tolist(),result.boxes.conf.tolist()):
                    objects.append({'label':self.detector.names[int(cls)],'confidence':round(score,3),
                                    'bbox':[round(x/s,5) for x,s in zip(box,[w,h,w,h])]})
                checks.append({'id':'objects','name':NAMES['objects'],'state':'DETECTED' if objects else 'NOT_OBSERVED',
                               'detail':f'COCO通用类别检出{len(objects)}个目标；不包含灯具专用零件'})
        if 'grip' in algorithms:
            path=self.model_dir/'hand_landmarker.task'
            try:
                if not path.is_file():raise FileNotFoundError()
                import mediapipe as mp
                import cv2
                if self.hands is None:
                    self.hands=mp.tasks.vision.HandLandmarker.create_from_options(mp.tasks.vision.HandLandmarkerOptions(
                        base_options=mp.tasks.BaseOptions(model_asset_path=str(path)),num_hands=2,
                        min_hand_detection_confidence=.5,min_hand_presence_confidence=.5))
                result=self.hands.detect(mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)))
                for i,landmarks in enumerate(result.hand_landmarks):
                    normalized=[[p.x,p.y,p.z] for p in landmarks]
                    world=[[p.x,p.y,p.z] for p in result.hand_world_landmarks[i]]
                    shape=hand_shape(world)
                    hands.append({'points':normalized,'shape':shape,'label':{'CLOSED':'握拳／抓握候选','OPEN':'张手','PARTIAL':'部分弯曲','UNKNOWN':'手势不确定'}[shape]})
                checks.append({'id':'grip','name':NAMES['grip'],'state':'CANDIDATE' if any(h['shape']=='CLOSED' for h in hands) else 'NOT_OBSERVED',
                               'detail':f'检出{len(hands)}只手；21点骨架与指关节角度，不确认真实持物或抓牢'})
            except (ImportError,FileNotFoundError):
                checks.append({'id':'grip','name':NAMES['grip'],'state':'UNAVAILABLE','detail':'缺少MediaPipe或hand_landmarker.task'})
        return {'mode':'ALGORITHM_TEST','checks':checks,'objects':objects,'hands':hands,'verdict':'TEST_ONLY'}
