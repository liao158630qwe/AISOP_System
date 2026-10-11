"""Download the official public HA4M skeleton/label subset, with hashes and resume.

Source: https://baltig.cnr.it/ISP/ha4m (CC BY 4.0).
No RGB, depth, or point-cloud archives are requested.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

SHARE = 'https://cloud.cnr.it/owncloud/index.php/s/j5BZvG1sXPzh6x1'
DAV = 'https://cloud.cnr.it/owncloud/public.php/webdav/'
PUBLIC_TOKEN = 'j5BZvG1sXPzh6x1'  # Public share ID published by the dataset authors.


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def get_file(session, url, path, *, params=None, limit):
    temporary = path.with_suffix(path.suffix + '.part')
    for attempt in range(3):
        try:
            with session.get(url, params=params, stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                if int(response.headers.get('Content-Length', 0)) > limit:
                    raise ValueError('Unexpectedly large response')
                count = 0
                with temporary.open('wb') as output:
                    for chunk in response.iter_content(256 * 1024):
                        count += len(chunk)
                        if count > limit:
                            raise ValueError('Response exceeds per-recording size limit')
                        output.write(chunk)
            if path.suffix == '.zip':
                with zipfile.ZipFile(temporary) as archive:
                    if archive.testzip() is not None:
                        raise ValueError('ZIP CRC mismatch')
                    names = archive.namelist()
                    if not any('Skeletons/' in name and name.endswith('.txt') for name in names):
                        raise ValueError('Archive has no skeleton files')
                    if any('/Color/' in name or '/Depth/' in name for name in names):
                        raise ValueError('Unexpected non-skeleton modality')
            temporary.replace(path)
            return
        except (requests.RequestException, ValueError, zipfile.BadZipFile):
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def download_recording(recording, root):
    raw = root / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    metadata = raw / (recording + '.json')
    if metadata.exists():
        saved = json.loads(metadata.read_text(encoding='utf-8'))
        if all((raw / f['name']).exists() and sha256(raw / f['name']) == f['sha256'] for f in saved['files']):
            return saved
    session = requests.Session()
    session.auth = (PUBLIC_TOKEN, '')
    skeleton = raw / (recording + '.zip')
    labels = raw / (recording + '-Labels.txt')
    get_file(session, SHARE + '/download', skeleton,
             params={'path': '/' + recording, 'files': 'Skeletons'}, limit=200 * 1024 * 1024)
    get_file(session, DAV + recording + '/Labels.txt', labels, limit=2 * 1024 * 1024)
    record = {'recording': recording, 'subject': int(recording[3:6]), 'license': 'CC-BY-4.0',
              'downloaded_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'source': SHARE, 'skeleton_url': SHARE + '/download?path=/' + recording + '&files=Skeletons',
              'label_url': DAV + recording + '/Labels.txt',
              'files': [{'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}
                        for path in (skeleton, labels)]}
    metadata.write_text(json.dumps(record, indent=2), encoding='utf-8')
    session.close()
    return record


def main(args):
    args.out.mkdir(parents=True, exist_ok=True)
    response = requests.request('PROPFIND', DAV, auth=(PUBLIC_TOKEN, ''),
                                headers={'Depth': '1'}, timeout=(15, 45))
    response.raise_for_status()
    (args.out / 'source-directory.xml').write_bytes(response.content)
    folders = sorted(set(re.findall(r'IDU\d{3}V\d{3}', response.text)))
    if not folders:
        raise RuntimeError('No HA4M recording folders in official share')
    if args.first_per_subject:
        folders = [name for name in folders if int(name[-3:]) <= args.first_per_subject]
    if args.limit:
        folders = folders[:args.limit]
    (args.out / 'selection.json').write_text(json.dumps({'source': SHARE, 'recordings': folders,
        'selection': 'all official share recordings' if not args.first_per_subject and not args.limit else
        'explicit subset selected before training', 'first_per_subject': args.first_per_subject}, indent=2), encoding='utf-8')
    successes, errors = [], []
    start = time.monotonic()
    print(f'Downloading skeletons + labels for {len(folders)} recordings with {args.workers} workers', flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(download_recording, name, args.out): name for name in folders}
        for completed, future in enumerate(as_completed(pending), 1):
            try:
                result = future.result()
                successes.append(result)
                print(f'{completed}/{len(folders)} {result["recording"]} '
                      f'{sum(f["bytes"] for f in result["files"])/1e6:.1f} MB '
                      f'elapsed={time.monotonic()-start:.0f}s', flush=True)
            except Exception as error:
                entry = {'recording': pending[future], 'error': str(error)}
                errors.append(entry)
                print(json.dumps(entry), flush=True)
            (args.out / 'download-manifest.json').write_text(json.dumps(
                {'source': SHARE, 'license': 'CC-BY-4.0', 'requested_recordings': folders,
                 'recordings': sorted(successes, key=lambda row: row['recording']), 'errors': errors}, indent=2), encoding='utf-8')
    if errors:
        raise SystemExit(f'{len(errors)} downloads failed; rerun to resume')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('work/public-training-2026-10-10/ha4m'))
    parser.add_argument('--workers', type=int, default=3, choices=range(1, 5))
    parser.add_argument('--first-per-subject', type=int, default=0)
    parser.add_argument('--limit', type=int, default=0)
    main(parser.parse_args())
