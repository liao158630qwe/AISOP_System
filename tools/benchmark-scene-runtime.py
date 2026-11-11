"""Real CUDA comparison of scene-keyed and weight-keyed caches at capacity one.

Reconstructs the former cache policy; both sides use the current rule evaluator.
Excludes HTTP, database, JPEG decoding and concurrent station/training traffic.
"""
import argparse
from collections import OrderedDict
import gc
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import torch
from ultralytics import YOLO

from algorithms.gpu import require_cuda
from algorithms.scene_runtime import SceneDetector
from algorithms.video import file_hash
from backend.scene_realtime import load_model
from backend.scenes import SceneDefinition


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames', type=int, default=12)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--model', type=Path, default=ROOT/'models/screw-head-v1.pt')
    parser.add_argument('--image', type=Path, default=ROOT/'models/screw-reference.jpg')
    parser.add_argument('--output', type=Path, default=ROOT/'test-results/scene-runtime-benchmark.json')
    args = parser.parse_args()
    if args.frames < 2 or args.rounds < 1:
        parser.error('At least two frames and one round are required')
    device = require_cuda()
    model = YOLO(str(args.model))
    classes = {str(k):str(v) for k,v in model.names.items()}
    identity = str(uuid.uuid4())
    frame = cv2.imread(str(args.image))
    if frame is None:
        raise RuntimeError('Reference image cannot be decoded')
    frame = cv2.resize(frame, (640,360))
    scenes = []
    for i in range(2):
        config = SceneDefinition(name=f'camera-{i}',model_id=identity,status='published',imgsz=640,min_sharpness=0,
            rules=[dict(id='target',name='target',class_id=int(next(iter(classes))),threshold=.5+i*.1,
                        roi=[0,0,1,1] if i==0 else [.1,.1,.9,.9])]).model_dump(mode='json',exclude={'revision'})
        frozen = dict(scene_id=str(uuid.uuid4()),revision=1,config=config,model_path=str(args.model.resolve()),
            model=dict(id=identity,task=model.task,classes=classes,sha256=file_hash(args.model)))
        frozen['sha256'] = hashlib.sha256(json.dumps(frozen,sort_keys=True).encode()).hexdigest()
        scenes.append(frozen)
    # Initialize CUDA/kernels before measuring either policy. Each measured cache starts empty.
    SceneDetector(scenes[0],model=model).inspect(frame)
    torch.cuda.synchronize(device)
    del model
    gc.collect()
    torch.cuda.empty_cache()
    measurements = {'per_scene':[], 'shared_weights':[]}
    references = {}
    for round_index in range(args.rounds):
        policies = ['per_scene','shared_weights'] if round_index%2==0 else ['shared_weights','per_scene']
        for policy in policies:
            cache, durations, load_times, loads, hits = OrderedDict(), [], [], 0, 0
            torch.cuda.reset_peak_memory_stats(device)
            for i in range(args.frames):
                frozen = scenes[i%2]
                key = frozen['sha256'] if policy=='per_scene' else (frozen['model']['id'],frozen['model']['sha256'],device)
                torch.cuda.synchronize(device)
                started = time.perf_counter()
                if key not in cache:
                    if cache: cache.popitem(last=False)
                    loading = time.perf_counter()
                    cache[key] = SceneDetector(frozen) if policy=='per_scene' else load_model(frozen)
                    load_times.append((time.perf_counter()-loading)*1000)
                    loads += 1
                else:
                    hits += 1
                runtime = cache[key] if policy=='per_scene' else SceneDetector(frozen,model=cache[key])
                result = runtime.inspect(frame)
                torch.cuda.synchronize(device)
                durations.append((time.perf_counter()-started)*1000)
                checks = [(c['id'],c['state'],c['count']) for c in result['checks']]
                if i%2 in references and checks != references[i%2]:
                    raise AssertionError('Caching strategy changed scene decisions')
                references[i%2] = checks
                del runtime
            measurements[policy].append(dict(round=round_index+1,frames=args.frames,model_loads=loads,cache_hits=hits,
                total_ms=round(sum(durations),3),median_frame_ms=round(statistics.median(durations),3),
                first_frame_ms=round(durations[0],3),frame_ms=[round(x,3) for x in durations],
                load_ms=[round(x,3) for x in load_times],
                peak_allocated_mib=round(torch.cuda.max_memory_allocated(device)/1024**2,2)))
            cache.clear()
            gc.collect()
            torch.cuda.empty_cache()
    report = dict(gpu=torch.cuda.get_device_name(device),torch=torch.__version__,model=str(args.model),
        model_sha256=scenes[0]['model']['sha256'],resolution=[640,360],capacity=1,scenes=2,
        scope='Reconstructed cache-policy comparison with current rule evaluator; no HTTP/DB/JPEG or concurrent load.',
        decision_equivalence=True,measurements=measurements)
    report['summary'] = {policy:dict(model_loads_per_round=rows[0]['model_loads'],cache_hits_per_round=rows[0]['cache_hits'],
        median_frame_ms=round(statistics.median(v for row in rows for v in row['frame_ms']),3),
        median_total_ms=round(statistics.median(row['total_ms'] for row in rows),3)) for policy,rows in measurements.items()}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(args.output),gpu=report['gpu'],summary=report['summary']),ensure_ascii=False))


if __name__ == '__main__':
    main()
