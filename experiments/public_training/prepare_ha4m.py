"""Convert the official HA4M skeleton ZIPs into causal COCO17-projected 74-D features.

This is a Kinect-derived research feature version, not a YOLO deployment model.
"""
import argparse
import io
import json
from pathlib import Path
import re
import zipfile

import numpy as np

from common import file_hash, save_json

COCO_FROM_KINECT = [27, 28, 30, 29, 31, 5, 12, 6, 13, 7, 14, 18, 22, 19, 23, 20, 24]
LABELS = ['carrier', 'gear_bearings', 'planet_gears', 'carrier_shaft', 'sun_shaft', 'sun_gear',
          'sun_gear_bearing', 'ring_bear', 'join_blocks', 'cover', 'screws', 'allen_key_turn_return']
FEATURE_SPEC = {'version': 'ha4m-kinect-coco17-74-v1', 'dimension': 74, 'sample_hz': 10,
    'source_hz': 30, 'sample_rule': 'source frame IDs 0,3,6,...; never interpolate',
    'joint_order': 'COCO17', 'kinect_joint_indices': COCO_FROM_KINECT,
    'features_0_67': 'per-joint interleaved torso-normalized x,y,confidence/0.666667,visible mask',
    'features_68_73': ['image-normalized hip velocity x', 'image-normalized hip velocity y',
                       'log torso-scale velocity', 'age since last new skeleton timestamp seconds',
                       'torso scale / 1536', 'fraction of visible COCO17 joints'],
    'image_dimensions': [2048, 1536], 'visible_confidence_threshold': .66,
    'origin': 'midpoint of hips', 'scale': 'distance from midpoint of hips to midpoint of shoulders',
    'masking': 'dont-care label 0, missing/ambiguous body, invalid root/scale, fewer than 8 visible joints, age > .5 seconds',
    'not_deployment_compatible': 'Kinect-derived projections/confidence and 12 HA4M labels differ from YOLO five-action runtime'}


def prepare_one(name, root):
    archive_path, labels_path = root/'raw'/(name+'.zip'), root/'raw'/(name+'-Labels.txt')
    output = root/'prepared'/(name+'.npz')
    meta_path = root/'prepared'/(name+'.json')
    if output.exists() and meta_path.exists():
        return json.loads(meta_path.read_text(encoding='utf-8'))
    labels = np.loadtxt(labels_path, dtype=np.int64, ndmin=2)
    assert labels.shape[1] == 3 and len(np.unique(labels[:, 0])) == len(labels)
    assert np.all((labels[:, 1] >= 0) & (labels[:, 1] <= 12))
    sampled_ids = np.arange(0, labels[:, 0].max()+1, 3)
    length = len(sampled_ids)
    xy = np.zeros((length, 17, 2), np.float32)
    conf = np.zeros((length, 17), np.float32)
    timestamps = np.full(length, -1, np.int64)
    y = np.full(length, -100, np.int64)
    indexed = {int(row[0]): int(row[1]) for row in labels}
    for i, frame in enumerate(sampled_ids):
        if indexed.get(int(frame), 0) > 0:
            y[i] = indexed[int(frame)]-1
    counters = {'missing': 0, 'ambiguous_body': 0, 'malformed': 0, 'duplicate_timestamp_samples': 0}
    seen = set()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            match = re.search(r'FrameID(\d+)_DeviceTimeStamp(\d+)us\.txt$', member)
            if not match:
                continue
            frame, timestamp = map(int, match.groups())
            if frame % 3 or frame//3 >= length:
                continue
            index = frame//3
            if index in seen:
                raise ValueError(f'{name}: duplicated frame ID or multiple cameras')
            seen.add(index)
            text = archive.read(member).decode('utf-8-sig')
            numbers = np.fromstring(text.split('\n', 1)[-1], sep=' ', dtype=np.float32)
            if not len(numbers) or len(numbers) % 14:
                counters['malformed'] += 1
                continue
            table = numbers.reshape(-1, 14)
            if len(np.unique(table[:, 0])) != 1:
                counters['ambiguous_body'] += 1
                continue
            if len(table) != 32 or sorted(table[:, 1].astype(int)) != list(range(32)):
                counters['malformed'] += 1
                continue
            table = table[np.argsort(table[:, 1])][COCO_FROM_KINECT]
            xy[index] = table[:, 10:12]
            conf[index] = table[:, 2]
            timestamps[index] = timestamp
    counters['missing'] = length-len(seen)
    finite = np.isfinite(xy).all(axis=-1) & np.isfinite(conf)
    visible = (conf >= .66) & finite
    xy = np.nan_to_num(xy)
    hip = (xy[:, 11]+xy[:, 12])/2
    shoulder = (xy[:, 5]+xy[:, 6])/2
    scale = np.linalg.norm(shoulder-hip, axis=1)
    root_valid = visible[:, [5, 6, 11, 12]].all(axis=1) & (scale > 30)
    normalized = np.clip((xy-hip[:, None])/np.maximum(scale[:, None, None], 30), -5, 5)
    normalized[~visible] = 0
    normalized[~root_valid] = 0
    confidence = np.clip(np.nan_to_num(conf)/.666667, 0, 1)*finite
    joint = np.concatenate((normalized, confidence[:, :, None], visible[:, :, None]), axis=-1).reshape(length, 68)
    extra = np.zeros((length, 6), np.float32)
    last_new = 0
    for i in range(length):
        if timestamps[i] >= 0 and (i == 0 or timestamps[i] != timestamps[i-1]):
            last_new = i
        elif i and timestamps[i] == timestamps[i-1] and timestamps[i] >= 0:
            counters['duplicate_timestamp_samples'] += 1
        extra[i, 3] = min((i-last_new)/10, 5)
        if i and root_valid[i] and root_valid[i-1] and timestamps[i] != timestamps[i-1]:
            extra[i, :2] = np.clip((hip[i]-hip[i-1])/np.array([2048, 1536])*10, -5, 5)
            extra[i, 2] = np.clip(np.log(scale[i]/scale[i-1])*10, -5, 5)
    extra[:, 4] = np.where(root_valid, scale/1536, 0)
    extra[:, 5] = visible.mean(axis=1)
    x = np.concatenate((joint, extra), axis=1).astype(np.float32)
    input_valid = root_valid & (visible.sum(axis=1) >= 8) & (extra[:, 3] <= .5) & (timestamps >= 0)
    annotated = int((y >= 0).sum())
    y[~input_valid] = -100
    assert np.isfinite(x).all() and x.shape == (length, 74)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, x=x, y=y, source_frame=sampled_ids, timestamp_us=timestamps,
                        input_valid=input_valid, subject=np.int64(int(name[3:6])))
    metadata = {'recording': name, 'subject': int(name[3:6]), 'samples_10hz': length,
        'annotated_action_samples': annotated, 'usable_action_samples': int((y >= 0).sum()),
        'coverage_of_annotated_samples': float((y >= 0).sum()/max(annotated, 1)),
        'class_counts': np.bincount(y[y >= 0], minlength=12).tolist(), 'source_anomalies': counters,
        'archive_sha256': file_hash(archive_path), 'labels_sha256': file_hash(labels_path),
        'prepared_sha256': file_hash(output), 'feature_version': FEATURE_SPEC['version']}
    save_json(meta_path, metadata)
    return metadata


