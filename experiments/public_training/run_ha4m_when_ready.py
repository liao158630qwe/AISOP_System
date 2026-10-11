"""Continue the requested local experiment after its running download completes."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path('work/public-training-2026-10-10')
manifest = root/'ha4m/download-manifest.json'
last_report = 0
while True:
    try:
        value = json.loads(manifest.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(5)
        continue
    expected = len(value['requested_recordings'])
    completed = len(value['recordings'])
    if expected != 82:
        raise RuntimeError('Expected the frozen 41-subject x 2-recording first experiment')
    if completed == expected and not value['errors']:
        break
    if completed+len(value['errors']) == expected and value['errors']:
        raise RuntimeError('Some downloads failed; resume downloads before training')
    if time.monotonic()-last_report > 60:
        print(f'Waiting for selected data: {completed}/{expected}', flush=True)
        last_report = time.monotonic()
    time.sleep(5)

for script in ['prepare_ha4m.py', 'train_temporal.py']:
    print(f'Running {script}', flush=True)
    subprocess.run([sys.executable, '-u', str(Path(__file__).with_name(script))], check=True)
print('HA4M training and testing complete.', flush=True)
