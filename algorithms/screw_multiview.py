"""Select a calibrated view using only features from the current frame."""
import copy
import time
import cv2
from .screw_refined import RefinedScrewDetector


class MultiViewScrewDetector:
    def __init__(self,frozen):
        self.frozen=frozen
        self.config=frozen['config']
        self.views=[]
        self.schema=self.config['targets']
        model=None
        for profile in [dict(id='original')]+self.config.get('calibrations',[]):
            local=copy.deepcopy(frozen)
            local['config'].update(profile)
            detector=RefinedScrewDetector(local,model=model)
            model=detector.model
            self.views.append((profile['id'],detector))
        self.model=model
        self.device=self.views[0][1].device
        self.reference=self.views[0][1].reference

    def inspect(self,image):
        started=time.perf_counter()
        first=self.views[0][1]
        small=cv2.resize(image,first.registration_size,interpolation=cv2.INTER_AREA)
        key,desc=first.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
        candidates=[]
        for identity,detector in self.views:
            registration=detector.localize(key,desc)
            if registration is not None:
                # Product feature support outranks features outside components.
                score=(sum(registration['componentInliers']),registration['inliers'])
                candidates.append((score,identity,detector,registration))
        for _,identity,detector,registration in sorted(candidates,key=lambda x:x[0],reverse=True):
            result=detector.inspect(image,registration)
            if result['registered']:
                # Retain field names only so panels keep their schema when a
                # product leaves. No observation, coordinates or boxes persist.
                self.schema=[{k:v for k,v in t.items() if k in ('id','name','preassembled')}
                             for t in result['targets']]
                result.update(calibrationId=identity,algorithm='multi-view-local-v1',
                              inferenceMs=round((time.perf_counter()-started)*1000,2))
                return result
        return dict(registered=False,inliers=0,algorithm='multi-view-local-v1',
            reason='当前帧与已标定工件视角均未可靠匹配，不能核对螺丝',
            targets=[dict(**t,state='UNKNOWN',score=None,bbox=None) for t in self.schema],
            inferenceMs=round((time.perf_counter()-started)*1000,2))
