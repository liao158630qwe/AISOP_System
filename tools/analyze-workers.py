"""Compatibility CLI for the reusable offline pipeline."""
import argparse
import json
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from algorithms.pipeline import analyze


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', default='assets/factory-workers.mp4')
    parser.add_argument('--model', default='models/yolo11s-pose.pt')
    parser.add_argument('--out', default='runtime-data/cli-analysis')
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--end', type=float)
    parser.add_argument('--sample-fps', type=float, default=5)
    parser.add_argument('--imgsz', type=int, default=640)
    parser.add_argument('--crop', type=int, nargs=4)
    parser.add_argument('--stock', action='store_true', help='Legacy option; does not assert media provenance')
    parser.add_argument('--pose-backend', choices=['yolo', 'rtmpose', 'rtmo'], default='yolo',
        help='姿态后端：yolo=YOLO11-pose（默认）；rtmpose=YOLOX+RTMPose（rtmlib）；rtmo=RTMO 单阶段（rtmlib）')
    parser.add_argument('--rtm-mode', choices=['lightweight', 'balanced', 'performance'], default='balanced',
        help='rtmlib 模型档位，仅 rtmpose/rtmo 后端使用')
    args = parser.parse_args()
    detector = model_meta = None
    if args.pose_backend != 'yolo':
        from algorithms.rtmlib_pose import RTMLibDetector
        detector = RTMLibDetector(variant=args.pose_backend, mode=args.rtm_mode)
        model_meta = detector.model_meta()
    data = analyze(args.video, args.model, args.out, run_id='cli-' + str(uuid.uuid4()), options={
        'start':args.start, 'end':args.end, 'sample_fps':args.sample_fps, 'imgsz':args.imgsz,
        'crop':args.crop, 'title':Path(args.video).name, 'model_meta':model_meta}, detector=detector)
    print(json.dumps(data['summary'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
