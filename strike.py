#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strike.py - portable API quota strike harness.

Pure stdlib, Python 3.8+. Nothing to install, nothing to compile.
Run from a USB stick; all state is written next to this file.

Subcommands
  probe      one GET /models -> key alive? how many models?
  map        one tiny request per model -> which models answer 200 vs 429
  strike     sustained load with N workers until budget/duration is spent
  selftest   spin a local stub server, run the real engine against it, verify counters

Examples
  python strike.py probe
  python strike.py map
  python strike.py strike --duration 60 --workers 16 --rpm 600
  python strike.py selftest
"""

import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
RESULTS_PATH = os.path.join(HERE, "results.jsonl")
SUMMARY_PATH = os.path.join(HERE, "summary.json")

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_KEY = ""   # supply with --key, or "key" in config.json / config.local.json

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


# ---------------------------------------------------------------- config

def load_config():
    cfg = {
        "base": DEFAULT_BASE,
        "key": DEFAULT_KEY,
        "models": [],          # empty -> discovered from /models
        "all_models": False,   # False -> skip embedding/tts/image families
        "prompt": "ping",
        "workers": 8,
        "rpm": 0,              # 0 = unlimited
        "duration": 30,        # seconds, strike mode
        "max_requests": 0,     # 0 = unlimited (duration governs)
        "timeout": 30,
        "user_agents": UA_POOL,
        "proxy": "",           # e.g. http://127.0.0.1:8080 ; empty = env/registry
        "retry_5xx": 2,
        "output": {
            "results": RESULTS_PATH,
            "summary": SUMMARY_PATH,
        },
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
                user = json.load(fh)
        except json.JSONDecodeError as exc:
            lines = exc.doc.splitlines() or [""]
            bad = lines[exc.lineno - 1] if 0 < exc.lineno <= len(lines) else ""
            print("config.json is not valid JSON")
            print("  file  : %s" % CONFIG_PATH)
            print("  error : %s (line %d, column %d)" % (exc.msg, exc.lineno, exc.colno))
            print("  line  : %s" % bad.strip())
            print('  hint  : model names need quotes -> "models": ["gemini-2.5-flash"]')
            raise SystemExit(2)
        cfg.update({k: v for k, v in user.items() if k in cfg})
        if isinstance(user.get("output"), dict):
            cfg["output"].update(user["output"])
    for _name in ("results", "summary"):
        _p = cfg["output"].get(_name)
        if _p and not os.path.isabs(_p):
            cfg["output"][_name] = os.path.join(HERE, _p)
    return cfg


def require_key(cfg):
    """Fail fast with an actionable message instead of a bare 403."""
    if not cfg.get("key"):
        print("no api key configured. use:  --key YOUR_KEY   or set \"key\" in config.json")
        return False
    return True


# ---------------------------------------------------------------- http

_OPENERS = {}


def _opener(proxy=None):
    """Cached opener. proxy=None keeps urllib's env-registry proxy discovery."""
    key = proxy or ""
    if key not in _OPENERS:
        if proxy:
            _OPENERS[key] = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            _OPENERS[key] = urllib.request.build_opener()
    return _OPENERS[key]


def _req(url, body=None, timeout=30, ua=None, headers=None, proxy=None):
    """Return (status, text, latency_s). Never raises for HTTP status codes."""
    hdrs = {"accept": "application/json"}
    if ua:
        hdrs["user-agent"] = ua
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    t0 = time.monotonic()
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw.decode("utf-8", "replace"), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return exc.code, raw, time.monotonic() - t0
    except Exception as exc:                      # DNS, TLS, timeout, reset
        return 0, "TRANSPORT:%s" % exc.__class__.__name__, time.monotonic() - t0


def list_models(cfg, timeout=None):
    url = "%s/models?key=%s" % (cfg["base"], urllib.parse.quote(cfg["key"]))
    status, text, dt = _req(url, timeout=timeout or cfg["timeout"],
                            ua=random.choice(cfg["user_agents"]),
                            proxy=cfg.get("proxy") or None)
    models = []
    if status == 200:
        try:
            models = [m["name"].split("/", 1)[-1] for m in json.loads(text).get("models", [])]
        except Exception:
            models = []
    return status, text, models, dt


def gen_once(cfg, model, prompt, timeout=None):
    url = "%s/models/%s:generateContent?key=%s" % (
        cfg["base"], model, urllib.parse.quote(cfg["key"]))
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    return _req(url, body=body, timeout=timeout or cfg["timeout"],
                ua=random.choice(cfg["user_agents"]),
                proxy=cfg.get("proxy") or None)


