import argparse
import json
import logging
import threading
import time
from pathlib import Path

from sqlalchemy import select

from .config import Settings
from .db import claim, finish, heartbeat, make_engine, media
from .cleanup import cleanup_data

log = logging.getLogger("worker")
_model_cache = {}


def execute_one(engine, settings, analyzer=None):
    real_inference = analyzer is None
    job = claim(engine, settings.lease_seconds)
    if job is None:
        return False
    from algorithms.video import AnalysisError, Cancelled, file_hash
    if analyzer is None:
        from algorithms.pipeline import analyze
        analyzer = analyze
    stop, lost = threading.Event(), threading.Event()
    progress_value = [0.0]

    def renew():
        while not stop.wait(max(1, settings.lease_seconds / 4)):
            try:
                if not heartbeat(engine, job["id"], job["token"], settings.lease_seconds, progress_value[0]):
                    lost.set()
                    return
            except Exception:
                log.exception("Heartbeat failed: %s", job["id"])
                lost.set()
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()

    def progress(value):
        progress_value[0] = value
        if lost.is_set():
            raise Cancelled()

    relative = f'{job["id"]}-{job["token"]}'
    folder = settings.data_dir / "runs" / relative
    try:
        with engine.connect() as c:
            source = c.execute(select(media).where(media.c.id == job["media_id"])).mappings().one()
        video = settings.data_dir / "uploads" / (source["id"] + source["suffix"])
        backend = job["options"].get("pose_backend") or "yolo"
        if backend == "yolo":
            if not settings.model_path.is_file() or file_hash(settings.model_path) != job["model_hash"]:
                raise AnalysisError("MODEL_CHANGED", "任务创建后模型改变或缺失，请重新创建任务")
        elif real_inference:
            from algorithms.rtmlib_pose import model_files
            if file_hash(model_files(backend)[-1][1]) != job["model_hash"]:
                raise AnalysisError("MODEL_CHANGED", "任务创建后姿态模型改变或缺失，请重新创建任务")
        if not video.is_file() or file_hash(video) != source["sha256"]:
            raise AnalysisError("MEDIA_CHANGED", "输入媒体缺失或校验失败")
        extra = {}
        if real_inference:
            if job["model_hash"] not in _model_cache:
                _model_cache.clear()
                if backend == "yolo":
                    from ultralytics import YOLO
                    _model_cache[job["model_hash"]] = YOLO(str(settings.model_path))
                else:
                    from algorithms.rtmlib_pose import RTMLibDetector
                    _model_cache[job["model_hash"]] = RTMLibDetector(variant=backend, mode="balanced")
            detector = _model_cache[job["model_hash"]]
            extra["detector"] = detector
            if backend != "yolo":
                job["options"]["model_meta"] = detector.model_meta()
        result = analyzer(video, settings.model_path, folder, run_id=job["id"], options=job["options"], progress=progress,
                          max_seconds=settings.max_video_seconds, **extra)
        progress(1)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if real_inference and (manifest.get("model_hash") != job["model_hash"] or manifest.get("source_hash") != source["sha256"]):
            raise AnalysisError("INPUT_CHANGED", "处理期间输入或模型版本发生改变，结果未发布")
        for name, sha in manifest["files"].items():
            if Path(name).name != name or file_hash(folder / name) != sha:
                raise AnalysisError("OUTPUT_INVALID", "输出文件完整性校验失败")
        if not {"analysis.json", "report.html", "segments.csv", "video.webm"}.issubset(manifest["files"]):
            raise AnalysisError("OUTPUT_INVALID", "分析结果不完整")
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        metrics = {
            "queue_wait_seconds": max(0, job["updated_at"] - job["created_at"]),
            "processing_seconds": summary.get("processingSeconds"),
            "inference_p95_ms": summary.get("p95InferenceMs"),
            "peak_memory_mb": summary.get("peakMemoryMB"),
            "peak_gpu_memory_mb": summary.get("peakGpuMemoryMB"),
            "analyzed_frames": summary.get("analyzedFrames"),
        }
        if not finish(engine, job["id"], job["token"], "SUCCEEDED", relative, metrics=metrics):
            finish(engine, job["id"], job["token"], "CANCELLED")
    except Cancelled:
        finish(engine, job["id"], job["token"], "CANCELLED")
    except AnalysisError as exc:
        if not finish(engine, job["id"], job["token"], "FAILED", error=(exc.code, str(exc))):
            finish(engine, job["id"], job["token"], "CANCELLED")
    except Exception:
        log.exception("Analysis failed: %s", job["id"])
        if not finish(engine, job["id"], job["token"], "FAILED", error=("ANALYSIS_FAILED", "分析失败，请查看服务器日志与任务编号")):
            finish(engine, job["id"], job["token"], "CANCELLED")
    finally:
        stop.set()
        thread.join(timeout=5)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    last_cleanup = 0.0
    while True:
        try:
            if time.time() - last_cleanup >= settings.cleanup_interval_seconds:
                result = cleanup_data(engine, settings.data_dir,
                                      run_retention_seconds=settings.run_retention_seconds,
                                      upload_retention_seconds=settings.upload_retention_seconds)
                log.info("Retention cleanup: %s", result)
                last_cleanup = time.time()
            worked = execute_one(engine, settings)
        except Exception:
            log.exception("Task polling failed")
            worked = False
        if args.once:
            break
        if not worked:
            time.sleep(2)


if __name__ == "__main__":
    main()
