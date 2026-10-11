"""Bounded offline pose analysis, reused by the CLI and server worker."""
import csv
import json
import platform
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import psutil

from .pose import COLORS, EDGES, LABELS
from .report import build_report
from .tracking import PoseTracker
from .action_sop import ActionSequence
from .video import AnalysisError, Cancelled, file_hash, probe, validate_options


def summarize(frames):
    segments, track_data, current = [], {}, {}
    for frame in frames:
        for p in frame["people"]:
            tid = p["trackId"]
            tr = track_data.setdefault(tid, dict(id=tid, firstSeen=frame["time"], visibleSeconds=0, confidences=[], segments=0))
            tr["lastSeen"] = frame["time"]
            tr["visibleSeconds"] += frame["end"] - frame["time"]
            tr["confidences"].append(p["confidence"])
            prev = current.get(tid)
            if prev and prev["action"] == p["action"] and abs(prev["end"] - frame["time"]) < .0001:
                prev["end"] = frame["end"]
                prev["scores"].append(p["confidence"])
            else:
                prev = dict(trackId=tid, start=frame["time"], end=frame["end"], action=p["action"],
                            label=p["label"], evidence=p["evidence"], scores=[p["confidence"]])
                segments.append(prev)
                current[tid] = prev
                tr["segments"] += 1
    for tr in track_data.values():
        tr["meanConfidence"] = float(np.mean(tr.pop("confidences")))
    segments.sort(key=lambda s: (s["start"], s["trackId"]))
    sums = defaultdict(float)
    for i, seg in enumerate(segments):
        seg.update(id=i + 1, duration=round(seg["end"] - seg["start"], 4), meanConfidence=float(np.mean(seg.pop("scores"))))
        sums[seg["action"]] += seg["duration"]
    return segments, list(track_data.values()), sums


