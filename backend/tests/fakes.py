"""Simulovaná API externích systémů pro testy (httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx

NOW_MS = 1_762_000_000_000


def _json(status: int, body: Any, ctype: str = "application/json") -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": ctype})


class FakeAgents:
    """Několik chain-agentů za jedním transportem; rozlišení podle hostitele v URL."""

    READ, CONTROL = "agent-read", "agent-control"

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.calls: list[tuple[str, str, str, str]] = []  # (host, metoda, cesta, token)
        self.chains: dict[str, list[dict[str, Any]]] = {
            "10.0.1.11": [self._chain("chain-a", "Příjem", "running", ["App1", "App2"])],
            "10.0.1.12": [self._chain("chain-b", "Fakturace", "broken", ["App1", "App4"], bad=1)],
        }

    @staticmethod
    def _chain(cid: str, name: str, state: str, apps: list[str], bad: int | None = None) -> dict[str, Any]:
        return {
            "spec": {"id": cid, "name": name, "apps": [{"type": a, "version": "1.0.0"} for a in apps]},
            "state": state, "desiredRunning": True, "busyOperation": None,
            "apps": [{"instanceId": f"{cid}.{a.lower()}", "type": a, "version": "1.0.0", "port": 8500 + i,
                      "position": i, "state": "error" if i == bad else "running", "ready": i != bad,
                      "restarts": 0, "configPending": False} for i, a in enumerate(apps)],
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        path = req.url.path.removeprefix("/agent/v1")
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        self.calls.append((host, req.method, path, token))
        if host in self.down:
            raise httpx.ConnectError("connection refused", request=req)
        if token not in (self.READ, self.CONTROL):
            return _json(401, {"title": "Unauthorized", "status": 401}, "application/problem+json")
        write = req.method != "GET" and path != "/validate"
        if write and token != self.CONTROL:
            return _json(403, {"title": "Forbidden", "status": 403}, "application/problem+json")
        vm = {"10.0.1.11": "vm-1", "10.0.1.12": "vm-2"}.get(host, host)
        if path == "/overview":
            return _json(200, {"vm": {"vmName": vm, "environment": "production"}, "state": "ok",
                               "chains": self.chains.get(host, [])})
        if path.startswith("/chains/") and req.method == "POST":
            _, _, cid, action = path.split("/")
            return _json(202, {"id": "op1", "kind": f"chain.{action}", "target": cid, "status": "running",
                               "steps": [], "startedAt": "2026-11-04T10:00:00Z"})
        if path == "/operations/op1":
            return _json(200, {"id": "op1", "kind": "chain.stop", "target": "chain-a", "status": "succeeded",
                               "message": "chain stopped", "steps": []})
        if path.endswith("/logs"):
            return _json(200, {"lines": ["line 1", "line 2"]})
        if path == "/catalog":
            return _json(200, [{"type": "App1", "versions": ["1.0.0"]}])
        if path == "/validate":
            body = json.loads(req.content)
            ok = len(body["apps"]) >= 2
            return _json(200, {"valid": ok, "errors": [] if ok else ["řetězec potřebuje alespoň 2 aplikace"],
                               "warnings": [], "assignedPorts": [8510, 8511][: len(body["apps"])]})
        if path == "/chains" and req.method == "POST":
            body = json.loads(req.content)
            if req.url.params.get("start") == "true":
                return _json(202, {"id": "op2", "kind": "chain.start", "target": "chain-new", "status": "running"})
            return _json(201, {"spec": {**body, "id": "chain-new"}, "state": "stopped", "apps": []})
        return _json(404, {"title": "Not found", "status": 404}, "application/problem+json")


class FakeKomodo:
    KEY, SECRET = "k-key", "k-secret"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.servers = [
            {"id": "s1", "name": "docker-01", "info": {"state": "Ok", "address": "10.0.3.31", "region": ""}},
            {"id": "s2", "name": "docker-02", "info": {"state": "NotOk", "address": "10.0.3.32", "region": "",
                                                       "err": {"error": "connection refused", "trace": []}}},
        ]
        self.stacks: list[dict[str, Any]] = [
            self._stack("st1", "cmu-notifikace", "s1", "docker-01", "running", ["sender", "gateway", "router"]),
            self._stack("st2", "other-stack", "s1", "docker-01", "running", ["x"]),
        ]
        # pořadí z labelů: gateway → router → sender
        self.services = {"st1": [
            self._svc("sender", 2, "Running"), self._svc("gateway", 0, "Running"), self._svc("router", 1, "Exited"),
        ]}
        self.updates: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _stack(sid: str, name: str, server: str, server_name: str, state: str, svcs: list[str]) -> dict[str, Any]:
        return {"id": sid, "type": "Stack", "name": name, "template": False, "tags": [],
                "info": {"server_id": server, "server_name": server_name, "state": state,
                         "services": [{"service": s, "image": f"reg/{s}:1", "update_available": False} for s in svcs]}}

    @staticmethod
    def _svc(name: str, pos: int, state: str) -> dict[str, Any]:
        return {"stack_id": "st1", "service": name, "image": f"reg/{name}:1", "state": state,
                "container": {"name": f"cmu-notifikace-{name}-1", "state": state.lower(),
                              "labels": {"cmu.chain.position": str(pos), "cmu.chain.name": "Notifikace",
                                         "cmu.app.name": name}}}

    def _update(self, op: str, target: str, status: str = "InProgress", success: bool = True) -> dict[str, Any]:
        uid = f"u{len(self.updates) + 1}"
        upd = {"_id": {"$oid": uid}, "operation": op, "start_ts": NOW_MS, "success": success, "operator": "cmu",
               "target": {"type": "Stack", "id": target}, "logs": [], "status": status}
        self.updates[uid] = upd
        return upd

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("x-api-key") != self.KEY or req.headers.get("x-api-secret") != self.SECRET:
            return _json(401, {"error": "invalid api key", "trace": []})
        _, kind, typ = req.url.path.split("/")
        params = json.loads(req.content or b"{}")
        self.calls.append((kind, typ, params))
        if typ == "ListServers":
            return _json(200, self.servers)
        if typ == "ListStacks":
            terms = (params.get("query") or {}).get("terms") or []
            return _json(200, [s for s in self.stacks if all(t in s["name"] for t in terms)])
        if typ == "ListStackServices":
            return _json(200, self.services.get(params["stack"], []))
        if typ in ("StartStack", "StopStack", "RestartStack", "DeployStack", "DestroyStack"):
            return _json(200, self._update(typ, params["stack"]))
        if typ == "GetUpdate":
            upd = self.updates[params["id"]]
            upd.update(status="Complete", end_ts=NOW_MS + 5000,
                       logs=[{"stage": "Compose", "command": "docker compose", "stdout": "ok", "stderr": "",
                              "success": True, "start_ts": NOW_MS, "end_ts": NOW_MS + 5000}])
            return _json(200, upd)
        if typ == "CreateStack":
            sid = f"st{len(self.stacks) + 1}"
            cfg = params["config"]
            self.stacks.append(self._stack(sid, params["name"], cfg["server_id"], "docker-01", "down", []))
            return _json(200, {"_id": {"$oid": sid}, "name": params["name"], "config": cfg})
        if typ == "DeleteStack":
            self.stacks = [s for s in self.stacks if s["name"] != params["id"] and s["id"] != params["id"]]
            return _json(200, {"name": params["id"]})
        if typ == "GetStackLog":
            return _json(200, {"stage": "logs", "command": "docker compose logs", "stdout": "a\nb\n", "stderr": "",
                               "success": True, "start_ts": NOW_MS, "end_ts": NOW_MS})
        return _json(400, {"error": f"unknown request {typ}", "trace": []})


class FakeZabbix:
    TOKEN = "zbx-token"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        rid = body["id"]
        if req.headers.get("authorization") != f"Bearer {self.TOKEN}":
            return _json(200, {"jsonrpc": "2.0", "error": {"code": -32602, "message": "Not authorized."}, "id": rid})
        if self.fail:
            return httpx.Response(500, content=b"boom")
        self.calls.append((body["method"], body["params"]))
        if body["method"] == "host.get":
            hosts = [{"hostid": "101", "host": "vm-1", "name": "VM 1"},
                     {"hostid": "102", "host": "zbx-docker-01", "name": "Docker 01"}]
            wanted = body["params"]["filter"]["host"]
            return _json(200, {"jsonrpc": "2.0", "result": [h for h in hosts if h["host"] in wanted], "id": rid})
        if body["method"] == "trigger.get":
            res = [
                {"triggerid": "9001", "description": "Disk /var zaplněn na 95 %", "priority": "4",
                 "lastchange": "1762000000", "hosts": [{"hostid": "101", "host": "vm-1", "name": "VM 1"}]},
                {"triggerid": "9002", "description": "Vysoká zátěž CPU", "priority": "2",
                 "lastchange": "1762000100",
                 "hosts": [{"hostid": "102", "host": "zbx-docker-01", "name": "Docker 01"}]},
            ]
            ids = set(body["params"]["hostids"])
            return _json(200, {"jsonrpc": "2.0", "id": rid,
                               "result": [t for t in res if t["hosts"][0]["hostid"] in ids]})
        return _json(200, {"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"}, "id": rid})


class FakePrometheus:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        q = req.url.params.get("query", "")
        self.queries.append(q)
        if "broken_query" in q:
            return _json(400, {"status": "error", "errorType": "bad_data", "error": "parse error"})
        result = [
            {"metric": {"vm": "vm-1", "chain": "chain-a"}, "value": [1762000000, "1234.5"]},
            {"metric": {"vm": "vm-2", "chain": "chain-b"}, "value": [1762000000, "NaN"]},
        ]
        return _json(200, {"status": "success", "data": {"resultType": "vector", "result": result}})
