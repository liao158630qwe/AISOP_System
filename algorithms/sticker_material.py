"""Site-local material features; surrounding installed patches are excluded."""
import cv2
import numpy as np


def features(pixels,mask):
    gray=cv2.cvtColor(pixels,cv2.COLOR_BGR2GRAY) if pixels.ndim==3 else pixels
    gray=cv2.GaussianBlur(gray,(3,3),.6).astype(np.float32)
    values=gray[mask]
    mean=float(values.mean());std=float(values.std())
    # Erased surroundings cannot reveal the progress of neighboring sites.
    filled=np.full_like(gray,mean);filled[mask]=gray[mask]
    normalized=(filled-mean)/(std+5.)
    return np.concatenate([cv2.resize(filled,(16,24)).ravel()/255.,
        cv2.resize(normalized,(16,24)).ravel(),np.histogram(values,bins=16,range=(0,256))[0]/max(1,len(values)),
        [mean/255.,std/128.]]).astype(np.float32)
