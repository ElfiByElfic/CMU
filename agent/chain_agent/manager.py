"""Správa řetězců: požadovaný stav (chains.json), validace, porty, konfigurace,
spouštění odzadu / zastavování odpředu a restart po pádu."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import appconfig
from .catalog import Catalog
from .config import AgentConfig
from .instance import AppInstance, Launch, StartError
from .models import ChainSpec, ChainState, InstanceState, Operation, ValidationResult, utcnow
from .operations import Operations

log = logging.getLogger(__name__)

CHAIN_ACTIONS = ("start", "stop", "restart")
APP_ACTIONS = ("start", "stop", "restart", "drain", "pause", "resume", "reload")


class NotFound(Exception):
    pass


class Busy(Exception):
    def __init__(self, op_id: str) -> None:
        super().__init__(op_id)
        self.op_id = op_id


class Invalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class UnknownAction(Exception):
    pass


@dataclass
class Runtime:
    spec: ChainSpec
    desired_running: bool = False
    apps: list[AppInstance] = field(default_factory=list)
    busy_op: str | None = None


def merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """JSON Merge Patch (RFC 7386): null klíč maže, objekty se slučují."""
    out = dict(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_patch(out[k], v)
        else:
            out[k] = v
    return out


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class ChainManager:
    def __init__(self, cfg: AgentConfig, catalog: Catalog, ops: Operations) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.ops = ops
        self.chains: dict[str, Runtime] = {}
        self._client: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._poll_task: asyncio.Task[None] | None = None
        self._crash_streak: dict[str, int] = {}

    # ================================================================ životní cyklus
    async def startup(self) -> None:
        self.catalog.rescan()
        self.load()
        self._poll_task = asyncio.create_task(self._poll_loop())
        for chain_id, rt in self.chains.items():
            if rt.desired_running and rt.spec.autostart:
                self.chain_action(chain_id, "start")

    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(
            *(a.kill_now() for rt in self.chains.values() for a in rt.apps), return_exceptions=True
        )
        if self._client:
            await self._client.aclose()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.pollIntervalMs / 1000)
            apps = [a for rt in self.chains.values() for a in rt.apps]
            await asyncio.gather(*(a.poll() for a in apps), return_exceptions=True)

    # ================================================================ persistence
    @property
    def _state_file(self) -> Path:
        return self.cfg.stateDir / "chains.json"

    def save(self) -> None:
        self.cfg.stateDir.mkdir(parents=True, exist_ok=True)
        data = [
            {"spec": rt.spec.model_dump(mode="json"), "desiredRunning": rt.desired_running}
            for rt in self.chains.values()
        ]
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state_file)

    def load(self) -> None:
        if not self._state_file.exists():
            return
        for item in json.loads(self._state_file.read_text(encoding="utf-8")):
            spec = ChainSpec.model_validate(item["spec"])
            assert spec.id
            _, val = self.validate(spec, replacing_id=spec.id)
            if not val.valid:
                log.warning("řetězec %s po načtení není platný: %s", spec.id, "; ".join(val.errors))
            rt = Runtime(spec=spec, desired_running=bool(item.get("desiredRunning")))
            self.chains[spec.id] = rt
            self._build_instances(rt)

    # ================================================================ validace
    def validate(self, spec: ChainSpec, replacing_id: str | None = None) -> tuple[ChainSpec, ValidationResult]:
        """Vrátí kopii specifikace s doplněným id a porty a výsledek validace."""
        spec = spec.model_copy(deep=True)
        errors: list[str] = []
        warnings: list[str] = []

        if spec.id is None:
            while True:
                spec.id = f"chain-{uuid.uuid4().hex[:6]}"
                if spec.id not in self.chains:
                    break
        elif spec.id != replacing_id and spec.id in self.chains:
            errors.append(f"řetězec s id {spec.id} už existuje")

        spec.name = spec.name.strip()
        if not spec.name:
            errors.append("chybí název řetězce")
        for cid, rt in self.chains.items():
            if cid != replacing_id and rt.spec.name.casefold() == spec.name.casefold():
                errors.append(f"název '{spec.name}' už na VM používá řetězec {cid}")

        if len(spec.apps) < 2:
            errors.append("řetězec potřebuje alespoň 2 aplikace")

        lo, hi = self.cfg.portRange
        used = {a.port for cid, rt in self.chains.items() if cid != replacing_id for a in rt.spec.apps}
        types: set[str] = set()
        for a in spec.apps:
            if a.type in types:
                errors.append(f"{a.type} je v řetězci dvakrát")
            types.add(a.type)
            if not self.catalog.has(a.type, a.version):
                errors.append(f"{a.type} {a.version} není v katalogu VM")
            if self.cfg.environment == "production" and "-" in a.version:
                warnings.append(f"{a.type} {a.version} je předběžná verze na produkční VM")
            if a.port is not None:
                if not lo <= a.port <= hi:
                    errors.append(f"port {a.port} mimo rozsah {lo}–{hi}")
                elif a.port in used:
                    errors.append(f"port {a.port} je obsazený jiným řetězcem")
                used.add(a.port)
            missing = [
                n for n in appconfig.secret_refs(a.config) if not (self.cfg.secretsDir / n).is_file()
            ]
            if missing:
                errors.append(f"{a.type}: chybí tajné hodnoty {', '.join(missing)}")

        for a in spec.apps:
            if a.port is None:
                p = lo
                while p <= hi and (p in used or not port_free(p)):
                    p += 1
                if p > hi:
                    errors.append("došly volné porty")
                    break
                a.port = p
                used.add(p)

        result = ValidationResult(
            valid=not errors, errors=errors, warnings=warnings,
            assignedPorts=[a.port for a in spec.apps if a.port is not None],
        )
        return spec, result

    # ================================================================ instance a konfigurace
    def _build_instances(self, rt: Runtime) -> None:
        spec = rt.spec
        assert spec.id
        for i, a in enumerate(spec.apps):
            slug = a.type.lower()
            local_id = f"{spec.id}.{slug}"
            data_dir = self.cfg.stateDir / "instances" / local_id
            launch = Launch(
                local_id=local_id,
                global_id=f"{self.cfg.vmName}/{spec.id}/{slug}",
                chain_id=spec.id,
                position=i,
                spec=a,
                executable=Path(),
                args=[],
                upstream_url=f"http://127.0.0.1:{spec.apps[i - 1].port}" if i > 0 else "",
                downstream_url=f"http://127.0.0.1:{spec.apps[i + 1].port}" if i + 1 < len(spec.apps) else "",
                data_dir=data_dir,
                log_file=self.cfg.logDir / f"{local_id}.log",
                config_file=data_dir / "config.json",
                config_format="json",
                config_url=f"{self.cfg.agent_url}/agent/v1/apps/{local_id}/config",
                token=secrets.token_urlsafe(24),
            )
            inst = AppInstance(launch, self._http, self._on_exit)
            self._refresh_from_catalog(inst)
            rt.apps.append(inst)

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:  # mimo startup() (např. v testech validace)
            self._client = httpx.AsyncClient(timeout=5.0, trust_env=False)
        return self._client

    def _refresh_from_catalog(self, inst: AppInstance) -> bool:
        entry = self.catalog.get(inst.l.spec.type, inst.l.spec.version)
        if entry is None:
            return False
        inst.l.executable = entry.executable
        inst.l.args = list(entry.args)
        inst.l.config_format = entry.config_format
        inst.l.config_file = inst.l.data_dir / f"config.{entry.config_format}"
        return True

    def _env_layer(self, type_: str) -> dict[str, Any]:
        if self.cfg.envConfigDir is None:
            return {}
        for ext in (".json", ".ini"):
            p = self.cfg.envConfigDir / f"{type_}{ext}"
            if p.is_file():
                return appconfig.load_file(p)
        return {}

    def effective_config(self, inst: AppInstance, resolve_secrets: bool) -> dict[str, Any]:
        """Složená konfigurace instance. resolve_secrets=False → tajné hodnoty zamaskované."""
        entry = self.catalog.get(inst.l.spec.type, inst.l.spec.version)
        layers = [
            entry.default_config if entry else {},
            self._env_layer(inst.l.spec.type),
            inst.l.spec.config,
        ]
        runtime = {
            "instanceId": inst.l.global_id,
            "chainId": inst.l.chain_id,
            "position": inst.l.position,
            "bindAddress": "127.0.0.1",
            "port": inst.l.port,
            "upstreamUrl": inst.l.upstream_url,
            "downstreamUrl": inst.l.downstream_url,
        }
        cfg = appconfig.build(layers, runtime)
        return appconfig.resolve_secrets(cfg, self.cfg.secretsDir if resolve_secrets else None)

    def render_config(self, inst: AppInstance, fmt: str, resolve_secrets: bool) -> tuple[str, str]:
        """Vrátí (text, verze). Verze nezávisí na formátu ani na maskování."""
        resolved = self.effective_config(inst, resolve_secrets=True)
        version = appconfig.etag(appconfig.render(resolved, "json"))
        cfg = resolved if resolve_secrets else self.effective_config(inst, resolve_secrets=False)
        return appconfig.render(cfg, fmt), version

    def _write_config(self, inst: AppInstance) -> None:
        text, version = self.render_config(inst, inst.l.config_format, resolve_secrets=True)
        path = inst.l.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # obsahuje tajné hodnoty
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        inst.config_version = version

    async def _start_instance(self, inst: AppInstance) -> None:
        if not self._refresh_from_catalog(inst):
            inst.state = InstanceState.error
            raise StartError(f"{inst.l.spec.type} {inst.l.spec.version} není v katalogu VM")
        try:
            self._write_config(inst)
        except (OSError, ValueError) as e:
            inst.state = InstanceState.error
            raise StartError(f"{inst.id}: konfigurace: {e}") from e
        await inst.start()

    # ================================================================ pád a restart
    def _on_exit(self, inst: AppInstance, code: int) -> None:
        chain_id = inst.l.chain_id
        rt = self.chains.get(chain_id)
        if rt is None or not rt.desired_running or rt.busy_op:
            return
        policy = rt.spec.restartPolicy
        if policy == "never" or (policy == "on-failure" and code == 0):
            return
        ran = (utcnow() - inst.started_at).total_seconds() if inst.started_at else 0
        streak = 0 if ran >= 60 else self._crash_streak.get(inst.id, 0)
        streak += 1
        self._crash_streak[inst.id] = streak
        delay = min(self.cfg.restartBackoffMaxS, 2 ** min(streak, 10))
        log.warning("%s: restart za %s s (pád č. %s)", inst.id, delay, streak)
        self._spawn(self._restart_later(chain_id, inst, delay))

    async def _restart_later(self, chain_id: str, inst: AppInstance, delay: float) -> None:
        await asyncio.sleep(delay)
        rt = self.chains.get(chain_id)
        if rt is None or inst not in rt.apps or not rt.desired_running or inst.running:
            return
        try:
            await self._start_instance(inst)
        except StartError as e:
            log.error("%s", e)

    # ================================================================ sekvence
    async def _wait_ready(self, inst: AppInstance) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.readyTimeoutMs / 1000
        while True:
            await inst.poll()
            if inst.ready:
                return True, "ready"
            if inst.state == InstanceState.error and not inst.running:
                return False, f"proces skončil při startu (kód {inst.last_exit_code})"
            if loop.time() > deadline:
                return False, "nepřipravena do timeoutu (/health/ready)"
            await asyncio.sleep(0.3)

    async def _start_seq(self, rt: Runtime, op: Operation) -> bool:
        """Odzadu: výstup → vstup; další až po /health/ready předchozí."""
        for inst in reversed(rt.apps):
            if inst.ready:
                self.ops.step(op, inst.id, "succeeded", "already running")
                continue
            self.ops.step(op, inst.id, "running", "starting")
            try:
                if not inst.running:
                    await self._start_instance(inst)
            except StartError as e:
                self.ops.step(op, inst.id, "failed", str(e))
                return False
            ok, msg = await self._wait_ready(inst)
            self.ops.step(op, inst.id, "succeeded" if ok else "failed", msg)
            if not ok:
                return False
        return True

    async def _stop_seq(self, rt: Runtime, op: Operation) -> bool:
        """Odpředu: vstup → výstup; každá přes /control/stop (drain)."""
        for inst in rt.apps:
            self.ops.step(op, inst.id, "running", "stopping")
            msg = await inst.stop(self.cfg.stopTimeoutMs / 1000)
            self.ops.step(op, inst.id, "succeeded", msg)
        return True

    def _begin(self, chain_id: str, kind: str) -> tuple[Runtime, Operation]:
        rt = self.chains.get(chain_id)
        if rt is None:
            raise NotFound(chain_id)
        if rt.busy_op:
            raise Busy(rt.busy_op)
        op = self.ops.begin(kind, chain_id)
        rt.busy_op = op.id
        return rt, op

    def _end(self, rt: Runtime, op: Operation, ok: bool, message: str) -> None:
        rt.busy_op = None
        self.ops.finish(op, ok, message)

    # ================================================================ akce nad řetězci
    def create(self, spec: ChainSpec, start: bool) -> tuple[str, Operation | None]:
        spec, val = self.validate(spec)
        if not val.valid:
            raise Invalid(val.errors)
        assert spec.id
        rt = Runtime(spec=spec, desired_running=start)
        self.chains[spec.id] = rt
        self._build_instances(rt)
        self.save()
        return spec.id, (self.chain_action(spec.id, "start") if start else None)

    def update(self, chain_id: str, spec: ChainSpec) -> Operation:
        if chain_id not in self.chains:
            raise NotFound(chain_id)
        spec = spec.model_copy(update={"id": chain_id})
        spec, val = self.validate(spec, replacing_id=chain_id)
        if not val.valid:
            raise Invalid(val.errors)
        rt, op = self._begin(chain_id, "chain.update")

        async def run() -> None:
            await self._stop_seq(rt, op)
            rt.spec = spec
            rt.apps = []
            self._build_instances(rt)
            self.save()
            if not rt.desired_running:
                return self._end(rt, op, True, "updated")
            ok = await self._start_seq(rt, op)
            self._end(rt, op, ok, "updated and started" if ok else "updated, start failed")

        self._spawn(run())
        return op

    def remove(self, chain_id: str) -> Operation:
        rt, op = self._begin(chain_id, "chain.remove")
        rt.desired_running = False

        async def run() -> None:
            await self._stop_seq(rt, op)
            self.chains.pop(chain_id, None)
            self.save()
            self.ops.finish(op, True, "removed")

        self._spawn(run())
        return op

    def chain_action(self, chain_id: str, action: str) -> Operation:
        if action not in CHAIN_ACTIONS:
            raise UnknownAction(action)
        rt, op = self._begin(chain_id, f"chain.{action}")
        rt.desired_running = action != "stop"
        self.save()

        async def run() -> None:
            if action in ("stop", "restart"):
                await self._stop_seq(rt, op)
            if action == "stop":
                return self._end(rt, op, True, "chain stopped")
            ok = await self._start_seq(rt, op)
            self._end(rt, op, ok, "chain running" if ok else "chain start failed")

        self._spawn(run())
        return op

    # ================================================================ akce nad instancemi
    def find_app(self, instance_id: str) -> tuple[Runtime, AppInstance]:
        for rt in self.chains.values():
            for a in rt.apps:
                if a.id == instance_id:
                    return rt, a
        raise NotFound(instance_id)

    def app_action(self, instance_id: str, action: str) -> Operation:
        if action not in APP_ACTIONS:
            raise UnknownAction(action)
        rt, inst = self.find_app(instance_id)
        if rt.busy_op:
            raise Busy(rt.busy_op)
        op = self.ops.begin(f"app.{action}", instance_id)

        async def start() -> tuple[bool, str]:
            try:
                await self._start_instance(inst)
            except StartError as e:
                return False, str(e)
            return await self._wait_ready(inst)

        async def run() -> None:
            if action == "start":
                ok, msg = await start()
            elif action == "stop":
                ok, msg = True, await inst.stop(self.cfg.stopTimeoutMs / 1000)
            elif action == "restart":
                await inst.stop(self.cfg.stopTimeoutMs / 1000)
                ok, msg = await start()
            else:
                if action == "reload":
                    try:
                        self._write_config(inst)  # aplikace si při reloadu načte čerstvou konfiguraci
                    except (OSError, ValueError) as e:
                        return self.ops.finish(op, False, f"konfigurace: {e}")
                ok, msg = await inst.control(action)
                if ok and action == "reload":
                    inst.started_config_version = inst.config_version
            self.ops.finish(op, ok, msg)

        self._spawn(run())
        return op

    async def app_config(self, instance_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Změní vrstvu řetězce v konfiguraci instance (JSON Merge Patch)."""
        rt, inst = self.find_app(instance_id)
        new_config = merge_patch(inst.l.spec.config, patch)
        missing = [n for n in appconfig.secret_refs(new_config) if not (self.cfg.secretsDir / n).is_file()]
        if missing:
            raise Invalid([f"chybí tajné hodnoty {', '.join(missing)}"])
        if appconfig.RUNTIME_SECTION in patch:
            raise Invalid([f"sekci '{appconfig.RUNTIME_SECTION}' nastavuje agent"])

        inst.l.spec.config = new_config  # AppSpec je sdílený s rt.spec → uloží se i do chains.json
        self.save()
        self._write_config(inst)

        if not inst.running:
            return self._config_result(inst, False, "uloženo; použije se při příštím startu")
        applied, msg = await inst.patch_config(patch)
        if applied:
            inst.started_config_version = inst.config_version
            return self._config_result(inst, True, "aplikováno za běhu")
        return self._config_result(inst, False, f"uloženo; vyžaduje restart ({msg})")

    @staticmethod
    def _config_result(inst: AppInstance, applied: bool, message: str) -> dict[str, Any]:
        return {
            "applied": applied,
            "restartRequired": inst.config_pending,
            "configVersion": inst.config_version,
            "message": message,
        }

    # ================================================================ stav
    @staticmethod
    def chain_state(rt: Runtime) -> ChainState:
        states = [a.state for a in rt.apps]
        if all(s == InstanceState.stopped for s in states):
            return ChainState.stopped
        if InstanceState.starting in states:
            return ChainState.starting
        if any(s in (InstanceState.stopping, InstanceState.draining) for s in states):
            return ChainState.stopping
        if any(s in (InstanceState.error, InstanceState.unreachable, InstanceState.stopped) for s in states):
            return ChainState.broken
        if any(s in (InstanceState.degraded, InstanceState.paused) for s in states):
            return ChainState.degraded
        return ChainState.running

    def chain_json(self, chain_id: str) -> dict[str, Any]:
        rt = self.chains.get(chain_id)
        if rt is None:
            raise NotFound(chain_id)
        return {
            "spec": rt.spec.model_dump(mode="json"),
            "state": self.chain_state(rt).value,
            "desiredRunning": rt.desired_running,
            "busyOperation": rt.busy_op,
            "apps": [a.to_json() for a in rt.apps],
        }

    def chains_json(self) -> list[dict[str, Any]]:
        return [self.chain_json(cid) for cid in self.chains]

    def vm_state(self) -> str:
        warn = False
        for rt in self.chains.values():
            s = self.chain_state(rt)
            if s == ChainState.broken or (s == ChainState.stopped and rt.desired_running):
                return "problem"
            if s in (ChainState.degraded, ChainState.starting, ChainState.stopping):
                warn = True
            if any(a.config_pending for a in rt.apps):
                warn = True
        return "warning" if warn else "ok"


__all__ = ["ChainManager", "NotFound", "Busy", "Invalid", "UnknownAction", "merge_patch"]
