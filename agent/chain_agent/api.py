"""REST API agenta – implementace api/agent-api.yaml (dokumentace i na /docs)."""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .catalog import Catalog
from .config import AgentConfig
from .manager import Busy, ChainManager, Invalid, NotFound, UnknownAction
from .models import ChainSpec, Operation, utcnow
from .operations import Operations

log = logging.getLogger(__name__)
PREFIX = "/agent/v1"


class Role(IntEnum):
    none = 0
    read = 1
    control = 2


class Problem(Exception):
    def __init__(self, status: int, title: str, detail: str | None = None, errors: list[str] | None = None):
        super().__init__(title)
        self.status, self.title, self.detail, self.errors = status, title, detail, errors


def problem_response(status: int, title: str, detail: str | None = None,
                     errors: list[str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else None


def _in(token: str, tokens: list[str]) -> bool:
    return any(secrets.compare_digest(token, t) for t in tokens)


def create_app(cfg: AgentConfig) -> FastAPI:
    catalog = Catalog(cfg.catalogDir)
    ops = Operations()
    manager = ChainManager(cfg, catalog, ops)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.startup()
        log.info("chain-agent %s: VM %s, API na %s:%s", __version__, cfg.vmName, cfg.listenAddress, cfg.listenPort)
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Chain Agent – API agenta na VM",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
    )
    app.state.manager = manager
    started_at = utcnow()

    # ------------------------------------------------------------------ chyby → problem+json
    @app.exception_handler(Problem)
    async def _problem(_: Request, e: Problem) -> JSONResponse:
        return problem_response(e.status, e.title, e.detail, e.errors)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound) -> JSONResponse:
        return problem_response(404, "Not found", str(e))

    @app.exception_handler(UnknownAction)
    async def _ua(_: Request, e: UnknownAction) -> JSONResponse:
        return problem_response(404, "Unknown action", str(e))

    @app.exception_handler(Busy)
    async def _busy(_: Request, e: Busy) -> JSONResponse:
        return problem_response(409, "Operation in progress", e.op_id)

    @app.exception_handler(Invalid)
    async def _inv(_: Request, e: Invalid) -> JSONResponse:
        return problem_response(422, "Invalid request", errors=e.errors)

    @app.exception_handler(RequestValidationError)
    async def _rve(_: Request, e: RequestValidationError) -> JSONResponse:
        errors = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        return problem_response(422, "Invalid request", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        return problem_response(e.status_code, str(e.detail))

    # ------------------------------------------------------------------ autorizace a audit
    def role_of(request: Request) -> Role:
        token = _bearer(request)
        if not token:
            return Role.none
        if _in(token, cfg.controlTokens):
            return Role.control
        if _in(token, cfg.readTokens):
            return Role.read
        return Role.none

    def require(needed: Role) -> Callable[[Request], Role]:
        def dep(request: Request) -> Role:
            r = role_of(request)
            if r < needed:
                raise Problem(401 if r == Role.none else 403, "Unauthorized" if r == Role.none else "Forbidden")
            return r
        return dep

    # Všechny handlery jsou async: běží ve smyčce událostí spolu se správcem řetězců.
    read = Depends(require(Role.read))
    control = Depends(require(Role.control))

    def audit(request: Request, what: str) -> None:
        token = _bearer(request) or ""
        who = hashlib.sha256(token.encode()).hexdigest()[:8]  # nikdy neukládat token samotný
        try:
            cfg.logDir.mkdir(parents=True, exist_ok=True)
            with open(cfg.logDir / "audit.log", "a", encoding="utf-8") as f:
                f.write(f"{utcnow().isoformat()} token:{who} {request.method} {request.url.path} {what}\n")
        except OSError as e:
            log.error("audit: %s", e)

    def accepted(op: Operation) -> JSONResponse:
        return JSONResponse(op.model_dump(mode="json"), status_code=202)

    def info() -> dict[str, Any]:
        return {
            "vmName": cfg.vmName,
            "environment": cfg.environment,
            "agentVersion": __version__,
            "startedAt": started_at.isoformat(),
            "portRange": list(cfg.portRange),
        }

    # ------------------------------------------------------------------ čtení
    @app.get(f"{PREFIX}/health", tags=["agent"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{PREFIX}/info", tags=["agent"], dependencies=[read])
    async def get_info() -> dict[str, Any]:
        return info()

    @app.get(f"{PREFIX}/overview", tags=["agent"], dependencies=[read])
    async def overview() -> dict[str, Any]:
        return {
            "vm": info(),
            "state": manager.vm_state(),
            "chains": manager.chains_json(),
            "generatedAt": utcnow().isoformat(),
        }

    @app.get(f"{PREFIX}/catalog", tags=["agent"], dependencies=[read])
    async def get_catalog() -> list[dict[str, Any]]:
        catalog.rescan()
        return catalog.to_json()

    @app.post(f"{PREFIX}/validate", tags=["chains"], dependencies=[read])
    async def validate(spec: ChainSpec) -> dict[str, Any]:
        _, result = manager.validate(spec, replacing_id=spec.id)
        return result.model_dump()

    @app.get(f"{PREFIX}/chains", tags=["chains"], dependencies=[read])
    async def list_chains() -> list[dict[str, Any]]:
        return manager.chains_json()

    @app.get(f"{PREFIX}/chains/{{chain_id}}", tags=["chains"], dependencies=[read])
    async def get_chain(chain_id: str) -> dict[str, Any]:
        return manager.chain_json(chain_id)

    @app.get(f"{PREFIX}/apps/{{instance_id}}", tags=["apps"], dependencies=[read])
    async def get_app(instance_id: str) -> dict[str, Any]:
        return manager.find_app(instance_id)[1].to_json()

    @app.get(f"{PREFIX}/apps/{{instance_id}}/logs", tags=["apps"], dependencies=[read])
    async def get_logs(instance_id: str, tail: int = Query(100, ge=1, le=5000)) -> dict[str, Any]:
        return {"lines": manager.find_app(instance_id)[1].tail_log(tail)}

    @app.get(f"{PREFIX}/operations/{{op_id}}", tags=["agent"], dependencies=[read])
    async def get_operation(op_id: str) -> dict[str, Any]:
        op = ops.get(op_id)
        if op is None:
            raise NotFound(op_id)
        return op.model_dump(mode="json")

    @app.get(
        f"{PREFIX}/apps/{{instance_id}}/config", tags=["apps"],
        responses={200: {"content": {"application/json": {}, "text/plain": {}}}, 304: {}},
    )
    async def get_config(
        instance_id: str, request: Request, format: Literal["json", "ini"] | None = None,
    ) -> Response:
        """Konfigurace instance. Token instance (APP_API_TOKEN) → s tajnými hodnotami,
        token role read/control → tajné hodnoty zamaskované. Podporuje ETag / If-None-Match."""
        _, inst = manager.find_app(instance_id)
        token = _bearer(request)
        is_instance = bool(token) and secrets.compare_digest(token or "", inst.l.token)
        if not is_instance and role_of(request) < Role.read:
            raise Problem(401, "Unauthorized")
        fmt = format or inst.l.config_format
        try:
            text, version = manager.render_config(inst, fmt, resolve_secrets=is_instance)
        except (OSError, ValueError) as e:
            raise Problem(500, "Config error", str(e)) from e
        tag = f'"{version}"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        media = "application/json" if fmt == "json" else "text/plain; charset=utf-8"
        return Response(text, media_type=media, headers={"ETag": tag, "X-Config-Version": version})

    # ------------------------------------------------------------------ změny
    @app.post(f"{PREFIX}/chains", tags=["chains"], status_code=201, dependencies=[control],
              responses={202: {"description": "Vytvořeno a spouští se (start=true)"}})
    async def create_chain(spec: ChainSpec, request: Request, start: bool = False) -> JSONResponse:
        chain_id, op = manager.create(spec, start)
        audit(request, f"create {chain_id}{' +start' if start else ''}")
        if op is not None:
            return accepted(op)
        return JSONResponse(manager.chain_json(chain_id), status_code=201)

    @app.put(f"{PREFIX}/chains/{{chain_id}}", tags=["chains"], status_code=202, dependencies=[control])
    async def update_chain(chain_id: str, spec: ChainSpec, request: Request) -> JSONResponse:
        op = manager.update(chain_id, spec)
        audit(request, f"update {chain_id}")
        return accepted(op)

    @app.delete(f"{PREFIX}/chains/{{chain_id}}", tags=["chains"], status_code=202, dependencies=[control])
    async def delete_chain(chain_id: str, request: Request) -> JSONResponse:
        op = manager.remove(chain_id)
        audit(request, f"remove {chain_id}")
        return accepted(op)

    @app.post(f"{PREFIX}/chains/{{chain_id}}/{{action}}", tags=["chains"], status_code=202, dependencies=[control])
    async def chain_action(chain_id: str, action: str, request: Request) -> JSONResponse:
        op = manager.chain_action(chain_id, action)
        audit(request, f"{action} chain {chain_id}")
        return accepted(op)

    @app.post(f"{PREFIX}/apps/{{instance_id}}/{{action}}", tags=["apps"], status_code=202, dependencies=[control])
    async def app_action(instance_id: str, action: str, request: Request) -> JSONResponse:
        op = manager.app_action(instance_id, action)
        audit(request, f"{action} app {instance_id}")
        return accepted(op)

    @app.patch(f"{PREFIX}/apps/{{instance_id}}/config", tags=["apps"], dependencies=[control])
    async def patch_config(
        instance_id: str, request: Request, patch: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """JSON Merge Patch vrstvy řetězce (null maže klíč). Běžící aplikaci se změna pošle
        přes PATCH /api/v1/config; když ji za běhu nepřijme, je potřeba restart."""
        result = await manager.app_config(instance_id, patch)
        audit(request, f"config {instance_id} keys={sorted(patch)}")  # hodnoty mohou být citlivé
        return result

    return app
