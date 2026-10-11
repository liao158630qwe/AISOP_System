"""Versioned fixed-fixture inspection configuration; never invent process evidence."""
import hashlib
import json
import copy
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'stations.json'

# Configuration is read on every API request in the original implementation.
# Real-time inspection can issue dozens of requests per second, so keep the
# validated catalog and frozen snapshots in memory and invalidate them when a
# source file changes. Hashing still happens on a cache miss, preserving the
# existing resource-integrity check without repeating disk I/O per frame.
_cache_lock = threading.RLock()
_profiles_cache = None
_snapshot_cache = {}


def _file_signature(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Target(Strict):
    id: str = Field(min_length=1, max_length=80)
    category: Literal['hole', 'screw', 'yellow_clip']
    roi: tuple[float, float, float, float]
    method: Literal['hsv', 'template', 'yolo']
    asset: str | None = None
    class_name: str | None = None
    threshold: float = Field(default=.8, gt=0, le=1)
    hsv_low: tuple[int, int, int] = (18, 90, 70)
    hsv_high: tuple[int, int, int] = (40, 255, 255)
    min_area: float = Field(default=.08, gt=0, le=1)
    max_area: float = Field(default=.8, gt=0, le=1)

    @model_validator(mode='after')
    def valid(self):
        x1, y1, x2, y2 = self.roi
        if not 0 <= x1 < x2 <= 1 or not 0 <= y1 < y2 <= 1:
            raise ValueError('ROI必须是归一化xyxy非空矩形')
        if self.method == 'hsv' and self.category != 'yellow_clip':
            raise ValueError('HSV基线仅用于黄色卡扣候选')
        if self.method != 'hsv' and not self.asset:
            raise ValueError('模板或YOLO必须配置资源文件')
        if self.method == 'yolo' and not self.class_name:
            raise ValueError('YOLO必须配置训练类别名称')
        if self.min_area > self.max_area:
            raise ValueError('面积范围无效')
        if any(not 0 <= a <= b <= limit for a,b,limit in zip(self.hsv_low,self.hsv_high,[179,255,255])):
            raise ValueError('HSV范围无效')
        return self


class Check(Strict):
    id: str
    name: str
    kind: Literal['presence', 'process']
    expected: dict[str, int] = Field(default_factory=dict)
    targets: list[Target] = Field(default_factory=list, max_length=30)
    required_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid(self):
        if len({t.id for t in self.targets}) != len(self.targets):
            raise ValueError('目标位置编号不能重复')
        if any(k not in {'screw','hole','yellow_clip'} or v < 1 or v > 20 for k,v in self.expected.items()):
            raise ValueError('目标类别或数量无效')
        if self.kind == 'presence' and not self.expected:
            raise ValueError('存在检测必须定义目标数量')
        if self.targets:
            counts = {k:sum(t.category == k for t in self.targets) for k in self.expected}
            if counts != self.expected or any(t.category not in self.expected for t in self.targets):
                raise ValueError('配置位置数量必须与检测需求一致')
            for i,a in enumerate(self.targets):
                for b in self.targets[i+1:]:
                    if min(a.roi[2],b.roi[2]) > max(a.roi[0],b.roi[0]) and min(a.roi[3],b.roi[3]) > max(a.roi[1],b.roi[1]):
                        raise ValueError('首版各位置ROI不能重叠，避免同一目标重复计数')
        return self


class Station(Strict):
    id: str
    name: str
    version: str
    approved: bool = False
    fixed_fixture: bool = False
    min_sharpness: float = Field(default=30, ge=0)
    checks: list[Check] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def valid(self):
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError('检查项编号重复')
        targets = [t for c in self.checks for t in c.targets]
        if len(targets) > 64 or len({t.asset for t in targets if t.method == 'yolo'}) > 1:
            raise ValueError('每工位最多64个位置且只加载一个专用YOLO模型')
        return self


def asset_path(name):
    path = (ROOT / name).resolve()
    if not any(path.is_relative_to((ROOT / folder).resolve()) for folder in ['models', 'config']):
        raise ValueError('检测资源仅允许位于models或config目录')
    if not path.is_file():
        raise ValueError('检测资源不存在')
    return path


def sha_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def profiles():
    global _profiles_cache
    signature = _file_signature(CONFIG)
    with _cache_lock:
        if _profiles_cache and _profiles_cache[0] == signature:
            return _profiles_cache[1]
        values = json.loads(CONFIG.read_text(encoding='utf-8'))
        stations = [Station.model_validate(v) for v in values]
        if len({s.id for s in stations}) != len(stations):
            raise ValueError('工位编号重复')
        _profiles_cache = (signature, stations)
        _snapshot_cache.clear()
        return stations


def snapshot(station_id):
    station = next((s for s in profiles() if s.id == station_id), None)
    if station is None:
        raise ValueError('未知工位')
    config_signature = _profiles_cache[0]
    asset_names = tuple(sorted({t.asset for c in station.checks for t in c.targets if t.asset}))
    # stat() is intentionally cheap and lets a running service notice a
    # replaced resource without hashing every frame.
    asset_signature = tuple((name, _file_signature(asset_path(name))) for name in asset_names)
    cache_key = (config_signature, asset_signature)
    with _cache_lock:
        cached = _snapshot_cache.get(station_id)
        if cached and cached[0] == cache_key:
            return copy.deepcopy(cached[1])
    config = station.model_dump(mode='json')
    assets = {name: sha_file(asset_path(name)) for name in asset_names}
    encoded = json.dumps([config, assets], sort_keys=True, ensure_ascii=False).encode()
    frozen = {'config':config, 'assets':assets, 'sha256':hashlib.sha256(encoded).hexdigest()}
    with _cache_lock:
        _snapshot_cache[station_id] = (cache_key, frozen)
    return copy.deepcopy(frozen)


def verify_assets(frozen):
    for name, sha in frozen['assets'].items():
        if sha_file(asset_path(name)) != sha:
            raise ValueError('任务检测资源已改变，请重新创建任务')
