"""Společné testovací prostředí: katalog s testovací aplikací a konfigurace agenta."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from chain_agent.api import create_app
from chain_agent.config import AgentConfig

FAKE_APP = Path(__file__).with_name("fake_app.py")
CONTROL = "test-control-token"
READ = "test-read-token"


def _free_port_range(size: int = 60) -> tuple[int, int]:
    """Najde souvislý rozsah volných portů (testy mohou běžet souběžně s jinými službami)."""
    for start in range(21000, 60000, 500):
        ok = True
        for p in range(start, start + size):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    ok = False
                    break
        if ok:
            return start, start + size - 1
    raise RuntimeError("žádný volný rozsah portů")


def _install(catalog: Path, type_: str, version: str, args: list[str] | None = None,
             fmt: str = "json", default: str | None = None, script: str | None = None) -> None:
    d = catalog / type_ / version
    (d / "bin").mkdir(parents=True)
    exe = d / "bin" / type_
    body = script or f'exec "{sys.executable}" "{FAKE_APP}" "$@"\n'
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    manifest: dict[str, Any] = {
        "type": type_, "version": version, "executable": f"bin/{type_}",
        "args": args or [], "configFormat": fmt,
    }
    if default is not None:
        name = f"default.{fmt}"
        (d / name).write_text(default)
        manifest["defaultConfig"] = name
    (d / "manifest.json").write_text(json.dumps(manifest))


class AgentEnv:
    """Dočasný adresář s katalogem, tajnými hodnotami a konfigurací agenta."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="chain-agent-test-")
        root = Path(self._tmp.name)
        catalog = root / "catalog"
        _install(catalog, "App1", "1.0.0", fmt="ini",
                 default="[General]\nlogLevel=INFO\n\n[database]\nhost=localhost\npassword=secret://db-password\n")
        _install(catalog, "App2", "1.0.0", default=json.dumps({"logLevel": "INFO", "timeoutMs": 5000}))
        _install(catalog, "App2", "2.0.0-rc1")
        _install(catalog, "App3", "1.0.0")
        _install(catalog, "Crashy", "1.0.0", args=["--ready-after", "0.1", "--crash-after", "1"])
        _install(catalog, "Hangs", "1.0.0", args=["--stop-hangs"])
        _install(catalog, "Broken", "1.0.0", script="echo 'cannot start' >&2\nexit 3\n")
        secrets_dir = root / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "db-password").write_text("s3cret\n")
        os.chmod(secrets_dir / "db-password", 0o600)

        self.cfg = AgentConfig(
            vmName="vm-test",
            environment="production",
            listenPort=9100,
            readTokens=[READ],
            controlTokens=[CONTROL],
            catalogDir=catalog,
            stateDir=root / "state",
            logDir=root / "log",
            secretsDir=secrets_dir,
            portRange=_free_port_range(),
            pollIntervalMs=200,
            readyTimeoutMs=5000,
            stopTimeoutMs=1500,
            restartBackoffMaxS=2,
        )

    def client(self, token: str | None = CONTROL) -> TestClient:
        c = TestClient(create_app(self.cfg))
        if token:
            c.headers["Authorization"] = f"Bearer {token}"
        return c

    def cleanup(self) -> None:
        self._tmp.cleanup()


def wait_op(c: TestClient, op: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = c.get(f"/agent/v1/operations/{op['id']}").json()
        if cur["status"] != "running":
            return cur
        time.sleep(0.1)
    raise AssertionError(f"operace {op['id']} neskončila: {cur}")


def wait_for(pred: Any, timeout: float = 15, interval: float = 0.1) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("podmínka nesplněna do timeoutu")


def chain(name: str, *types: str, **kw: Any) -> dict[str, Any]:
    apps = []
    for t in types:
        type_, _, version = t.partition("@")
        apps.append({"type": type_, "version": version or "1.0.0"})
    return {"name": name, "apps": apps, **kw}
