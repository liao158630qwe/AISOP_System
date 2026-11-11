"""Measure a running same-origin API; optional real inference uses supplied imagery.

No production cycles or analysis jobs are created. HTTP transport latency is not
camera-to-display latency and a recorded image is not a live multi-camera test.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlparse
import uuid

import requests

ROOT = Path(__file__).resolve().parents[1]


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0}
    return {
        "samples": len(ordered),
        "meanMs": round(sum(ordered) / len(ordered), 2),
        "p50Ms": round(ordered[math.ceil(len(ordered) * .5) - 1], 2),
        "p95Ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
        "p99Ms": round(ordered[math.ceil(len(ordered) * .99) - 1], 2),
        "maxMs": round(ordered[-1], 2),
    }


def session():
    client = requests.Session()
    client.trust_env = False
    return client


def credentials(base):
    username, password = os.getenv("APP_USERNAME"), os.getenv("APP_PASSWORD")
    if password:
        return {"username": username or "admin", "password": password}
    if urlparse(base).hostname in ("localhost", "127.0.0.1", "::1"):
        return json.loads((ROOT / ".runtime/local-settings.json").read_text(encoding="utf-8"))
    raise RuntimeError("Set APP_USERNAME and APP_PASSWORD for a remote API")


def authenticate(client, base, login):
    response = client.post(base + "/api/v1/login", json=login, timeout=10)
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = response.json()["csrf"]


def health_probe(base, count):
    results = []
    with session() as client:
        for _ in range(count):
            started = time.perf_counter()
            try:
                response = client.get(base + "/api/v1/health", timeout=5)
                ready = response.status_code == 200 and response.json().get("status") == "ready"
                status = "ready" if ready else str(response.status_code)
            except (requests.RequestException, ValueError):
                status = "network_error"
            results.append({"status": status, "ms": (time.perf_counter() - started) * 1000})
    return results


def inference_probe(base, login, task, payload, count, stream_index):
    samples, identity = [], str(uuid.uuid4())
    with session() as client:
        authenticate(client, base, login)
        try:
            for index in range(count + 1):
                started = time.perf_counter()
                try:
                    response = client.post(base + "/api/v1/realtime/frame?analysis_type=" + task,
                        data=payload, headers={"Content-Type": "image/jpeg", "X-Stream-Id": identity,
                            "X-Frame-Time": str(index * .25)}, timeout=60)
                    body = response.json()
                except (requests.RequestException, ValueError) as error:
                    samples.append({"stream": stream_index, "index": index, "warmup": index == 0,
                        "status": "network_error", "error": type(error).__name__,
                        "httpMs": round((time.perf_counter() - started) * 1000, 2)})
                    break
                elapsed = (time.perf_counter() - started) * 1000
                samples.append({"stream": stream_index, "index": index, "warmup": index == 0,
                    "status": response.status_code, "httpMs": round(elapsed, 2),
                    "processingMs": body.get("processingMs"), "stagesMs": body.get("timingsMs"),
                    "serverTiming": response.headers.get("Server-Timing"),
                    "retryAfter": response.headers.get("Retry-After")})
                if response.status_code == 429:
                    time.sleep(min(3, float(response.headers.get("Retry-After", "1"))))
                elif response.status_code != 200:
                    break
        finally:
            try:
                client.delete(base + "/api/v1/realtime/streams/" + identity, timeout=5)
            except requests.RequestException:
                pass
            try:
                client.post(base + "/api/v1/logout", timeout=5)
            except requests.RequestException:
                pass
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--clients", type=int, default=15)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--task", choices=("pose", "screw", "sticker"), default="pose")
    parser.add_argument("--streams", type=int, default=1)
    parser.add_argument("--out", type=Path, default=ROOT / "test-results/service-optimization-20261105/benchmark.json")
    args = parser.parse_args()
    if not 1 <= args.clients <= 64 or not 1 <= args.streams <= 15 or not 1 <= args.samples <= 1000:
        parser.error("clients 1–64, streams 1–15, samples 1–1000")
    base = args.base.rstrip("/")
    report = {"base": base, "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": "HTTP transport and supplied-image inference; not a camera/display or 15-camera acceptance test",
        "clients": args.clients, "inferenceStreams": args.streams if args.image else 0}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.clients + args.streams) as pool:
        inference = []
        if args.image:
            login, payload = credentials(base), args.image.read_bytes()
            inference = [pool.submit(inference_probe, base, login, args.task, payload, args.samples, i)
                         for i in range(args.streams)]
            report.update(task=args.task, image=str(args.image.resolve()))
        health = [pool.submit(health_probe, base, args.samples) for _ in range(args.clients)]
        health_rows = [row for future in health for row in future.result()]
        inference_rows = []
        for index, future in enumerate(inference):
            try:
                inference_rows.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                inference_rows.append({'stream': index, 'index': -1, 'warmup': False,
                    'status': 'setup_error', 'error': type(error).__name__})
    report["elapsedSeconds"] = round(time.perf_counter() - started, 2)
    report["health"] = {"statuses": dict(Counter(row["status"] for row in health_rows)),
        **distribution([row["ms"] for row in health_rows if row["status"] == "ready"])}
    if inference_rows:
        steady = [row for row in inference_rows if not row["warmup"] and row["status"] == 200]
        report["inference"] = {"statuses": dict(Counter(row["status"] for row in inference_rows)),
            "http": distribution([row["httpMs"] for row in steady]),
            "processing": distribution([row["processingMs"] for row in steady if row["processingMs"] is not None]),
            "rows": inference_rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "inference"}, ensure_ascii=False, indent=2))
    if inference_rows:
        print(json.dumps({k: v for k, v in report["inference"].items() if k != "rows"}, indent=2))
    print("Report:", args.out)
    successful_streams = {row['stream'] for row in inference_rows if not row['warmup'] and row['status'] == 200}
    inference_ok = not args.image or len(successful_streams) == args.streams
    return 0 if all(row["status"] == "ready" for row in health_rows) and all(row["status"] in (200, 429) for row in inference_rows) and inference_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
