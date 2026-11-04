"""Extract reviewable source frames without inventing missing-screw labels."""
import argparse
import json
from pathlib import Path
import sys
import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('video', type=Path)
    parser.add_argument('--out', type=Path, default=Path('outputs/screw-inspection-2026-10-17'))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    folder = args.out / 'frames'
    folder.mkdir(exist_ok=True)
    cap = cv2.VideoCapture(str(args.video))
    fps, count = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    metadata = dict(source=str(args.video.resolve()), fps=fps, frame_count=count,
                    width=int(cap.get(3)), height=int(cap.get(4)), duration=count/fps)
    frames, tiles, next_sample = [], [], 0
    for index in range(count):
        ok, image = cap.read()
        if not ok:
            raise RuntimeError(f'Decode ended early at {index}/{count}')
        t = index/fps
        if t + 1e-6 < next_sample:
            continue
        next_sample += .25
        image = cv2.resize(image, (1920, 1080), interpolation=cv2.INTER_AREA)
        name = f'{index:05}.jpg'
        cv2.imwrite(str(folder/name), image, [cv2.IMWRITE_JPEG_QUALITY, 94])
        frames.append(dict(frame=index, time=t, file=name))
        if round(next_sample*4) % 4 == 1:
            crop = image[170:1040, 450:1100]
            tile = cv2.resize(crop, (260, 348))
            cv2.putText(tile, f'{t:.2f}s', (8, 26), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 255), 2)
            tiles.append(tile)
        if len(frames) % 40 == 0:
            print(f'extracted {len(frames)} frames at {t:.1f}s', flush=True)
    cap.release()
    (args.out/'source.json').write_text(json.dumps(dict(**metadata, samples=frames), indent=2), encoding='utf-8')
    for start in range(0, len(tiles), 18):
        group = tiles[start:start+18]
        group += [np.zeros_like(tiles[0])]*(18-len(group))
        sheet = np.vstack([np.hstack(group[i:i+6]) for i in range(0, 18, 6)])
        cv2.imwrite(str(args.out/f'operations-{start//18+1}.jpg'), sheet)
    print(json.dumps({k:v for k,v in metadata.items() if k != 'samples'}), flush=True)


if __name__ == '__main__':
    main()
