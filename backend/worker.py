import argparse
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import claim, finish, heartbeat, make_engine, media
from .cleanup import cleanup_data
from .health import pulse_worker, remove_worker

log = logging.getLogger("worker")
_model_cache = {}


def load_analyzer(analysis_type):
    if analysis_type == 'screw':
        from algorithms.screws import analyze
    elif analysis_type == 'sticker':
        from algorithms.stickers import analyze
    elif analysis_type == 'scene':
        from algorithms.scene_runtime import analyze
    else:
        from algorithms.pipeline import analyze
    return analyze


def execute_one(engine, settings, analyzer=None, *, worker_id=None):
    real_inference = analyzer is None
    job = claim(engine, settings.lease_seconds)
    if job is None:
        return False
    from algorithms.video import AnalysisError, Cancelled, file_hash
    stop, lost = threading.Event(), threading.Event()
    lease_uncertain = threading.Event()
    progress_value = [0.0]

    def renew():
        while not stop.wait(max(1, min(2, settings.lease_seconds / 10))):
            try:
                if not heartbeat(engine, job["id"], job["token"], settings.lease_seconds, progress_value[0]):
                    lost.set()
                    return
                if worker_id:
                    pulse_worker(engine, worker_id)
            except Exception:
                log.exception("Heartbeat failed: %s", job["id"])
                lease_uncertain.set()
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
        if analyzer is None:
            # Optional dependency failures must terminate this job, not strand it
            # RUNNING outside the heartbeat/error handling boundary.
            analyzer = load_analyzer(job['options'].get('analysis_type'))
        with engine.connect() as c:
            source = c.execute(select(media).where(media.c.id == job["media_id"])).mappings().one()
        video = settings.data_dir / "uploads" / (source["id"] + source["suffix"])
        screw_mode = job['options'].get('analysis_type') in ('screw','sticker')
        scene_mode = job['options'].get('analysis_type') == 'scene'
        backend = job["options"].get("pose_backend") or "yolo"
        if scene_mode:
            from algorithms.scene_runtime import verify
            verify(job['options']['scene_config'])
        elif screw_mode:
            task=job['options']['analysis_type']
            if task=='sticker':
                from algorithms.stickers import verify
            else:
                from algorithms.screws import verify
            verify(job['options'][task+'_config'])
            if job['options'].get('person_config'):
                from algorithms.person_detection import verify as verify_person
                verify_person(job['options']['person_config'])
        elif backend == "yolo":
            if not settings.model_path.is_file() or file_hash(settings.model_path) != job["model_hash"]:
                raise AnalysisError("MODEL_CHANGED", "任务创建后模型改变或缺失，请重新创建任务")
        elif real_inference:
            from algorithms.rtmlib_pose import model_files
            if file_hash(model_files(backend)[-1][1]) != job["model_hash"]:
                raise AnalysisError("MODEL_CHANGED", "任务创建后姿态模型改变或缺失，请重新创建任务")
        if not video.is_file() or file_hash(video) != source["sha256"]:
            raise AnalysisError("MEDIA_CHANGED", "输入媒体缺失或校验失败")
        extra = {}
        if real_inference and screw_mode:
            from algorithms.person_detection import load_detector, snapshot as person_snapshot
            frozen_person = job['options'].get('person_config') or person_snapshot(settings.model_path, backend)
            job['options']['person_config'] = frozen_person
            pose_key = ('person', backend, tuple(asset['sha256'] for asset in frozen_person['assets']))
            if pose_key not in _model_cache:
                _model_cache.clear()
                _model_cache[pose_key] = load_detector(settings.model_path, backend)
            extra['pose_detector'] = _model_cache[pose_key]
        if real_inference and scene_mode:
            from algorithms.scene_runtime import SceneDetector
            frozen = job['options']['scene_config']
            cache_key = ('scene', frozen['sha256'])
            if cache_key not in _model_cache:
                _model_cache.clear()
                _model_cache[cache_key] = SceneDetector(frozen)
            extra['detector'] = _model_cache[cache_key]
        if real_inference and not screw_mode and not scene_mode:
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
        if real_inference and job['options'].get('person_config'):
            from algorithms.person_detection import verify as verify_person
            verify_person(job['options']['person_config'])
            if manifest.get('person_model_hash') != job['options']['person_config']['sha256']:
                raise AnalysisError('INPUT_CHANGED', '人体姿态模型版本发生改变，结果未发布')
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
        from .evidence import offline_record
        evidence = offline_record(job, folder, result if isinstance(result, dict) else {}, settings.evidence_retention_seconds)
        if not finish(engine, job["id"], job["token"], "SUCCEEDED", relative, metrics=metrics, evidence=evidence):
            finish(engine, job["id"], job["token"], "CANCELLED")
    except Cancelled:
        if not lease_uncertain.is_set():
            finish(engine, job["id"], job["token"], "CANCELLED")
        # An unavailable DB does not mean the user cancelled. Leave the lease
        # for recovery instead of turning a transient outage into data loss.
    except AnalysisError as exc:
        if not finish(engine, job["id"], job["token"], "FAILED", error=(exc.code, str(exc))):
            finish(engine, job["id"], job["token"], "CANCELLED")
    except SQLAlchemyError:
        # Infrastructure failure is recoverable through the lease; a committed
        # success also stays terminal if only its acknowledgement was lost.
        log.exception('Database interrupted job %s; preserving state for recovery', job['id'])
    except Exception:
        log.exception("Analysis failed: %s", job["id"])
        if not finish(engine, job["id"], job["token"], "FAILED", error=("ANALYSIS_FAILED", "分析失败，请查看服务器日志与任务编号")):
            finish(engine, job["id"], job["token"], "CANCELLED")
    finally:
        stop.set()
        thread.join(timeout=5)
    return True


