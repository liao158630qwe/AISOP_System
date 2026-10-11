import json
import time


def _station_config(asset=None):
    target = {
        'id': 'target-1', 'category': 'yellow_clip', 'roi': [0, 0, 1, 1],
        'method': 'hsv', 'min_area': .1, 'max_area': .8,
    }
    if asset:
        target.update(method='template', asset=asset, threshold=.9)
    return [{
        'id': 'cache-test', 'name': '缓存测试工位', 'version': '1',
        'approved': True, 'fixed_fixture': True,
        'checks': [{'id': 'check-1', 'name': '测试', 'kind': 'presence',
                    'expected': {'yellow_clip': 1}, 'targets': [target]}],
    }]


def test_station_snapshot_cache_reuses_hash_until_resource_changes(monkeypatch, tmp_path):
    import algorithms.stations as stations

    config = tmp_path / 'stations.json'
    asset = tmp_path / 'template.png'
    asset.write_bytes(b'first')
    config.write_text(json.dumps(_station_config('template.png')), encoding='utf-8')
    monkeypatch.setattr(stations, 'CONFIG', config)
    monkeypatch.setattr(stations, 'ROOT', tmp_path)

    calls = []
    original_hash = stations.sha_file
    monkeypatch.setattr(stations, 'asset_path', lambda name: asset)
    monkeypatch.setattr(stations, 'sha_file', lambda path: calls.append(path) or original_hash(path))

    first = stations.snapshot('cache-test')
    second = stations.snapshot('cache-test')
    assert first == second
    assert len(calls) == 1

    asset.write_bytes(b'second-content')
    # Ensure filesystems with coarse timestamp resolution still produce a
    # different signature during this test.
    time.sleep(.001)
    third = stations.snapshot('cache-test')
    assert third['sha256'] != first['sha256']
    assert len(calls) == 2


def test_action_snapshot_cache_reuses_frozen_workflow(monkeypatch, tmp_path):
    import algorithms.action_sop as action_sop

    config = tmp_path / 'action-workflows.json'
    config.write_text(json.dumps([{
        'id': 'cache-test', 'name': '缓存测试流程', 'version': '1',
        'steps': [{'id': 'raise', 'name': '举手', 'actions': ['raised']}],
    }]), encoding='utf-8')
    monkeypatch.setattr(action_sop, 'CONFIG', config)

    calls = []
    original_freeze = action_sop.freeze
    monkeypatch.setattr(action_sop, 'freeze', lambda value: calls.append(value) or original_freeze(value))

    first = action_sop.snapshot('cache-test')
    second = action_sop.snapshot('cache-test')
    assert first == second
    assert len(calls) == 1
