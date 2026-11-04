"""REST API backendu (/api/v1). Dokumentace běžícího backendu: /docs, schéma: /openapi.json.

Role: viewer (čtení) a operator (akce, vytváření). Bez nastavených tokenů je API otevřené
(jen pro vývoj). Prometheus service discovery používá stejné tokeny (role viewer).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .core import Core
from .integrations import PrometheusIntegration, sd_targets
from .model import (
    CatalogEntry,
    Chain,
    ChainCreate,
    ChainKind,
    ChainValidation,
    ComponentStatus,
    Location,
    Operation,
    Summary,
    Target,
    utcnow,
)
from .operations import LocalOperations
from .settings import Settings
from .sources import AgentSource, SourceError

log = logging.getLogger(__name__)
P = "/api/v1"


class Role(IntEnum):
    none = 0
    viewer = 1
    operator = 2


class ApiError(Exception):
    def __init__(self, status: int, title: str, detail: str | None = None, errors: list[str] | None = None):
        super().__init__(title)
        self.status, self.title, self.detail, self.errors = status, title, detail, errors


def problem(status: int, title: str, detail: str | None = None, errors: list[str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def create_app(settings: Settings, transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> FastAPI:
    core = Core(settings, transports)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await core.start()
        log.info("CMU backend %s: %d zdrojů, %d integrací", __version__, len(core.sources), len(core.integrations))
        yield
        await core.stop()

    app = FastAPI(title="CMU – centrální backend", version=__version__, lifespan=lifespan,
                  description="Jednotný pohled na řetězce aplikací (agenti na VM, Komodo) "
                              "a integrace (Zabbix, Prometheus).")
    app.state.core = core
    if settings.corsOrigins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.corsOrigins, allow_methods=["*"],
                           allow_headers=["*"])

    # ------------------------------------------------------------------ chyby
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        return problem(e.status, e.title, e.detail, e.errors)

    @app.exception_handler(SourceError)
    async def _source_error(_: Request, e: SourceError) -> JSONResponse:
        return problem(e.status, "Chyba zdroje", str(e), e.errors)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError) -> JSONResponse:
        return problem(422, "Neplatný požadavek",
                       errors=[f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()])

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        return problem(e.status_code, str(e.detail))

    # ------------------------------------------------------------------ autorizace
    auth = settings.auth

    def role_of(request: Request) -> Role:
        if not auth.enabled:
            return Role.operator
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if token and any(secrets.compare_digest(token, t) for t in auth.operatorTokens):
            return Role.operator
        if token and any(secrets.compare_digest(token, t) for t in auth.viewerTokens):
            return Role.viewer
        return Role.none

    def require(needed: Role) -> Callable[[Request], Role]:
        def dep(request: Request) -> Role:
            r = role_of(request)
            if r < needed:
                raise ApiError(401 if r == Role.none else 403,
                               "Nepřihlášen" if r == Role.none else "Nedostatečné oprávnění")
            return r
        return dep

    viewer = Depends(require(Role.viewer))
    operator = Depends(require(Role.operator))

    def get_chain(chain_id: str) -> Chain:
        ch = core.chain(chain_id)
        if ch is None:
            raise ApiError(404, "Řetězec nenalezen", chain_id)
        return ch

    def source_of(chain: Chain) -> Any:
        src = core.source(chain.source.id)
        if src is None:
            raise ApiError(404, "Zdroj nenalezen", chain.source.id)
        return src

    def need(chain: Chain, capability: str) -> None:
        if capability not in chain.capabilities:
            raise ApiError(409, "Akce není pro tento řetězec dostupná", capability)
        if chain.stale:
            raise ApiError(409, "Lokalita je nedostupná", f"{chain.location.name}: data nejsou aktuální")

    # ------------------------------------------------------------------ obecné
    @app.get(f"{P}/health", tags=["system"])
    async def api_health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get(f"{P}/me", tags=["system"])
    async def me(request: Request) -> dict[str, Any]:
        r = role_of(request)
        return {"role": r.name, "authEnabled": auth.enabled}

    @app.get(f"{P}/summary", tags=["chains"], dependencies=[viewer])
    async def summary() -> Summary:
        return core.summary()

    @app.get(f"{P}/components", tags=["system"], dependencies=[viewer])
    async def components() -> list[ComponentStatus]:
        """Stav zdrojů a integrací."""
        return core.components()

    # ------------------------------------------------------------------ řetězce
    @app.get(f"{P}/chains", tags=["chains"], dependencies=[viewer])
    async def list_chains(
        health: str | None = Query(None, description="Čárkami oddělené: problem,warning,unknown,stopped,ok"),
        kind: ChainKind | None = None,
        source: str | None = None,
        location: str | None = None,
        environment: str | None = None,
        q: str | None = Query(None, description="Hledání v názvu, lokalitě, aplikacích a image"),
    ) -> list[Chain]:
        chains, _ = core.view()
        if health:
            wanted = set(health.split(","))
            chains = [c for c in chains if c.health.value in wanted]
        if kind:
            chains = [c for c in chains if c.kind == kind]
        if source:
            chains = [c for c in chains if c.source.id == source]
        if location:
            chains = [c for c in chains if c.location.name == location]
        if environment:
            chains = [c for c in chains if c.location.environment == environment]
        if q:
            needle = q.casefold()
            chains = [c for c in chains if needle in " ".join(
                [c.name, c.localId, c.location.name, c.location.address or ""]
                + [f"{a.name} {a.image or ''} {a.version or ''}" for a in c.apps]).casefold()]
        return chains

    @app.get(f"{P}/chains/{{chain_id}}", tags=["chains"], dependencies=[viewer])
    async def chain_detail(chain_id: str) -> Chain:
        return get_chain(chain_id)

    @app.post(f"{P}/chains/{{chain_id}}/actions/{{action}}", tags=["chains"], status_code=202,
              dependencies=[operator])
    async def chain_action(chain_id: str, action: str) -> Operation:
        ch = get_chain(chain_id)
        need(ch, action)
        return await source_of(ch).chain_action(ch, action)

    @app.delete(f"{P}/chains/{{chain_id}}", tags=["chains"], status_code=202, dependencies=[operator])
    async def delete_chain(chain_id: str) -> Operation:
        ch = get_chain(chain_id)
        need(ch, "delete")
        return await source_of(ch).delete_chain(ch)

    @app.post(f"{P}/chains/{{chain_id}}/apps/{{app_id}}/actions/{{action}}", tags=["chains"], status_code=202,
              dependencies=[operator])
    async def app_action(chain_id: str, app_id: str, action: str) -> Operation:
        ch = get_chain(chain_id)
        need(ch, f"app:{action}")
        if not any(a.id == app_id for a in ch.apps):
            raise ApiError(404, "Aplikace nenalezena", app_id)
        return await source_of(ch).app_action(ch, app_id, action)

    @app.get(f"{P}/chains/{{chain_id}}/apps/{{app_id}}/logs", tags=["chains"], dependencies=[viewer])
    async def app_logs(chain_id: str, app_id: str, tail: int = Query(100, ge=1, le=5000)) -> dict[str, list[str]]:
        ch = get_chain(chain_id)
        need(ch, "logs")
        return {"lines": await source_of(ch).app_logs(ch, app_id, tail)}

    @app.get(f"{P}/operations/{{op_id}}", tags=["chains"], dependencies=[viewer])
    async def operation(op_id: str) -> Operation:
        if op_id.startswith(LocalOperations.PREFIX + ":"):
            op = core.local_ops.get(op_id)
            if op is None:
                raise ApiError(404, "Operace nenalezena", op_id)
            return op
        parts = op_id.split(":", 2)
        if len(parts) != 3:
            raise ApiError(404, "Operace nenalezena", op_id)
        src = core.source(parts[0])
        if src is None:
            raise ApiError(404, "Zdroj nenalezen", parts[0])
        return await src.operation(parts[1], parts[2])

    # ------------------------------------------------------------------ lokality
    @app.get(f"{P}/locations", tags=["locations"], dependencies=[viewer])
    async def locations() -> list[Location]:
        return core.view()[1]

    # ------------------------------------------------------------------ vytváření
    @app.get(f"{P}/targets", tags=["create"], dependencies=[viewer])
    async def targets(kind: ChainKind | None = None) -> list[Target]:
        now = utcnow()
        out = [t for s in core.sources for t in s.targets(now)]
        return [t for t in out if kind is None or t.kind == kind]

    @app.get(f"{P}/targets/{{source_id}}/{{location}}/catalog", tags=["create"], dependencies=[viewer])
    async def catalog(source_id: str, location: str) -> list[CatalogEntry]:
        src = core.source(source_id)
        if src is None:
            raise ApiError(404, "Zdroj nenalezen", source_id)
        return await src.catalog(location)

    def source_for_create(req: ChainCreate) -> Any:
        src = core.source(req.sourceId)
        if src is None:
            raise ApiError(404, "Zdroj nenalezen", req.sourceId)
        return src

    @app.post(f"{P}/chains/validate", tags=["create"], dependencies=[viewer])
    async def validate_chain(req: ChainCreate) -> ChainValidation:
        """Kontrola bez provedení změn; u kontejnerů vrací náhled docker-compose."""
        return await source_for_create(req).validate_chain(req)

    @app.post(f"{P}/chains", tags=["create"], status_code=202, dependencies=[operator])
    async def create_chain(req: ChainCreate) -> Operation:
        return await source_for_create(req).create_chain(req)

    # ------------------------------------------------------------------ integrace
    @app.get(f"{P}/integrations/prometheus/targets", tags=["integrations"], dependencies=[viewer])
    async def prometheus_targets() -> list[dict[str, Any]]:
        """HTTP service discovery pro Prometheus (http_sd_configs) – všichni agenti z inventáře."""
        prom = next((i for i in core.integrations if isinstance(i, PrometheusIntegration)), None)
        return sd_targets([s for s in core.sources if isinstance(s, AgentSource)], prom.cfg if prom else None)

    # ------------------------------------------------------------------ frontend
    if settings.staticDir is not None:
        if settings.staticDir.is_dir():
            app.mount("/", StaticFiles(directory=settings.staticDir, html=True), name="frontend")
        else:
            log.warning("staticDir %s neexistuje – frontend se nebude servírovat", settings.staticDir)

    return app
