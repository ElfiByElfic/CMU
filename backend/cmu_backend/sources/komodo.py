"""Zdroj: Komodo (řetězce kontejnerů jako docker compose stacky).

Komodo Core API: POST {url}/{read|write|execute}/{Typ} s JSON parametry,
hlavičky X-Api-Key a X-Api-Secret. Řetězec CMU = stack, jehož jméno začíná `stackPrefix`.
Pořadí a jméno řetězce se čtou z labelů kontejnerů (viz compose.py).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..model import (
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
    OperationStep,
    Target,
    utcnow,
)
from ..operations import LocalOperations
from ..settings import KomodoSourceSettings
from . import compose
from .base import Source, SourceError

log = logging.getLogger(__name__)

CHAIN_CAPS = ["start", "stop", "restart", "delete", "app:start", "app:stop", "app:restart", "logs"]

# StackState Komoda → stav řetězce
_STACK_STATE = {
    "running": ChainState.running,
    "deploying": ChainState.starting,
    "restarting": ChainState.starting,
    "paused": ChainState.degraded,
    "stopped": ChainState.stopped,
    "created": ChainState.stopped,
    "down": ChainState.stopped,
    "dead": ChainState.broken,
    "removing": ChainState.stopping,
    "unhealthy": ChainState.broken,
    "unknown": ChainState.unknown,
}
# StackServiceState / ContainerStateStatusEnum → stav instance (stejné hodnoty jako u agenta)
_APP_STATE = {
    "running": "running", "healthy": "running",
    "created": "stopped", "exited": "stopped", "down": "stopped",
    "paused": "paused", "restarting": "starting", "stopping": "stopping", "removing": "stopping",
    "dead": "error", "unhealthy": "degraded", "unknown": "unknown", "": "unknown",
}


def _oid(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("$oid", ""))
    return str(value or "")


def _ts(ms: Any) -> datetime | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=utcnow().tzinfo)


class KomodoSource(Source):
    type = "komodo"

    def __init__(self, cfg: KomodoSourceSettings, stale_after_s: float,
                 local_ops: LocalOperations | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(cfg.id, stale_after_s, local_ops)
        self._tasks: set[asyncio.Task[None]] = set()
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.url.rstrip("/"), timeout=cfg.timeoutS, verify=cfg.verifyTls, transport=transport,
            headers={"X-Api-Key": cfg.apiKey, "X-Api-Secret": cfg.apiSecret},
        )
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._servers: dict[str, dict[str, Any]] = {}       # id → ServerListItem
        self._stacks: list[dict[str, Any]] = []             # StackListItem
        self._services: dict[str, list[dict[str, Any]]] = {}  # stack id → StackService[]

    # ------------------------------------------------------------------ HTTP
    async def _rpc(self, kind: str, typ: str, params: dict[str, Any]) -> Any:
        try:
            async with self._sem:
                r = await self._client.post(f"/{kind}/{typ}", json=params)
        except httpx.TimeoutException as e:
            raise SourceError(f"Komodo {typ}: vypršel čas", status=504) from e
        except httpx.HTTPError as e:
            raise SourceError(f"Komodo je nedostupné: {e}", status=502) from e
        if r.status_code != 200:
            try:
                err = r.json().get("error") or r.text
            except ValueError:
                err = r.text
            status = r.status_code if r.status_code in (400, 404, 409, 422) else 502
            raise SourceError(f"Komodo {typ}: {err}", status=status)
        return r.json()

    # ------------------------------------------------------------------ refresh
    async def refresh(self) -> None:
        self.last_attempt = utcnow()
        try:
            servers = await self._rpc("read", "ListServers", {"limit": 0})
            stacks = await self._rpc("read", "ListStacks", {"query": {"terms": [self.cfg.stackPrefix]}, "limit": 0})
            stacks = [s for s in stacks if str(s.get("name", "")).startswith(self.cfg.stackPrefix)]
            results = await asyncio.gather(
                *(self._rpc("read", "ListStackServices", {"stack": s["id"]}) for s in stacks),
                return_exceptions=True,
            )
        except SourceError as e:
            self.last_error = str(e)
            return
        services: dict[str, list[dict[str, Any]]] = {}
        for s, res in zip(stacks, results):
            if isinstance(res, BaseException):
                log.warning("Komodo: služby stacku %s: %s", s.get("name"), res)
                services[s["id"]] = self._services.get(s["id"], [])
            else:
                services[s["id"]] = res
        self._servers = {s["id"]: s for s in servers}
        self._stacks, self._services = stacks, services
        self.last_success, self.last_error = utcnow(), ""

    # ------------------------------------------------------------------ pohled
    def _fresh(self, now: datetime) -> bool:
        return self.last_success is not None and now - self.last_success <= timedelta(seconds=self.stale_after_s)

    def _server_ref(self, server_id: str, fallback_name: str | None = None) -> LocationRef:
        srv = self._servers.get(server_id) or {}
        info = srv.get("info") or {}
        name = srv.get("name") or fallback_name or server_id or "?"
        return LocationRef(id=f"{self.id}:{name}", name=name, address=info.get("address") or None,
                           environment=self.cfg.environment or info.get("region") or None, kind="docker-host")

    def _apps(self, stack: dict[str, Any], stale: bool) -> list[ChainApp]:
        svcs = self._services.get(stack["id"]) or []
        listed = [x.get("service") for x in (stack.get("info") or {}).get("services", [])]
        images = {x.get("service"): x.get("image") for x in (stack.get("info") or {}).get("services", [])}
        apps = []
        by_name = {s.get("service"): s for s in svcs}
        names = list(dict.fromkeys([*listed, *by_name]))
        for i, name in enumerate(names):
            s = by_name.get(name) or {}
            container = s.get("container") or {}
            labels = container.get("labels") or {}
            raw = str(s.get("state") or container.get("state") or "unknown").lower()
            try:
                pos = int(labels.get(compose.LABEL_POSITION, i))
            except ValueError:
                pos = i
            apps.append(ChainApp(
                id=name, name=labels.get(compose.LABEL_APP, name), position=pos,
                state="unknown" if stale else _APP_STATE.get(raw, raw),
                ready=None if stale else raw in ("running", "healthy"),
                image=s.get("image") or images.get(name),
                detail={"container": container.get("name"), "status": container.get("status")},
            ))
        return sorted(apps, key=lambda a: (a.position, a.id))

    def _chain_name(self, stack: dict[str, Any]) -> str:
        for s in self._services.get(stack["id"]) or []:
            label = ((s.get("container") or {}).get("labels") or {}).get(compose.LABEL_CHAIN_NAME)
            if label:
                return str(label)
        return str(stack["name"])[len(self.cfg.stackPrefix):] or str(stack["name"])

    def chains(self, now: datetime) -> list[Chain]:
        stale = not self._fresh(now)
        out = []
        for st in self._stacks:
            info = st.get("info") or {}
            loc = self._server_ref(info.get("server_id", ""), info.get("server_name"))
            raw = str(info.get("state", "unknown")).lower()
            out.append(Chain(
                id=f"{self.id}:{loc.name}:{st['name']}", localId=st["name"], name=self._chain_name(st),
                kind=ChainKind.container, source=self.ref, location=loc,
                state=ChainState.unknown if stale else _STACK_STATE.get(raw, ChainState.unknown),
                desiredRunning=None, stale=stale, lastSeen=self.last_success,
                apps=self._apps(st, stale), capabilities=list(CHAIN_CAPS),
            ))
        return out

    def locations(self, now: datetime) -> list[Location]:
        fresh = self._fresh(now)
        counts: dict[str, int] = {}
        for st in self._stacks:
            sid = (st.get("info") or {}).get("server_id", "")
            counts[sid] = counts.get(sid, 0) + 1
        out = []
        for sid, srv in self._servers.items():
            ref = self._server_ref(sid)
            info = srv.get("info") or {}
            ok = fresh and info.get("state") == "Ok"
            err = (info.get("err") or {}).get("error") if isinstance(info.get("err"), dict) else None
            out.append(Location(
                id=ref.id, name=ref.name, kind="docker-host", source=self.ref, address=ref.address,
                environment=ref.environment, reachable=ok, lastSeen=self.last_success,
                error=None if ok else (err or self.last_error or f"stav serveru: {info.get('state')}"),
                chainCount=counts.get(sid, 0),
            ))
        return out

    def status(self) -> ComponentStatus:
        s = super().status()
        s.details = {"servers": len(self._servers), "stacks": len(self._stacks), "url": self.cfg.url}
        return s

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ operace
    def _update_op(self, location: str, upd: dict[str, Any]) -> Operation:
        status = upd.get("status")
        running = status in ("Queued", "InProgress")
        logs = upd.get("logs") or []
        steps = [OperationStep(target=str(lg.get("stage", "")),
                               status="succeeded" if lg.get("success") else "failed",
                               message=(lg.get("stderr") or lg.get("stdout") or "").strip()[-500:])
                 for lg in logs]
        failed = [s for s in steps if s.status == "failed"]
        return Operation(
            id=f"{self.id}:{location}:{_oid(upd.get('_id'))}",
            kind=str(upd.get("operation", "")), target=str((upd.get("target") or {}).get("id", "")),
            status="running" if running else ("succeeded" if upd.get("success") else "failed"),
            message="" if running else (failed[-1].message if failed else "dokončeno"),
            steps=steps, startedAt=_ts(upd.get("start_ts")), finishedAt=_ts(upd.get("end_ts")),
        )

    async def operation(self, location: str, op_id: str) -> Operation:
        return self._update_op(location, await self._rpc("read", "GetUpdate", {"id": op_id}))

    async def chain_action(self, chain: Chain, action: str) -> Operation:
        typ = {"start": "StartStack", "stop": "StopStack", "restart": "RestartStack"}.get(action)
        if typ is None:
            raise SourceError(f"neznámá akce {action}", status=404)
        upd = await self._rpc("execute", typ, {"stack": chain.localId})
        return self._update_op(chain.location.name, upd)

    async def app_action(self, chain: Chain, app_id: str, action: str) -> Operation:
        typ = {"start": "StartStack", "stop": "StopStack", "restart": "RestartStack"}.get(action)
        if typ is None:
            raise SourceError(f"akce {action} není pro kontejnery podporovaná", status=404)
        upd = await self._rpc("execute", typ, {"stack": chain.localId, "services": [app_id]})
        return self._update_op(chain.location.name, upd)

    async def delete_chain(self, chain: Chain) -> Operation:
        """docker compose down (DestroyStack) a pak smazání stacku z Komoda (DeleteStack).
        Běží na pozadí jako lokální operace backendu."""
        op = self.local_ops.begin("chain.delete", chain.id)

        async def run() -> None:
            try:
                self.local_ops.step(op, "DestroyStack", "running")
                upd = self._update_op(chain.location.name,
                                      await self._rpc("execute", "DestroyStack", {"stack": chain.localId}))
                while upd.status == "running":
                    await asyncio.sleep(1)
                    upd = await self.operation(chain.location.name, upd.id.rsplit(":", 1)[-1])
                if upd.status == "failed":
                    self.local_ops.step(op, "DestroyStack", "failed", upd.message)
                    return self.local_ops.finish(op, False, f"docker compose down selhal: {upd.message}")
                self.local_ops.step(op, "DestroyStack", "succeeded")
                await self._rpc("write", "DeleteStack", {"id": chain.localId})
                self.local_ops.step(op, "DeleteStack", "succeeded")
                await self.refresh()
                self.local_ops.finish(op, True, "kontejnery odstraněny, stack smazán")
            except SourceError as e:
                self.local_ops.finish(op, False, str(e))

        self._tasks.add(task := asyncio.get_running_loop().create_task(run()))
        task.add_done_callback(self._tasks.discard)
        return op

    async def app_logs(self, chain: Chain, app_id: str, tail: int) -> list[str]:
        log_ = await self._rpc("read", "GetStackLog", {"stack": chain.localId, "services": [app_id], "tail": tail})
        text = (log_.get("stdout") or "") + (log_.get("stderr") or "")
        return [ln for ln in text.splitlines() if ln][-tail:]

    # ------------------------------------------------------------------ vytváření
    def targets(self, now: datetime) -> list[Target]:
        fresh = self._fresh(now)
        return [
            Target(sourceId=self.id, sourceType=self.type, kind=ChainKind.container, location=ref.name,
                   locationId=ref.id, environment=ref.environment,
                   reachable=fresh and (srv.get("info") or {}).get("state") == "Ok")
            for sid, srv in self._servers.items()
            for ref in [self._server_ref(sid)]
        ]

    def _server_id(self, name: str) -> str | None:
        for sid, srv in self._servers.items():
            if srv.get("name") == name:
                return sid
        return None

    async def validate_chain(self, req: ChainCreate) -> ChainValidation:
        errors, warnings = compose.validate(req.containerApps)
        stack = compose.stack_name(self.cfg.stackPrefix, req.name)
        if self._server_id(req.location) is None:
            errors.append(f"server {req.location} v Komodu neexistuje")
        if any(s.get("name") == stack for s in self._stacks):
            errors.append(f"stack {stack} už v Komodu existuje")
        preview = None
        if req.containerApps:
            preview = compose.render(compose.build(req.name, stack, req.location, req.containerApps))
        return ChainValidation(valid=not errors, errors=errors, warnings=warnings, preview=preview,
                               assignedPorts=[a.publish for a in req.containerApps if a.publish])

    async def create_chain(self, req: ChainCreate) -> Operation:
        v = await self.validate_chain(req)
        if not v.valid:
            raise SourceError("neplatný řetězec", status=422, errors=v.errors)
        stack = compose.stack_name(self.cfg.stackPrefix, req.name)
        server_id = self._server_id(req.location)
        await self._rpc("write", "CreateStack", {
            "name": stack,
            "config": {"server_id": server_id, "file_contents": v.preview, "project_name": stack},
        })
        if req.start:
            upd = await self._rpc("execute", "DeployStack", {"stack": stack})
            await self.refresh()
            return self._update_op(req.location, upd)
        await self.refresh()
        now = utcnow()
        return Operation(id=f"{self.id}:{req.location}:created-{stack}", kind="CreateStack", target=stack,
                         status="succeeded", message="stack vytvořen, nenasazen", startedAt=now, finishedAt=now)