def classify(status, text):
    """Coarse bucket so the summary is readable."""
    if status == 200:
        return "ok"
    if status == 429:
        return "quota"
    if status == 403:
        if "API_KEY_INVALID" in text or "API key not valid" in text:
            return "key_revoked"
        return "forbidden"
    if status == 404:
        return "no_model"
    if status == 400:
        return "bad_request"
    if status == 0:
        return "transport"
    if 500 <= status < 600:
        return "server_error"
    return "other"


# ---------------------------------------------------------------- stats

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.buckets = {}
        self.codes = {}
        self.sent = 0
        self.lat = []
        self.first_quota = None
        self.first_quota_at_sent = None
        self.stop = threading.Event()
        self.t0 = time.monotonic()

    def add(self, status, text, dt):
        bucket = classify(status, text)
        with self.lock:
            self.sent += 1
            self.buckets[bucket] = self.buckets.get(bucket, 0) + 1
            self.codes[str(status)] = self.codes.get(str(status), 0) + 1
            if len(self.lat) < 20000:
                self.lat.append(dt)
            if bucket == "quota" and self.first_quota is None:
                self.first_quota = time.monotonic() - self.t0
                self.first_quota_at_sent = self.sent
            if bucket == "key_revoked":
                self.stop.set()
        return bucket

    def elapsed(self):
        return time.monotonic() - self.t0

    def snapshot(self):
        with self.lock:
            lat = sorted(self.lat) if self.lat else [0.0]
            return {
                "sent": self.sent,
                "elapsed_s": round(self.elapsed(), 2),
                "rps": round(self.sent / max(self.elapsed(), 0.001), 2),
                "buckets": dict(self.buckets),
                "http_codes": dict(self.codes),
                "first_quota_at_s": None if self.first_quota is None else round(self.first_quota, 2),
                "first_quota_after_requests": self.first_quota_at_sent,
                "latency_ms": {
                    "p50": round(lat[len(lat) // 2] * 1000, 1),
                    "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000, 1),
                    "max": round(lat[-1] * 1000, 1),
                },
                "key_still_valid": "key_revoked" not in self.buckets,
            }


class Budget:
    """Exact request budget shared by all workers (no overshoot from races)."""

    def __init__(self, limit):
        self.limit = int(limit or 0)
        self.lock = threading.Lock()
        self.used = 0

    def take(self):
        if not self.limit:
            return True
        with self.lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


# families that appear in /models but do not serve generateContent
NON_TEXT_MODEL_HINTS = (
    "embedding", "tts", "image", "imagen", "lyria", "aqa", "veo",
    "computer-use", "robotics", "deep-research", "antigravity",
)


def text_models(models):
    """Drop families whose :generateContent is 400/404 by design."""
    return [m for m in models
            if not any(h in m.lower() for h in NON_TEXT_MODEL_HINTS)]


class Rate:
    """Token bucket by fixed interval; rpm<=0 disables pacing."""

    def __init__(self, rpm):
        self.interval = (60.0 / rpm) if rpm and rpm > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = time.monotonic()

    def take(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            if self.next_at < now:
                self.next_at = now
            slot = self.next_at
            self.next_at += self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Sink:
    """Append-only jsonl writer; flushes every N records so a yanked USB loses little."""

    def __init__(self, path, flush_every=20):
        self.path = path
        self.flush_every = flush_every
        self.lock = threading.Lock()
        self.buf = []
        self.fh = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, rec):
        line = json.dumps(rec, ensure_ascii=False)
        with self.lock:
            self.buf.append(line)
            if len(self.buf) >= self.flush_every:
                self.fh.write("\n".join(self.buf) + "\n")
                self.fh.flush()
                os.fsync(self.fh.fileno())
                self.buf = []

    def close(self):
        with self.lock:
            if self.buf:
                self.fh.write("\n".join(self.buf) + "\n")
                self.buf = []
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()


# ---------------------------------------------------------------- modes

def cmd_probe(cfg):
    if not require_key(cfg):
        return 2
    status, text, models, dt = list_models(cfg)
    print("GET /models -> HTTP %s in %.0f ms" % (status, dt * 1000))
    if status != 200:
        snippet = text.strip().replace("\n", " ")[:300]
        print("body: %s" % snippet)
        return 1 if status else 2
    print("models visible: %d" % len(models))
    for m in models[:15]:
        print("  - %s" % m)
    if len(models) > 15:
        print("  ... (+%d more)" % (len(models) - 15))
    return 0


def cmd_map(cfg):
    if not require_key(cfg):
        return 2
    status, text, models, dt = list_models(cfg)
    if status != 200:
        print("cannot enumerate models: HTTP %s %s" % (status, text[:200]))
        return 1
    targets = cfg["models"] or (models if cfg.get("all_models") else text_models(models))
    print("mapping %d models with one minimal request each" % len(targets))
    rows = []
    for m in targets:
        st, body, lat = gen_once(cfg, m, cfg["prompt"])
        bucket = classify(st, body)
        rows.append((m, st, bucket, lat))
        print("  %-52s %3s %-12s %5.0f ms" % (m, st, bucket, lat * 1000))
        if bucket == "key_revoked":
            print("key rejected - stopping")
            break
        time.sleep(0.15)
    ok = [r for r in rows if r[2] == "ok"]
    quota = [r for r in rows if r[2] == "quota"]
    print("\nsummary: ok=%d quota=%d other=%d" % (len(ok), len(quota), len(rows) - len(ok) - len(quota)))
    if quota and not ok:
        print("all probed models already rate-limited")
    return 0


def cmd_strike(cfg, duration=None, workers=None, rpm=None, max_requests=None):
    if not require_key(cfg):
        return 2
    duration = cfg["duration"] if duration is None else duration
    workers = cfg["workers"] if workers is None else workers
    rpm = cfg["rpm"] if rpm is None else rpm
    max_requests = cfg["max_requests"] if max_requests is None else max_requests

    status, text, models, _ = list_models(cfg)
    targets = cfg["models"] or (models if cfg.get("all_models") else text_models(models))
    if not targets:
        print("no models to hit: /models returned HTTP %s" % status)
        print(text[:300])
        return 1

    stats = Stats()
    rate = Rate(rpm)
    budget = Budget(max_requests)
    sink = Sink(cfg["output"]["results"])
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-%04x" % random.getrandbits(16)
    dead = set()                       # models that answered 404 (retired for this key)
    dead_lock = threading.Lock()
    flunk = {}                         # model -> consecutive transport failures
    FLUNK_LIMIT = 3
    deadline = time.monotonic() + duration if duration > 0 else float("inf")

    print("strike: models=%d workers=%d rpm=%s duration=%ss budget=%s run=%s" % (
        len(targets), workers, rpm or "unlimited", duration or "none",
        max_requests or "unlimited", run_id))
    print("results -> %s" % cfg["output"]["results"])

    def worker(idx):
        i = idx
        while not stats.stop.is_set() and time.monotonic() < deadline:
            if not budget.take():
                stats.stop.set()
                break
            model = targets[i % len(targets)]
            i += 1
            if model in dead:
                if len(dead) >= len(targets):
                    stats.stop.set()
                    break
                continue
            rate.take()
            attempts = 0
            while True:
                st, body, dt = gen_once(cfg, model, cfg["prompt"])
                bucket = stats.add(st, body, dt)
                sink.write({
                    "ts": round(time.time(), 3),
                    "run": run_id,
                    "t_rel": round(stats.elapsed(), 3),
                    "model": model,
                    "status": st,
                    "bucket": bucket,
                    "latency_ms": round(dt * 1000, 1),
                    "err": None if st == 200 else body[:200],
                })
                if bucket == "no_model":
                    with dead_lock:
                        dead.add(model)
                    break
                if bucket == "transport":
                    with dead_lock:
                        flunk[model] = flunk.get(model, 0) + 1
                        if flunk[model] >= FLUNK_LIMIT:
                            dead.add(model)
                    if model in dead:
                        break
                if 500 <= st < 600 and attempts < cfg["retry_5xx"]:
                    attempts += 1
                    time.sleep(0.5 * attempts)
                    continue
                break

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(worker, k) for k in range(max(1, workers))]
        last = 0
        while any(not f.done() for f in futures):
            time.sleep(1.0)
            now = time.monotonic()
            if now - last >= 2:
                last = now
                snap = stats.snapshot()
                print("  t=%5.0fs sent=%6d rps=%6.2f ok=%d quota=%d dead=%d other=%d" % (
                    snap["elapsed_s"], snap["sent"], snap["rps"],
                    snap["buckets"].get("ok", 0), snap["buckets"].get("quota", 0),
                    len(dead),
                    snap["sent"] - snap["buckets"].get("ok", 0) - snap["buckets"].get("quota", 0)))
            if stats.stop.is_set():
                break
        for f in futures:
            f.cancel()

    sink.close()
    snap = stats.snapshot()
    snap["run"] = run_id
    snap["targets"] = targets
    snap["dropped_models"] = sorted(dead)
    snap["config"] = {"workers": workers, "rpm": rpm, "duration": duration,
                      "budget": max_requests}
    with open(cfg["output"]["summary"], "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, ensure_ascii=False)
    print("\n" + json.dumps(snap, indent=2, ensure_ascii=False))
    print("\nsummary -> %s" % cfg["output"]["summary"])
    return 0


# ---------------------------------------------------------------- selftest

def cmd_selftest(cfg):
    """Local stub: first N requests 200, then 429. Proves the engine without touching the net."""
    import http.server

    state = {"n": 0, "threshold": 25, "lock": threading.Lock()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self._send(200, {"models": [{"name": "models/stub-a"}, {"name": "models/stub-b"}]})

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            with state["lock"]:
                state["n"] += 1
                n = state["n"]
            if n <= state["threshold"]:
                self._send(200, {"candidates": [{"content": {"parts": [{"text": "pong"}]}}]})
            else:
                self._send(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                           "message": "stub quota exceeded"}})

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    test_cfg = dict(cfg)
    test_cfg["base"] = "http://127.0.0.1:%d/v1beta" % port
    test_cfg["key"] = "stub-key-not-a-real-credential"
    test_cfg["models"] = []
    test_cfg["duration"] = 6
    test_cfg["workers"] = 8
    test_cfg["rpm"] = 0
    test_cfg["max_requests"] = 60
    test_cfg["output"] = {
        "results": os.path.join(HERE, "selftest.results.jsonl"),
        "summary": os.path.join(HERE, "selftest.summary.json"),
    }

    rc = cmd_strike(test_cfg, duration=6, workers=8, rpm=0, max_requests=60)
    srv.shutdown()

    if rc != 0:
        print("SELFTEST FAIL: strike returned %d" % rc)
        return 1
    with open(test_cfg["output"]["summary"], "r", encoding="utf-8") as fh:
        snap = json.load(fh)
    ok = snap["buckets"].get("ok", 0)
    quota = snap["buckets"].get("quota", 0)
    checks = [
        ("engine sent requests", snap["sent"] >= 40),
        ("200s counted", ok >= 20),
        ("429s counted", quota >= 5),
        ("first 429 recorded", snap["first_quota_at_s"] is not None),
        ("key still valid flag", snap["key_still_valid"] is True),
    ]
    bad = 0
    for name, passed in checks:
        print("[%s] %s" % ("PASS" if passed else "FAIL", name))
        bad += 0 if passed else 1
    print("\nSELFTEST %s (sent=%d ok=%d quota=%d)" % (
        "PASS" if not bad else "FAIL", snap["sent"], ok, quota))
    return 0 if not bad else 1


# ---------------------------------------------------------------- main

def main(argv):
    import urllib.parse  # noqa: F401  (kept local so callers can stub early)
    ap = argparse.ArgumentParser(description="portable API quota strike harness")
    ap.add_argument("mode", choices=["probe", "map", "strike", "selftest"])
    ap.add_argument("--duration", type=float, default=None, help="seconds, strike mode")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--rpm", type=int, default=None, help="requests per minute cap, 0=off")
    ap.add_argument("--budget", type=int, default=None, help="max total requests")
    ap.add_argument("--key", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--proxy", default=None, help="http://host:port ; overrides config")
    ap.add_argument("--all-models", action="store_true",
                    help="also hit embedding/tts/image families (mostly 400/404)")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.key:
        cfg["key"] = args.key
    if args.base:
        cfg["base"] = args.base
    if args.proxy:
        cfg["proxy"] = args.proxy
    if args.all_models:
        cfg["all_models"] = True

    if args.mode == "probe":
        return cmd_probe(cfg)
    if args.mode == "map":
        return cmd_map(cfg)
    if args.mode == "strike":
        return cmd_strike(cfg, args.duration, args.workers, args.rpm, args.budget)
    return cmd_selftest(cfg)


if __name__ == "__main__":
    import urllib.parse  # real import for the request builders
    sys.exit(main(sys.argv[1:]))