def poll_once(engine, settings, *, cleanup_due=False, worker_id=None):
    if cleanup_due:
        try:
            from .evidence import expire_evidence
            expire_evidence(engine, settings.data_dir, retention=settings.evidence_retention_seconds)
            result = cleanup_data(engine, settings.data_dir,
                                  run_retention_seconds=settings.run_retention_seconds,
                                  upload_retention_seconds=settings.upload_retention_seconds)
            log.info('Retention cleanup: %s', result)
        except Exception:
            log.exception('Retention cleanup failed; task polling will continue')
    try:
        if worker_id:
            pulse_worker(engine, worker_id)
        if not settings.worker_analysis_enabled:
            return False
        if worker_id:
            return execute_one(engine, settings, worker_id=worker_id)
        return execute_one(engine, settings)
    except Exception:
        log.exception('Task polling failed')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    from .inference_runtime import configure_inference_runtime, configure_torch_runtime
    configure_inference_runtime(settings.opencv_threads)
    configure_torch_runtime(settings.torch_threads)
    from algorithms.gpu import check_cuda
    log.info("GPU runtime: %s", check_cuda())
    engine = make_engine(settings.database_url)
    from .migrations import require_current
    with engine.connect() as c:
        require_current(c)
    from .training_service import poll as poll_training
    from .dataset_service import poll as poll_datasets
    training_stop = threading.Event()
    supervisors = []
    if not args.once:
        jobs = [('datasets', poll_datasets)]
        if settings.worker_training_enabled:
            jobs.append(('training', poll_training))
        for name, target in jobs:
            thread = threading.Thread(target=target, args=(engine, settings, training_stop),
                                      name=name + '-supervisor', daemon=True)
            supervisors.append(thread)
            thread.start()
    log.info('Worker workloads: offline-analysis=%s, training=%s',
             settings.worker_analysis_enabled, settings.worker_training_enabled)
    next_cleanup, worker_id = 0.0, str(uuid.uuid4())
    try:
        while True:
            cleanup_due = time.monotonic() >= next_cleanup
            if cleanup_due:
                # Failed maintenance must not hammer the disk/database every 2s.
                next_cleanup = time.monotonic() + settings.cleanup_interval_seconds
            worked = poll_once(engine, settings, cleanup_due=cleanup_due, worker_id=worker_id)
            if args.once:
                break
            if not worked:
                time.sleep(2)
    finally:
        training_stop.set()
        for thread in supervisors:
            thread.join(timeout=15)
        try:
            remove_worker(engine, worker_id)
        except Exception:
            log.warning('Worker presence will expire automatically')
        engine.dispose()


if __name__ == "__main__":
    main()
