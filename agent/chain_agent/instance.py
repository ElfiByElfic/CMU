"""Jedna instance aplikace: proces + komunikace s jejím povinným REST API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .models import APP_STATES, AppSpec, InstanceState, utcnow

log = logging.getLogger(__name__)


class StartError(Exception):
    pass


@dataclass
class Launch:
    local_id: str          # chain-a.app2 – ID v API agenta
    global_id: str         # vm-prod-02/chain-a/app2 – CHAIN_INSTANCE_ID
    chain_id: str
    position: int
    spec: AppSpec          # port je už přidělený
    executable: Path
    args: list[str]
    upstream_url: str
    downstream_url: str
    data_dir: Path
    log_file: Path
    config_file: Path
    config_format: str
    config_url: str
    token: str             # společný token agent ↔ instance
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def port(self) -> int:
        assert self.spec.port is not None
        return self.spec.port


class AppInstance:
    def __init__(
        self,
        launch: Launch,
        client: httpx.AsyncClient,
        on_exit: Callable[[AppInstance, int], None],
    ) -> None:
        self.l = launch
        self._client = client
        self._on_exit = on_exit
        self._proc: asyncio.subprocess.Process | None = None
        self._waiter: asyncio.Task[None] | None = None
        self.state = InstanceState.stopped
        self.ready = False
        self.starts = 0
        self.last_exit_code: int | None = None
        self.last_status: dict[str, Any] = {}
        self.last_polled_at: datetime | None = None
        self.started_at: datetime | None = None
        self.config_version: str | None = None          # verze aktuálně vyrenderované konfigurace
        self.started_config_version: str | None = None  # verze, se kterou proces startoval
        self._stop_requested = False
        self._poll_failures = 0

    # ------------------------------------------------------------------ vlastnosti
    @property
    def id(self) -> str:
        return self.l.local_id

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def restarts(self) -> int:
        return max(0, self.starts - 1)

    @property
    def config_pending(self) -> bool:
        """Konfigurace se změnila od startu procesu (je potřeba reload nebo restart)."""
        return self.running and self.config_version != self.started_config_version

    # ------------------------------------------------------------------ proces
    def environment(self) -> dict[str, str]:
        """Spouštěcí kontrakt – viz README, kapitola Spouštěcí kontrakt."""
        env = os.environ.copy()
        env.update(self.l.extra_env)
        env.update({
            "CHAIN_INSTANCE_ID": self.l.global_id,
            "CHAIN_ID": self.l.chain_id,
            "CHAIN_POSITION": str(self.l.position),
            "CHAIN_UPSTREAM_URL": self.l.upstream_url,
            "CHAIN_DOWNSTREAM_URL": self.l.downstream_url,
            "APP_BIND_ADDRESS": "127.0.0.1",
            "APP_PORT": str(self.l.port),
            "APP_API_TOKEN": self.l.token,
            "APP_CONFIG_FILE": str(self.l.config_file),
            "APP_CONFIG_FORMAT": self.l.config_format,
            "APP_CONFIG_URL": self.l.config_url,
            "APP_DATA_DIR": str(self.l.data_dir),
        })
        return env

    async def start(self) -> None:
        if self.running:
            return
        self.l.data_dir.mkdir(parents=True, exist_ok=True)
        self.l.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.l.log_file, "ab") as logf:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    str(self.l.executable), *self.l.args,
                    cwd=self.l.data_dir, env=self.environment(),
                    stdin=asyncio.subprocess.DEVNULL, stdout=logf, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,  # vlastní skupina procesů – signály agenta ji nezasáhnou
                )
            except OSError as e:
                self.state = InstanceState.error
                raise StartError(f"{self.id}: {e}") from e
        self._stop_requested = False
        self.ready = False
        self._poll_failures = 0
        self.last_status = {}
        self.last_exit_code = None
        self.started_config_version = self.config_version
        self.started_at = utcnow()
        self.starts += 1
        self.state = InstanceState.starting
        self._waiter = asyncio.create_task(self._wait(self._proc))
        log.info("%s: spuštěn (pid %s, port %s)", self.id, self._proc.pid, self.l.port)

    async def _wait(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        self.ready = False
        self.last_exit_code = code
        if self._stop_requested:
            self.state = InstanceState.stopped
            return
        log.warning("%s: neočekávaně skončil s kódem %s", self.id, code)
        self.state = InstanceState.error
        self._on_exit(self, code)

    async def stop(self, timeout_s: float) -> str:
        """Řízené zastavení: POST /control/stop → po timeoutu SIGTERM → po 5 s SIGKILL."""
        if not self.running or self._proc is None or self._waiter is None:
            self.ready = False
            self.state = InstanceState.stopped
            return "not running"
        proc, waiter = self._proc, self._waiter
        self._stop_requested = True
        self.ready = False
        self.state = InstanceState.stopping
        with contextlib.suppress(httpx.HTTPError):
            await self._request("POST", "/api/v1/control/stop")
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout_s)
            return "stopped"
        except asyncio.TimeoutError:
            log.warning("%s: nezastavil se včas, posílám SIGTERM", self.id)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), 5)
            return "terminated (SIGTERM)"
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await waiter
        return "killed (SIGKILL)"

    async def kill_now(self) -> None:
        """Rychlé ukončení při vypínání agenta."""
        if not self.running or self._proc is None:
            return
        self._stop_requested = True
        with contextlib.suppress(ProcessLookupError):
            self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), 3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()

    # ------------------------------------------------------------------ REST API aplikace
    async def _request(self, method: str, path: str, json: Any = None) -> httpx.Response:
        return await self._client.request(
            method, f"http://127.0.0.1:{self.l.port}{path}", json=json,
            headers={"Authorization": f"Bearer {self.l.token}"},
        )

    async def poll(self) -> None:
        """GET /health/ready a /status; aktualizuje ready a state."""
        if not self.running or self.state == InstanceState.stopping:
            return
        try:
            r = await self._request("GET", "/api/v1/health/ready")
        except httpx.HTTPError:
            self.ready = False
            # při startu je nedostupnost normální; jinak 3× po sobě = unreachable
            if self.state != InstanceState.starting:
                self._poll_failures += 1
                if self._poll_failures >= 3:
                    self.state = InstanceState.unreachable
            return
        self._poll_failures = 0
        self.ready = r.status_code == 200
        try:
            s = await self._request("GET", "/api/v1/status")
        except httpx.HTTPError:
            return
        if s.status_code != 200:
            return
        with contextlib.suppress(ValueError):
            self.last_status = s.json()
        self.last_polled_at = utcnow()
        if self.running and self.state != InstanceState.stopping:
            self.state = APP_STATES.get(str(self.last_status.get("state")), InstanceState.error)

    async def control(self, action: str) -> tuple[bool, str]:
        if not self.running:
            return False, "not running"
        try:
            r = await self._request("POST", f"/api/v1/control/{action}")
        except httpx.HTTPError as e:
            return False, str(e)
        return r.status_code == 202, f"HTTP {r.status_code}"

    async def patch_config(self, patch: dict[str, Any]) -> tuple[bool, str]:
        """Pošle změnu aplikaci. (True, …) = aplikována za běhu."""
        if not self.running:
            return False, "not running"
        try:
            r = await self._request("PATCH", "/api/v1/config", json=patch)
        except httpx.HTTPError as e:
            return False, str(e)
        if r.status_code == 200:
            return True, "applied at runtime"
        detail = f"HTTP {r.status_code}"
        with contextlib.suppress(ValueError):
            detail = r.json().get("detail", detail)
        return False, detail

    # ------------------------------------------------------------------ výstup
    def tail_log(self, lines: int) -> list[str]:
        try:
            with open(self.l.log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                data = f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return []
        return [ln for ln in data.splitlines() if ln][-lines:]

    def to_json(self) -> dict[str, Any]:
        return {
            "instanceId": self.id,
            "globalId": self.l.global_id,
            "type": self.l.spec.type,
            "version": self.l.spec.version,
            "port": self.l.port,
            "position": self.l.position,
            "state": self.state.value,
            "ready": self.ready,
            "pid": self._proc.pid if self.running and self._proc else None,
            "restarts": self.restarts,
            "lastExitCode": self.last_exit_code,
            "configFormat": self.l.config_format,
            "configVersion": self.config_version,
            "configPending": self.config_pending,
            "lastStatus": self.last_status,
            "lastPolledAt": self.last_polled_at.isoformat() if self.last_polled_at else None,
        }
