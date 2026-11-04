"""Zdroj: chain-agenti na VM (řetězce procesů). Viz api/agent-api.yaml."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..model import (
    CatalogEntry,
    Chain,
    ChainApp,
    ChainCreate,
    ChainKind,
    ChainState,
    ChainValidation,
    ComponentStatus,
    Location,
    LocationRef,
    Operation,
    Target,
    utcnow,
)
from ..operations import LocalOperations
from ..settings import AgentEntry, AgentSourceSettings
from .base import Source, SourceError

log = logging.getLogger(__name__)

CHAIN_CAPS = ["start", "stop", "restart", "delete", "app:start", "app:stop", "app:restart", "app:reload", "logs"]
_STATES = {s.value for s in ChainState}


@dataclass
class _Agent:
    entry: AgentEntry
    overview: dict[str, Any] | None = None
    last_ok: datetime | None = None
    last_attempt: datetime | None = None
    error: str = ""


class AgentSource(Source):
    type = "agent"

    def __init__(self, cfg: AgentSourceSettings, stale_after_s: float,
                 local_ops: LocalOperations | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(cfg.id, stale_after_s, local_ops)
        self._tasks: set[asyncio.Task[None]] = set()
        self.cfg = cfg
        self._agents: dict[str, _Agent] = {}
        self._inventory_mtime: float | None = None
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._client = httpx.AsyncClient(timeout=cfg.timeoutS, verify=cfg.verifyTls, transport=transport)
        self._load_inventory()

    # ------------------------------------------------------------------ inventář
    def _load_inventory(self) -> None:
        entries = list(self.cfg.agents)
        f = self.cfg.inventoryFile
        if f is not None:
            try:
                self._inventory_mtime = f.stat().st_mtime
                raw = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or []
                entries += [AgentEntry.model_validate(x) for x in raw]
            except (OSError, ValueError, yaml.YAMLError) as e:
                log.error("inventář agentů %s: %s", f, e)
                self.last_error = f"inventář: {e}"
        seen: dict[str, _Agent] = {}
        for entry in entries:
            if entry.name in seen:
                log.warning("agent %s je v inventáři vícekrát, použit první", entry.name)
                continue
            old = self._agents.get(entry.name)
            seen[entry.name] = (_Agent(entry=entry) if old is None
                                else _Agent(entry, old.overview, old.last_ok, old.last_attempt, old.error))
        self._agents = seen

    def _inventory_changed(self) -> bool:
        f = self.cfg.inventoryFile
        if f is None:
            return False
        try:
            return f.stat().st_mtime != self._inventory_mtime
        except OSError:
            return False

    # ------------------------------------------------------------------ HTTP
    def _base(self, a: AgentEntry) -> str:
        return f"{self.cfg.scheme}://{a.address}:{a.port or self.cfg.port}/agent/v1"

    def _agent(self, name: str) -> _Agent:
        a = self._agents.get(name)
        if a is None:
            raise SourceError(f"lokalita {name} není v inventáři", status=404)
        return a

    async def _call(self, name: str, method: str, path: str, *, control: bool = False,
                    json: Any = None, params: dict[str, Any] | None = None) -> httpx.Response:
        a = self._agent(name).entry
        token = (a.controlToken or self.cfg.controlToken) if control else (a.token or self.cfg.token)
        try:
            async with self._sem:
                r = await self._client.request(method, self._base(a) + path, json=json, params=params,
                                               headers={"Authorization": f"Bearer {token}"})
        except httpx.TimeoutException as e:
            raise SourceError(f"agent {name} neodpověděl včas", status=504) from e
        except httpx.HTTPError as e:
            raise SourceError(f"agent {name} je nedostupný: {e}", status=502) from e
        if r.status_code in (401, 403):
            raise SourceError(f"agent {name} odmítl token backendu (HTTP {r.status_code})", status=502)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            msg = body.get("detail") or body.get("title") or f"HTTP {r.status_code}"
            raise SourceError(f"agent {name}: {msg}", status=r.status_code if r.status_code < 500 else 502,
                              errors=body.get("errors") or [])
        return r

    # ------------------------------------------------------------------ refresh
    async def refresh(self) -> None:
        if self._inventory_changed():
            self._load_inventory()
        self.last_attempt = utcnow()
        await asyncio.gather(*(self._refresh_one(n) for n in list(self._agents)))
        failed = [n for n, a in self._agents.items() if a.error]
        self.last_error = f"{len(failed)} z {len(self._agents)} agentů neodpovídá" if failed else ""
        if len(failed) < len(self._agents) or not self._agents:
            self.last_success = utcnow()

    async def _refresh_one(self, name: str) -> None:
        a = self._agents.get(name)
        if a is None:
            return
        a.last_attempt = utcnow()
        try:
            r = await self._call(name, "GET", "/overview")
            data = r.json()
        except (SourceError, ValueError) as e:
            a.error = str(e)
            return
        vm = (data.get("vm") or {}).get("vmName")
        if vm and vm != name:
            log.warning("agent %s se hlásí jako %s – ID instancí nebudou sedět s inventářem", name, vm)
        a.overview, a.last_ok, a.error = data, utcnow(), ""

    def _refresh_soon(self, name: str) -> None:
        async def later() -> None:
            await asyncio.sleep(0.3)
            await self._refresh_one(name)
        self._tasks.add(task := asyncio.get_running_loop().create_task(later()))
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ pohled
    def _fresh(self, a: _Agent, now: datetime) -> bool:
        return a.last_ok is not None and now - a.last_ok <= timedelta(seconds=self.stale_after_s)

    def _loc_ref(self, a: _Agent) -> LocationRef:
        env = a.entry.environment or ((a.overview or {}).get("vm") or {}).get("environment")
        return LocationRef(id=f"{self.id}:{a.entry.name}", name=a.entry.name, address=a.entry.address,
                           environment=env, kind="vm")

    def chains(self, now: datetime) -> list[Chain]:
        out: list[Chain] = []
        for a in self._agents.values():
            if a.overview is None:
                continue
            stale = not self._fresh(a, now)
            loc = self._loc_ref(a)
            for c in a.overview.get("chains", []):
                spec = c.get("spec", {})
                local = spec.get("id") or ""
                state = c.get("state", "unknown")
                apps = [
                    ChainApp(
                        id=x.get("instanceId", ""),
                        name=x.get("type", ""),
                        position=x.get("position", i),
                        state="unknown" if stale else x.get("state", "unknown"),
                        ready=None if stale else x.get("ready"),
                        version=x.get("version"),
                        port=x.get("port"),
                        restarts=x.get("restarts"),
                        configPending=None if stale else x.get("configPending"),
                        detail={"pid": x.get("pid"), "lastExitCode": x.get("lastExitCode"),
                                "lastPolledAt": x.get("lastPolledAt")},
                    )
                    for i, x in enumerate(c.get("apps", []))
                ]
                out.append(Chain(
                    id=f"{self.id}:{a.entry.name}:{local}", localId=local,
                    name=spec.get("name") or local, kind=ChainKind.process,
                    source=self.ref, location=loc,
                    state=ChainState.unknown if stale or state not in _STATES else ChainState(state),
                    desiredRunning=c.get("desiredRunning"), stale=stale, lastSeen=a.last_ok,
                    apps=sorted(apps, key=lambda x: x.position), capabilities=list(CHAIN_CAPS),
                    busyOperation=None if stale else c.get("busyOperation"),
                ))
        return out

    def locations(self, now: datetime) -> list[Location]:
        out = []
        for a in self._agents.values():
            ref = self._loc_ref(a)
            out.append(Location(
                id=ref.id, name=ref.name, kind="vm", source=self.ref, address=ref.address,
                environment=ref.environment, reachable=self._fresh(a, now) and not a.error,
                lastSeen=a.last_ok, error=a.error or None,
                chainCount=len((a.overview or {}).get("chains", [])),
            ))
        return out

    def status(self) -> ComponentStatus:
        s = super().status()
        s.details = {"agents": len(self._agents), "unreachable": sum(1 for a in self._agents.values() if a.error)}
        return s

    def agent_targets(self) -> list[tuple[AgentEntry, str | None]]:
        """Pro Prometheus service discovery: (agent, prostředí)."""
        return [(a.entry, self._loc_ref(a).environment) for a in self._agents.values()]

    @property
    def default_port(self) -> int:
        return self.cfg.port

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ akce
    def _op(self, name: str, data: dict[str, Any]) -> Operation:
        op = Operation.model_validate(data)
        op.id = f"{self.id}:{name}:{op.id}"
        return op

    async def chain_action(self, chain: Chain, action: str) -> Operation:
        name = chain.location.name
        r = await self._call(name, "POST", f"/chains/{chain.localId}/{action}", control=True)
        self._refresh_soon(name)
        return self._op(name, r.json())

    async def delete_chain(self, chain: Chain) -> Operation:
        name = chain.location.name
        r = await self._call(name, "DELETE", f"/chains/{chain.localId}", control=True)
        self._refresh_soon(name)
        return self._op(name, r.json())

    async def app_action(self, chain: Chain, app_id: str, action: str) -> Operation:
        name = chain.location.name
        r = await self._call(name, "POST", f"/apps/{app_id}/{action}", control=True)
        self._refresh_soon(name)
        return self._op(name, r.json())

    async def app_logs(self, chain: Chain, app_id: str, tail: int) -> list[str]:
        r = await self._call(chain.location.name, "GET", f"/apps/{app_id}/logs", params={"tail": tail})
        return list(r.json().get("lines", []))

    async def operation(self, location: str, op_id: str) -> Operation:
        r = await self._call(location, "GET", f"/operations/{op_id}")
        op = self._op(location, r.json())
        if op.status != "running":
            self._refresh_soon(location)
        return op

    # ------------------------------------------------------------------ vytváření
    def targets(self, now: datetime) -> list[Target]:
        return [
            Target(sourceId=self.id, sourceType=self.type, kind=ChainKind.process, location=a.entry.name,
                   locationId=f"{self.id}:{a.entry.name}", environment=self._loc_ref(a).environment,
                   reachable=self._fresh(a, now) and not a.error)
            for a in self._agents.values()
        ]

    async def catalog(self, location: str) -> list[CatalogEntry]:
        r = await self._call(location, "GET", "/catalog")
        return [CatalogEntry.model_validate(x) for x in r.json()]

    @staticmethod
    def _spec(req: ChainCreate) -> dict[str, Any]:
        return {"name": req.name, "apps": [a.model_dump() for a in req.processApps]}

    async def validate_chain(self, req: ChainCreate) -> ChainValidation:
        r = await self._call(req.location, "POST", "/validate", json=self._spec(req))
        v = r.json()
        return ChainValidation(valid=v.get("valid", False), errors=v.get("errors", []),
                               warnings=v.get("warnings", []), assignedPorts=v.get("assignedPorts", []))

    async def create_chain(self, req: ChainCreate) -> Operation:
        r = await self._call(req.location, "POST", "/chains", control=True, json=self._spec(req),
                             params={"start": "true" if req.start else "false"})
        self._refresh_soon(req.location)
        if r.status_code == 202:
            return self._op(req.location, r.json())
        chain_id = r.json().get("spec", {}).get("id", "")
        now = utcnow()
        return Operation(id=f"{self.id}:{req.location}:created-{chain_id}", kind="chain.create",
                         target=chain_id, status="succeeded", message="vytvořeno bez spuštění",
                         startedAt=now, finishedAt=now)
