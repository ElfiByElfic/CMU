#!/usr/bin/env python3
"""Testovací aplikace: minimální implementace povinného API aplikace (api/app-api.yaml).

Používá jen standardní knihovnu. Parametry:
  --ready-after S   po kolika sekundách začne /health/ready vracet 200 (výchozí 0.3)
  --crash-after S   po S sekundách spadne s kódem 1
  --stop-hangs      ignoruje /control/stop (test SIGTERM)
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

p = argparse.ArgumentParser()
p.add_argument("--ready-after", type=float, default=0.3)
p.add_argument("--crash-after", type=float, default=None)
p.add_argument("--stop-hangs", action="store_true")
args = p.parse_args()

PORT = int(os.environ["APP_PORT"])
TOKEN = os.environ.get("APP_API_TOKEN", "")
CONFIG_FILE = os.environ.get("APP_CONFIG_FILE", "")
STARTED = time.time()
STATE = {"state": "starting", "reloads": 0, "logLevel": "INFO"}


def load_config() -> dict:
    if not CONFIG_FILE:
        return {}
    text = open(CONFIG_FILE, encoding="utf-8").read()
    if CONFIG_FILE.endswith(".ini"):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read_string(text)
        return {s: dict(cp.items(s)) for s in cp.sections()}
    return json.loads(text)


CONFIG = load_config()
print(f"fake app {os.environ.get('CHAIN_INSTANCE_ID')} on :{PORT}, config keys {sorted(CONFIG)}", flush=True)


def exit_later(code: int, delay: float) -> None:
    def go() -> None:
        sys.stdout.flush()
        os._exit(code)
    threading.Timer(delay, go).start()


if args.crash_after is not None:
    exit_later(1, args.crash_after)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # bez access logu
        pass

    def send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        if self.path.startswith("/api/v1/health/"):
            return True
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self.send(401, {"title": "Unauthorized", "status": 401})
        return False

    def ready(self) -> bool:
        ok = time.time() - STARTED >= args.ready_after
        if ok and STATE["state"] == "starting":
            STATE["state"] = "running"
        return ok

    def do_GET(self) -> None:
        if not self.authorized():
            return
        if self.path == "/api/v1/health/live":
            return self.send(200, {"status": "ok"})
        if self.path == "/api/v1/health/ready":
            ok = self.ready()
            return self.send(200 if ok else 503, {"ready": ok, "checks": []})
        if self.path == "/api/v1/status":
            self.ready()
            return self.send(200, {
                "instanceId": os.environ.get("CHAIN_INSTANCE_ID"), "state": STATE["state"],
                "uptimeSec": int(time.time() - STARTED),
                "extra": {"reloads": STATE["reloads"], "logLevel": STATE["logLevel"], "config": CONFIG},
            })
        if self.path == "/api/v1/info":
            return self.send(200, {"instanceId": os.environ.get("CHAIN_INSTANCE_ID"), "pid": os.getpid()})
        self.send(404, {"title": "Not found", "status": 404})

    def do_POST(self) -> None:
        global CONFIG
        if not self.authorized():
            return
        if self.path == "/api/v1/control/stop":
            self.send(202, {"accepted": True, "action": "stop", "state": "stopping"})
            if not args.stop_hangs:
                STATE["state"] = "stopping"
                exit_later(0, 0.1)
            return
        if self.path == "/api/v1/control/reload":
            CONFIG = load_config()
            STATE["reloads"] += 1
            return self.send(202, {"accepted": True, "action": "reload", "state": STATE["state"]})
        if self.path.startswith("/api/v1/control/"):
            return self.send(202, {"accepted": True, "action": self.path.rsplit("/", 1)[1], "state": STATE["state"]})
        self.send(404, {"title": "Not found", "status": 404})

    def do_PATCH(self) -> None:
        if not self.authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if set(body) - {"logLevel"}:
            return self.send(422, {"title": "Key not runtime editable", "status": 422,
                                   "detail": ",".join(sorted(set(body) - {"logLevel"}))})
        STATE["logLevel"] = body.get("logLevel", STATE["logLevel"])
        self.send(200, {"logLevel": {"value": STATE["logLevel"], "runtimeEditable": True}})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