def main(root):
    manifest = json.loads((root/'download-manifest.json').read_text(encoding='utf-8'))
    if manifest['errors'] or len(manifest['recordings']) != len(manifest['requested_recordings']):
        raise RuntimeError('Complete selected downloads before freezing the train/test dataset')
    records = []
    for i, record in enumerate(manifest['recordings'], 1):
        result = prepare_one(record['recording'], root)
        records.append(result)
        if i % 10 == 0 or i == len(manifest['recordings']):
            print(f'Prepared {i}/{len(manifest["recordings"])}', flush=True)
    rng = np.random.default_rng(20261010)
    subjects = sorted({record['subject'] for record in records})
    shuffled = rng.permutation(subjects).tolist()
    if len(subjects) != 41:
        raise ValueError('This registered split expects all 41 subjects')
    groups = {'train': sorted(shuffled[:27]), 'val': sorted(shuffled[27:34]), 'test': sorted(shuffled[34:])}
    splits = {part: [r['recording'] for r in records if r['subject'] in members] for part, members in groups.items()}
    assert not (set(groups['train']) & set(groups['test']) or set(groups['train']) & set(groups['val']) or set(groups['val']) & set(groups['test']))
    save_json(root/'dataset.json', {'source': manifest['source'], 'license': 'CC-BY-4.0',
        'feature_spec': FEATURE_SPEC, 'labels': LABELS, 'records': records,
        'splits': splits, 'subjects': groups, 'split_seed': 20261010,
        'protocol': 'Custom subject-disjoint 27/7/7 split, fixed before model training; not an official benchmark score',
        'preparation_script_sha256': file_hash(__file__),
        'download_manifest_sha256': file_hash(root/'download-manifest.json')})
    print(json.dumps({'recordings': len(records), 'samples_10hz': sum(r['samples_10hz'] for r in records),
        'split_recordings': {k: len(v) for k, v in splits.items()}, 'subjects': groups}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('work/public-training-2026-10-10/ha4m'))
    main(parser.parse_args().data)
