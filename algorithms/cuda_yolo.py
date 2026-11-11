"""YOLO preprocessing on CUDA, preserving source coordinates without image downloads."""
import math
import torch
from torch.nn import functional as F
from . import cuda_vision as vision


def _predictor(task):
    from ultralytics.models.yolo.detect import DetectionPredictor
    from ultralytics.models.yolo.pose import PosePredictor
    base = PosePredictor if task == 'pose' else DetectionPredictor

    class CudaPredictor(base):
        cuda_images = True

        def postprocess(self, preds, img, orig_imgs, **kwargs):
            # Base postprocessing accepts a list and only needs shape to scale
            # boxes/keypoints. Keep the actual original RGB images on device.
            # These Results are for machine observations, never plot/save().
            originals=orig_imgs if isinstance(orig_imgs,list) else self.originals
            return super().postprocess(preds, img, originals, **kwargs)

    return CudaPredictor


@torch.inference_mode()
def predict(model, images, *, imgsz, **options):
    options.setdefault('quantize',16)
    event=getattr(model,'_cuda_completed',None)
    if event is not None:
        torch.cuda.current_stream().wait_event(event)
    preprocess_start,preprocess_end=(torch.cuda.Event(enable_timing=True) for _ in range(2))
    preprocess_start.record()
    if not isinstance(images, (list, tuple)):
        images = [images]
    originals = [vision.rgb(image) for image in images]
    shapes = [tuple(image.shape[-2:]) for image in originals]
    if len(set(shapes)) != 1:
        raise ValueError('CUDA inference batches require equal source dimensions')
    height, width = shapes[0]
    stride = 32
    size = math.ceil(int(imgsz) / stride) * stride
    gain = min(size / height, size / width)
    resized = (round(width * gain), round(height * gain))
    dw, dh = (size-resized[0]) % stride, (size-resized[1]) % stride
    left, right = round(dw/2-.1), round(dw/2+.1)
    top, bottom = round(dh/2-.1), round(dh/2+.1)
    batch=vision.resize(torch.stack(originals),resized)
    batch=F.pad(batch,(left,right,top,bottom),value=114).half().div_(255)
    preprocess_end.record()
    factory = _predictor(model.task)
    if not getattr(model.predictor, 'cuda_images', False):
        model.predictor = factory(overrides={**model.overrides, 'task':model.task, 'mode':'predict',
                                            'save':False, 'verbose':False, **options},
                                  _callbacks=model.callbacks)
        model.predictor.setup_model(model=model.model, verbose=False)
    model.predictor.originals = [image.permute(1,2,0) for image in originals]
    try:
        predictor=model.predictor
        with predictor._lock:
            for key in ('conf','iou','classes','max_det'):
                if key in options:setattr(predictor.args,key,options[key])
            if predictor.model.format=='pt' and predictor.model.end2end:
                predictor.model.model.set_head_attr(max_det=max(predictor.args.max_det,300),
                                                    agnostic_nms=predictor.args.agnostic_nms)
            predictor.batch=(['']*len(originals),None,None)
            from ultralytics.data.loaders import SourceTypes
            predictor.source_type=SourceTypes(tensor=True)
            if not predictor.done_warmup:
                predictor.model.warmup(im=batch)
                predictor.done_warmup=True
            # Ultralytics stream_inference's profiler synchronizes the entire
            # GPU before/after every stage. That serializes independent station
            # streams. Use its same inference/postprocess with stream events.
            start,network_end,end=(torch.cuda.Event(enable_timing=True) for _ in range(3))
            start.record()
            predictions=predictor.inference(batch)
            network_end.record()
            results=predictor.postprocess(predictions,batch,batch)
            end.record();end.synchronize()
            for result in results:
                result.speed={'preprocess':preprocess_start.elapsed_time(preprocess_end)/len(results),
                              'inference':start.elapsed_time(network_end)/len(results),
                              'postprocess':network_end.elapsed_time(end)/len(results)}
            return results
    finally:
        model._cuda_completed=torch.cuda.Event()
        model._cuda_completed.record()
        model.predictor.originals = []