def annotate(img, people):
    h, w = img.shape[:2]
    for p in people:
        box = (np.array(p["bbox"]) * [w, h, w, h]).astype(int)
        cv2.rectangle(img, tuple(box[:2]), tuple(box[2:]), (235, 225, 45), 2)
        cv2.putText(img, f'P{p["trackId"]:02}', (box[0], max(18, box[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, .6, (235, 225, 45), 2)
        k = np.array(p["keypoints"]) * [w, h, 1]
        for a, b in EDGES:
            if min(k[a, 2], k[b, 2]) >= .35:
                cv2.line(img, tuple(k[a, :2].astype(int)), tuple(k[b, :2].astype(int)), (235, 225, 45), 2)
    return img


def _model_descriptor(model_path, model_meta, device, torch, sample_fps, imgsz):
    """Model section of the result; external backends supply their own metadata."""
    common = dict(torchVersion=torch.__version__, sampleFps=sample_fps, detectorThreshold=.35,
        keypointThreshold=.35,
        actionMethod="轨迹内自适应关键点平滑＋尺度归一化多动作证据＋稳健位移＋带滞回的按秒因果解码；非工序分类器",
        trackingMethod="运动预测＋骨架相似度的全局匹配；关联不确定时拒绝累计步骤",
        temporalModel="causal-duration-v3")
    if model_meta:
        info = dict(name=model_meta["name"], sha256=model_meta["sha256"], device=model_meta["device"],
                    inputSize=model_meta.get("inputSize", imgsz), **common)
        if model_meta.get("backend"):
            info["backend"] = model_meta["backend"]
        if model_meta.get("components"):
            info["components"] = model_meta["components"]
        return info
    return dict(name=Path(model_path).stem.replace("yolo", "YOLO"), sha256=file_hash(model_path),
                device=torch.cuda.get_device_name(0) if device == 0 else platform.processor() + " / CPU",
                inputSize=imgsz, **common)


def analyze(video, model_path, out, *, run_id, options=None, progress=None, detector=None, max_seconds=600):
    started = time.perf_counter()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    options = options or {}
    sequence = ActionSequence(options['action_workflow']) if options.get('action_workflow') else None
    inspector = None
    if options.get('inspection_config'):
        if options.get('crop') is not None:
            raise AnalysisError('INVALID_STATION_CROP', '工位配置不能与视频裁切叠加')
        from .inspection import Inspector
        inspector = Inspector(options['inspection_config'])
    meta = probe(video, max_seconds)
    start, end, sample_fps, crop, imgsz = validate_options(meta, options.get("start", 0), options.get("end"),
        options.get("sample_fps", 5), options.get("crop"), options.get("imgsz", 640))
    model_meta = options.get("model_meta")
    if not model_meta and not Path(model_path).is_file():
        raise AnalysisError("MODEL_MISSING", "模型文件不存在，请检查服务器模型配置")
    import torch
    gpu_peak = 0.0
    if torch.cuda.is_available():
        try:
            torch.cuda.reset_peak_memory_stats()
        except Exception:
            pass
    if detector is None:
        from ultralytics import YOLO
        detector = YOLO(str(model_path))
    device = 0 if torch.cuda.is_available() else "cpu"
    fps = meta["fps"]
    first, last = round(start * fps), min(meta["frame_count"], int(end * fps))
    if first >= last:
        raise AnalysisError("EMPTY_RANGE", "选择的时间范围不包含可解码帧")
    origin = first / fps
    x, y, w, h = crop
    ratio = min(1, 1280 / w, 720 / h)
    pw, ph = max(2, int(w * ratio) // 2 * 2), max(2, int(h * ratio) // 2 * 2)
    preview_fps = min(fps, 15)
    cap = cv2.VideoCapture(str(video))
    writer = cv2.VideoWriter(str(out / "video.webm"), cv2.VideoWriter_fourcc(*"VP80"), preview_fps, (pw, ph))
    if not writer.isOpened():
        cap.release()
        raise AnalysisError("ENCODER_UNAVAILABLE", "服务器缺少WebM编码能力")
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    frames, evidence, inference = [], {}, []
    tracker = PoseTracker()
    next_sample = next_preview = 0
    process = psutil.Process()
    peak, duration, detections = 0, 0, 0
    try:
        for frame_index in range(first, last):
            if progress:
                progress((frame_index - first) / max(1, last - first))
            ok, full = cap.read()
            if not ok:
                raise AnalysisError("DECODE_INTERRUPTED", "视频在预期片尾前解码中断，未发布完整报告")
            t = (frame_index - first) / fps
            actual_pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if actual_pts > 0 and abs(actual_pts - frame_index / fps) > .25:
                raise AnalysisError("VARIABLE_FRAME_RATE", "该视频时间戳不规则，请先转为恒定帧率视频")
            image = full[y:y + h, x:x + w]
            duration = (frame_index - first + 1) / fps
            if t + 1e-7 >= next_preview:
                writer.write(cv2.resize(image, (pw, ph)))
                next_preview += 1 / preview_fps
            if t + 1e-7 < next_sample:
                continue
            next_sample += 1 / sample_fps
            result = detector.predict(image, imgsz=imgsz, conf=.35, iou=.5, max_det=50, device=device, verbose=False)[0]
            people = tracker.update(result, w, h, t)
            detections += len(people)
            if detections > 50000:
                raise AnalysisError("OBSERVATION_LIMIT", "人员观测数量过多，请缩短分析片段")
            ms = float(result.speed.get("inference", 0))
            inference.append(ms)
            if torch.cuda.is_available():
                try:
                    gpu_peak = max(gpu_peak, torch.cuda.max_memory_allocated() / 1048576)
                except Exception:
                    pass
            frames.append(dict(time=round(t, 6), inferenceMs=ms, people=people))
            if sequence:
                observation = sequence.update(people, t)
                # Store only newly emitted events per sample; avoid quadratic JSON.
                previous_count = frames[-2].get('actionSequence', {}).get('eventCount', 0) if len(frames)>1 else 0
                frames[-1]['actionSequence'] = {k: observation[k] for k in ['mode', 'verdict', 'subjects', 'eventCount']}
                frames[-1]['actionSequence']['subjects'] = [subject for subject in observation['subjects'] if subject['observed']]
                frames[-1]['actionSequence']['events'] = [event for event in observation['events'] if event['id']>previous_count]
            if inspector:
                frames[-1]['inspection'] = inspector.inspect(image)
            # At most six evidence images; raw frames are never retained for the whole video.
            for action in {p["action"] for p in people}:
                if action not in evidence:
                    name = f"evidence-{len(evidence) + 1}.jpg"
                    cv2.imwrite(str(out / name), annotate(cv2.resize(image, (pw, ph)), people))
                    evidence[action] = dict(time=round(t, 6), image=name, caption=LABELS[action])
            peak = max(peak, process.memory_info().rss / 1048576)
    finally:
        cap.release()
        writer.release()
    if not frames:
        raise AnalysisError("EMPTY_RANGE", "没有生成分析采样")
    for i, frame in enumerate(frames):
        frame["end"] = frames[i + 1]["time"] if i + 1 < len(frames) else duration
    segments, tracks, sums = summarize(frames)
    scores = [p["confidence"] for f in frames for p in f["people"]]
    person_seconds = sum(sums.values())
    events = [dict(id=i + 1, trackId=s["trackId"], start=s["start"], end=s["end"], title="姿态证据不足",
        severity="low", description="关键点不足，不能确定动作；请复核遮挡和画面清晰度。", segmentId=s["id"])
        for i, s in enumerate(s for s in segments if s["action"] == "unknown" and s["duration"] >= .6)]
    if not events:
        events.append(dict(id=1, trackId=None, start=0, end=duration, title="SOP完成证据待补充", severity="low",
                           description="未接入工具、工件和设备状态，不能判定工序合格。"))
    data = dict(version=2, id=run_id, generatedAt=datetime.now(timezone.utc).isoformat(), verdict="REVIEW",
        source=dict(title=options.get("title", Path(video).name), description="用户上传视频的服务器离线分析；姿态规则不等于工艺动作。",
            file=Path(video).name, sha256=file_hash(video), start=origin, end=origin + duration, duration=duration,
            fps=fps, width=w, height=h, crop=crop, video="video.webm", hasBurnedAnnotations=None),
        model=_model_descriptor(model_path, model_meta, device, torch, sample_fps, imgsz),
        summary=dict(analyzedFrames=len(frames), framesWithPeople=sum(bool(f["people"]) for f in frames),
            presenceRate=sum(bool(f["people"]) for f in frames) / len(frames), trackCount=len(tracks),
            maxPeople=max(len(f["people"]) for f in frames), segmentCount=len(segments), personSeconds=person_seconds,
            meanConfidence=float(np.mean(scores)) if scores else 0, actionCoverage=1 - sums["unknown"] / person_seconds if person_seconds else 0,
            meanInferenceMs=float(np.mean(inference)), p95InferenceMs=float(np.percentile(inference, 95)),
            processingSeconds=time.perf_counter() - started, peakMemoryMB=peak,
            peakGpuMemoryMB=round(gpu_peak, 1) if gpu_peak else None),
        frames=frames, tracks=tracks, segments=segments, events=events, evidence=list(evidence.values()),
        actionSummary=[dict(action=a, label=LABELS[a], seconds=v, color=COLORS[a]) for a, v in sums.items() if v > 0],
        sop=[dict(name=name, reason=reason) for name, reason in [
            ("人员存在与作业区域", "人体检测不等于进入指定工位；需要区域与产品配置。"),
            ("工具与装配动作", "当前只有姿态规则，未验证工具、零件及实际操作。"),
            ("完成与质量确认", "需要正式SOP和设备结果，不能凭视频片尾确认完成。")]],
        limitations=["当前只输出人体和姿态规则，不自动判定SOP合规。", "轨迹编号不是人员身份；遮挡与重连可能产生新编号。",
            "采样之间的标签保持是展示假设，不能证明持续质量条件。", "未使用人工真值验证，不能将检测分数解释为动作准确率。",
            "首版支持恒定帧率视频；预览最长15fps、最大1280×720，不含音频，尾帧时长可能存在一个预览帧的量化误差。",
            "没有独立核验源视频是否包含旧标注，只有本次青色框和报告记录属于本次分析。"])
    if inspector:
        from .stations import verify_assets
        verify_assets(inspector.frozen)
        data['inspection'] = {'config':inspector.frozen, 'lastFrame':frames[-1]['inspection'],
                              'scope':'逐采样帧候选；不将跨帧可见性累计成产品完成'}
        data['sop'] = [dict(name=c['name'],reason=c['reason']) for c in frames[-1]['inspection']['checks']]
        data['limitations'][0] = '工位视觉仅输出可见候选；动作、顺序及安装质量需要正式配置和设备证据。'
    if sequence:
        data['actionSequence'] = sequence.result(final=True)
        data['summary']['sequenceSubjects'] = len(sequence.subjects)
        for event in sequence.events:
            if event['code'] == 'STEP_OBSERVED':
                continue
            data['events'].append(dict(id=len(data['events'])+1, trackId=event['trackId'], start=event['time'],
                end=min(duration, event['time']+1/sample_fps), title='动作时序复核：'+event['code'],
                severity='low', description=event['detail']))
        data['limitations'].append('动作时序是按同一人物轨迹观察的诊断，缺少产品、工具和设备结果时不输出生产合格。')
    with (out / "segments.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["人员轨迹", "开始秒", "结束秒", "持续秒", "姿态规则标签", "人体检测平均置信度", "判定依据", "数据来源"])
        for s in segments:
            cw.writerow([f'P{s["trackId"]:02}', s["start"], s["end"], s["duration"], s["label"],
                         s["meanConfidence"], s["evidence"], data["model"]["name"] + " 实际离线分析"])
    (out / "analysis.json").write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    build_report(data, out)
    manifest = dict(schema_version=1, run_id=run_id, run_mode="OFFLINE", verdict="REVIEW",
        model_hash=data["model"]["sha256"], source_hash=data["source"]["sha256"], options=options,
        completed_at=datetime.now(timezone.utc).isoformat(), total_processing_seconds=time.perf_counter() - started,
        files={p.name: file_hash(p) for p in out.iterdir() if p.is_file() and p.name != "manifest.json"})
    manifest["algorithm_hashes"] = {p.name: file_hash(p) for p in Path(__file__).parent.glob("*.py")}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    return data
