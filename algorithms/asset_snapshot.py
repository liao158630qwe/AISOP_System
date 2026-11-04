"""Thread-safe, bounded snapshots for any configured model and its dependencies."""
import copy
import hashlib
import json
from pathlib import Path
import threading

from .video import AnalysisError, file_hash


def signature(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class AssetSnapshotCache:
    """One cache entry per consumer; stat every dependency, hash only on change.

    This is a performance cache, not a replacement for full task-boundary hash
    verification. Publishers should replace assets atomically. A changing file
    set is never published as a frozen snapshot.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entry = None

    def get(self, config_path, root, assets_for, *, validate=None,
            missing_code='MODEL_MISSING', missing_message='模型或标定资源缺失'):
        config_path, root = Path(config_path).resolve(), Path(root).resolve()
        key = (str(config_path), str(root))
        with self._lock:
            for _ in range(3):
                try:
                    config_sig = signature(config_path)
                    if self._entry and self._entry[0] == key:
                        _, old_config_sig, old_asset_sigs, frozen = self._entry
                        if old_config_sig == config_sig and all(
                            signature(root / name) == sig for name, sig in old_asset_sigs
                        ):
                            return copy.deepcopy(frozen)
                    raw = config_path.read_bytes()
                    config = json.loads(raw.decode('utf-8'))
                    if validate:
                        validate(config)
                    names = list(dict.fromkeys(assets_for(config)))
                    before = tuple((name, signature(root / name)) for name in names)
                    frozen = dict(config=config, config_hash=hashlib.sha256(raw).hexdigest(),
                                  assets={name: file_hash(root / name) for name in names})
                    after = tuple((name, signature(root / name)) for name in names)
                    if config_sig == signature(config_path) and before == after:
                        self._entry = (key, config_sig, after, frozen)
                        return copy.deepcopy(frozen)
                except OSError as exc:
                    self._entry = None
                    raise AnalysisError(missing_code, missing_message) from exc
            self._entry = None
            raise AnalysisError('MODEL_CHANGED', '模型或配置正在更新，请稍后重试')
