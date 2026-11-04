"""Zdroj: simulovaná data pro vývoj frontendu a ukázky (bez agentů a Komoda).

Obsahuje řetězce procesů i kontejnerů na několika lokalitách, nedostupnou VM,
výstrahy a metriky. Akce (start, stop, restart, smazání, vytvoření) mění stav postupně
po krocích `stepS` stejně jako skutečné zdroje: start odzadu, stop odpředu.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..model import (
    Alert,
    CatalogEntry,
    Chain,
    ChainApp,
    ChainCreate,
    ChainKind,
    ChainState,
    ChainValidation,
    Location,
    LocationRef,
    Metric,
    Operation,
    Severity,
    Target,
    utcnow,
)
from ..operations import LocalOperations
from ..settings import DemoSourceSettings
from . import compose
from .base import Source, SourceError

_CATALOG = {
    "App1": ["2.5.0-rc1", "2.4.1"], "App2": ["2.5.0-rc1", "2.4.1", "2.4.0"], "App3": ["2.0.0-rc1", "1.9.0"],
    "App4": ["0.8.3"], "App5": ["3.0.0"], "App6": ["1.1.2"], "App7": ["1.0.0"], "App8": ["1.0.0"],
}
_PROCESS_CAPS = ["start", "stop", "restart", "delete", "app:start", "app:stop", "app:restart", "app:reload", "logs"]
_CONTAINER_CAPS = ["start", "stop", "restart", "delete", "app:start", "app:stop", "app:restart", "logs"]


@dataclass
class _Loc:
    name: str
    address: str
    environment: str
    kind: str  # vm | docker-host
    reachable: bool = True


@dataclass
class _App:
    name: str
    state: str = "running"
    version: str | None = None
    image: str | None = None
    port: int | None = None
    restarts: int = 0


@dataclass
class _Chain:
    local_id: str
    name: str
    kind: ChainKind
    location: str
    apps: list[_App]
    desired: bool = True
    alerts: list[Alert] = field(default_factory=list)
    busy: str | None = None


def _p(name: str, version: str, port: int, state: str = "running") -> _App:
    return _App(name=name, version=version, port=port, state=state)


def _c(name: str, image: str, state: str = "running") -> _App:
    return _App(name=name.lower(), image=image, state=state)


class DemoSource(Source):
    type = "demo"

    def __init__(self, cfg: DemoSourceSettings, stale_after_s: float, local_ops: LocalOperations | None = None):
        super().__init__(cfg.id, stale_after_s, local_ops)
        self.cfg = cfg
        self._rng = random.Random(cfg.seed)
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = utcnow()
        self._locs = {x.name: x for x in [
            _Loc("vm-prod-01", "10.0.1.11", "production", "vm"),
            _Loc("vm-prod-02", "10.0.1.12", "production", "vm"),
            _Loc("vm-prod-03", "10.0.1.13", "production", "vm"),
            _Loc("vm-test-01", "10.0.2.21", "test", "vm"),
            _Loc("vm-test-02", "10.0.2.22", "test", "vm", reachable=False),
            _Loc("docker-01", "10.0.3.31", "production", "docker-host"),
            _Loc("docker-02", "10.0.3.32", "test", "docker-host"),
        ]}
        zbx_since = utcnow() - timedelta(minutes=42)
        self._chains: dict[str, _Chain] = {}
        for ch in [
            _Chain("chain-a", "Příjem objednávek", ChainKind.process, "vm-prod-01",
                   [_p("App1", "2.4.1", 8500), _p("App2", "2.4.1", 8501), _p("App3", "1.9.0", 8502)]),
            _Chain("chain-b", "Fakturace", ChainKind.process, "vm-prod-01",
                   [_p("App1", "2.4.1", 8503), _p("App4", "0.8.3", 8504)]),
            _Chain("chain-a", "Příjem objednávek", ChainKind.process, "vm-prod-02",
                   [_p("App1", "2.4.1", 8500), _p("App2", "2.4.1", 8501, "degraded"), _p("App3", "1.9.0", 8502)]),
            _Chain("chain-c", "Reporting", ChainKind.process, "vm-prod-02",
                   [_p("App5", "3.0.0", 8503), _p("App2", "2.4.1", 8504), _p("App6", "1.1.2", 8505)],
                   alerts=[Alert(source="zabbix", severity=Severity.average, host="vm-prod-02",
                                 name="Disk /var zaplněn na 91 %", since=zbx_since)]),
            _Chain("chain-a", "Příjem objednávek", ChainKind.process, "vm-prod-03",
                   [_p("App1", "2.4.1", 8500), _p("App2", "2.4.0", 8501, "error"), _p("App3", "1.9.0", 8502)]),
            _Chain("chain-d", "Archivace", ChainKind.process, "vm-prod-03",
                   [_p("App7", "1.0.0", 8503, "stopped"), _p("App8", "1.0.0", 8504, "stopped")], desired=False),
            _Chain("chain-a", "Příjem objednávek", ChainKind.process, "vm-test-01",
                   [_p("App1", "2.5.0-rc1", 8500), _p("App2", "2.5.0-rc1", 8501), _p("App3", "2.0.0-rc1", 8502)]),
            _Chain("chain-e", "Import dat", ChainKind.process, "vm-test-02",
                   [_p("App5", "3.0.0", 8500), _p("App6", "1.1.2", 8501)]),
            _Chain("cmu-notifikace", "Notifikace", ChainKind.container, "docker-01",
                   [_c("gateway", "registry.local/gateway:1.4.0"), _c("router", "registry.local/router:2.1.0"),
                    _c("sender", "registry.local/sender:2.1.0")]),
            _Chain("cmu-export", "Export pro partnery", ChainKind.container, "docker-01",
                   [_c("collector", "registry.local/collector:0.9.2"), _c("packer", "registry.local/packer:0.9.2")],
                   alerts=[Alert(source="zabbix", severity=Severity.high, host="docker-01",
                                 name="Kontejner packer: vysoké využití paměti (96 %)", since=zbx_since)]),
            _Chain("cmu-sandbox", "Sandbox", ChainKind.container, "docker-02",
                   [_c("echo-in", "registry.local/echo:1.0.0"), _c("echo-out", "registry.local/echo:1.0.0")]),
        ]:
            self._chains[f"{ch.location}:{ch.local_id}"] = ch

    # ------------------------------------------------------------------ pohled
    async def refresh(self) -> None:
        self.last_attempt = self.last_success = utcnow()

    def _loc_ref(self, name: str) -> LocationRef:
        loc = self._locs[name]
        return LocationRef(id=f"{self.id}:{name}", name=name, address=loc.address, environment=loc.environment,
                           kind="vm" if loc.kind == "vm" else "docker-host")

    @staticmethod
    def _state(ch: _Chain) -> ChainState:
        states = [a.state for a in ch.apps]
        if all(s == "stopped" for s in states):
            return ChainState.stopped
        if "starting" in states:
            return ChainState.starting
        if "stopping" in states:
            return ChainState.stopping
        if any(s in ("error", "stopped") for s in states):
            return ChainState.broken
        if "degraded" in states:
            return ChainState.degraded
        return ChainState.running

    def chains(self, now: datetime) -> list[Chain]:
        out = []
        for ch in self._chains.values():
            loc = self._locs[ch.location]
            stale = not loc.reachable
            seen = (self._started - timedelta(minutes=33)) if stale else self.last_success
            apps = [
                ChainApp(
                    id=f"{ch.local_id}.{a.name.lower()}" if ch.kind == ChainKind.process else a.name,
                    name=a.name, position=i, state="unknown" if stale else a.state,
                    ready=None if stale else a.state in ("running", "degraded"),
                    version=a.version, image=a.image, port=a.port, restarts=a.restarts,
                    configPending=False if ch.kind == ChainKind.process and not stale else None,
                )
                for i, a in enumerate(ch.apps)
            ]
            metrics = []
            if not stale:
                running = self._state(ch) in (ChainState.running, ChainState.degraded)
                base = 120 + (sum(map(ord, ch.local_id + ch.location)) % 900)
                metrics = [
                    Metric(id="throughput", label="Zpráv/min", unit="/min",
                           value=float(base + self._rng.randint(-15, 15)) if running else 0.0),
                    Metric(id="restarts_1h", label="Restarty za hodinu", value=float(sum(a.restarts for a in ch.apps))),
                ]
            out.append(Chain(
                id=f"{self.id}:{ch.location}:{ch.local_id}", localId=ch.local_id, name=ch.name, kind=ch.kind,
                source=self.ref, location=self._loc_ref(ch.location),
                state=ChainState.unknown if stale else self._state(ch),
                desiredRunning=ch.desired if ch.kind == ChainKind.process else None,
                stale=stale, lastSeen=seen, apps=apps, alerts=list(ch.alerts), metrics=metrics,
                capabilities=list(_PROCESS_CAPS if ch.kind == ChainKind.process else _CONTAINER_CAPS),
                busyOperation=ch.busy,
            ))
        return out

    def locations(self, now: datetime) -> list[Location]:
        return [
            Location(
                id=f"{self.id}:{n}", name=n, kind="vm" if loc.kind == "vm" else "docker-host", source=self.ref,
                address=loc.address, environment=loc.environment, reachable=loc.reachable,
                lastSeen=self.last_success if loc.reachable else self._started - timedelta(minutes=33),
                error=None if loc.reachable else "agent neodpovídá (connection refused)",
                chainCount=sum(1 for c in self._chains.values() if c.location == n),
            )
            for n, loc in self._locs.items()
        ]

    # ------------------------------------------------------------------ akce
    def _find(self, chain: Chain) -> _Chain:
        ch = self._chains.get(f"{chain.location.name}:{chain.localId}")
        if ch is None:
            raise SourceError("řetězec nenalezen", status=404)
        if not self._locs[ch.location].reachable:
            raise SourceError(f"lokalita {ch.location} je nedostupná", status=502)
        if ch.busy:
            raise SourceError(f"nad řetězcem už běží operace {ch.busy}", status=409)
        return ch

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _seq(self, op: Operation, ch: _Chain, apps: list[_App], via: str, final: str) -> None:
        for a in apps:
            self.local_ops.step(op, a.name, "running")
            a.state = via
            await asyncio.sleep(self.cfg.stepS)
            a.state = final
            self.local_ops.step(op, a.name, "succeeded", "ready" if final == "running" else "stopped")

    async def chain_action(self, chain: Chain, action: str) -> Operation:
        if action not in ("start", "stop", "restart"):
            raise SourceError(f"neznámá akce {action}", status=404)
        ch = self._find(chain)
        op = self.local_ops.begin(f"chain.{action}", chain.id)
        ch.busy, ch.desired = op.id, action != "stop"

        async def run() -> None:
            if action in ("stop", "restart"):
                await self._seq(op, ch, ch.apps, "stopping", "stopped")
            if action in ("start", "restart"):
                await self._seq(op, ch, list(reversed(ch.apps)), "starting", "running")
            ch.busy = None
            self.local_ops.finish(op, True, "chain stopped" if action == "stop" else "chain running")

        self._spawn(run())
        return op

    async def app_action(self, chain: Chain, app_id: str, action: str) -> Operation:
        ch = self._find(chain)
        app = next((a for a in ch.apps if a.name == app_id or f"{ch.local_id}.{a.name.lower()}" == app_id), None)
        if app is None:
            raise SourceError(f"aplikace {app_id} nenalezena", status=404)
        if action not in ("start", "stop", "restart", "reload"):
            raise SourceError(f"neznámá akce {action}", status=404)
        op = self.local_ops.begin(f"app.{action}", app_id)

        async def run() -> None:
            if action in ("stop", "restart"):
                await self._seq(op, ch, [app], "stopping", "stopped")
            if action in ("start", "restart"):
                await self._seq(op, ch, [app], "starting", "running")
                app.restarts += action == "restart"
            self.local_ops.finish(op, True, "hotovo")

        self._spawn(run())
        return op

    async def delete_chain(self, chain: Chain) -> Operation:
        ch = self._find(chain)
        op = self.local_ops.begin("chain.delete", chain.id)
        ch.busy = op.id

        async def run() -> None:
            await self._seq(op, ch, ch.apps, "stopping", "stopped")
            self._chains.pop(f"{ch.location}:{ch.local_id}", None)
            self.local_ops.finish(op, True, "removed")

        self._spawn(run())
        return op

    async def app_logs(self, chain: Chain, app_id: str, tail: int) -> list[str]:
        ch = self._find(chain)
        t0 = utcnow() - timedelta(seconds=40)
        lines = []
        for i in range(min(tail, 12)):
            ts = (t0 + timedelta(seconds=i * 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(f"{ts} INFO  {app_id}: zpracována dávka #{48200 + i} ({self._rng.randint(380, 520)} zpráv)")
        if any(a.state == "degraded" for a in ch.apps):
            lines.append(f"{utcnow():%Y-%m-%dT%H:%M:%SZ} WARN  fronta 1 840 > limit 1 000")
        return lines

    async def operation(self, location: str, op_id: str) -> Operation:
        op = self.local_ops.get(op_id)
        if op is None:
            raise SourceError("operace nenalezena", status=404)
        return op

    # ------------------------------------------------------------------ vytváření
    def targets(self, now: datetime) -> list[Target]:
        return [
            Target(sourceId=self.id, sourceType=self.type,
                   kind=ChainKind.process if loc.kind == "vm" else ChainKind.container,
                   location=n, locationId=f"{self.id}:{n}", environment=loc.environment, reachable=loc.reachable)
            for n, loc in self._locs.items()
        ]

    async def catalog(self, location: str) -> list[CatalogEntry]:
        return [CatalogEntry(type=t, versions=v) for t, v in _CATALOG.items()]

    async def validate_chain(self, req: ChainCreate) -> ChainValidation:
        loc = self._locs.get(req.location)
        errors: list[str] = []
        warnings: list[str] = []
        preview = None
        ports: list[int] = []
        if loc is None:
            return ChainValidation(valid=False, errors=[f"lokalita {req.location} neexistuje"])
        if not loc.reachable:
            errors.append(f"lokalita {req.location} je nedostupná")
        if any(c.location == req.location and c.name.casefold() == req.name.strip().casefold()
               for c in self._chains.values()):
            errors.append(f"název '{req.name}' už na lokalitě existuje")
        if req.kind == ChainKind.process:
            if loc.kind != "vm":
                errors.append("řetězec procesů lze umístit jen na VM s agentem")
            apps = req.processApps
            if len(apps) < 2:
                errors.append("řetězec potřebuje alespoň 2 aplikace")
            types = [a.type for a in apps]
            errors += [f"{t} je v řetězci dvakrát" for t in sorted({t for t in types if types.count(t) > 1})]
            for a in apps:
                if a.version not in _CATALOG.get(a.type, []):
                    errors.append(f"{a.type} {a.version} není v katalogu VM")
                if "-" in a.version and loc.environment == "production":
                    warnings.append(f"{a.type} {a.version} je předběžná verze na produkční VM")
            used = {a.port for c in self._chains.values() if c.location == req.location for a in c.apps}
            p = 8500
            for _ in apps:
                while p in used:
                    p += 1
                ports.append(p)
                used.add(p)
        else:
            if loc.kind != "docker-host":
                errors.append("řetězec kontejnerů lze umístit jen na server v Komodu")
            e, w = compose.validate(req.containerApps)
            errors += e
            warnings += w
            stack = compose.stack_name("cmu-", req.name)
            if req.containerApps:
                preview = compose.render(compose.build(req.name, stack, req.location, req.containerApps))
            ports = [a.publish for a in req.containerApps if a.publish]
        return ChainValidation(valid=not errors, errors=errors, warnings=warnings, preview=preview,
                               assignedPorts=ports)

    async def create_chain(self, req: ChainCreate) -> Operation:
        v = await self.validate_chain(req)
        if not v.valid:
            raise SourceError("neplatný řetězec", status=422, errors=v.errors)
        if req.kind == ChainKind.process:
            local = f"chain-{compose.slugify(req.name)[:20] or 'new'}"
            apps = [_p(a.type, a.version, port, "stopped") for a, port in zip(req.processApps, v.assignedPorts)]
        else:
            local = compose.stack_name("cmu-", req.name)
            apps = [_c(a.name, a.image, "stopped") for a in req.containerApps]
        ch = _Chain(local, req.name.strip(), req.kind, req.location, apps, desired=req.start)
        self._chains[f"{req.location}:{local}"] = ch
        if not req.start:
            op = self.local_ops.begin("chain.create", local)
            self.local_ops.finish(op, True, "vytvořeno bez spuštění")
            return op
        chain = next(c for c in self.chains(utcnow()) if c.localId == local and c.location.name == req.location)
        return await self.chain_action(chain, "start")
