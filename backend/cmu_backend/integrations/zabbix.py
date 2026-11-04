"""Integrace Zabbix: aktivní problémy hostitelů jako výstrahy na lokalitách a jejich řetězcích.

JSON-RPC 2.0 na {url} (…/api_jsonrpc.php), autentizace hlavičkou `Authorization: Bearer <API token>`
(Zabbix 6.4+). Jeden dotaz host.get (mapování lokalita → hostid, cache) a jeden trigger.get
s aktivními triggery (value=1) všech mapovaných hostitelů za interval.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from ..model import Alert, Chain, Location, Severity, utcnow
from ..settings import ZabbixSettings
from .base import Integration

log = logging.getLogger(__name__)

# priorita triggeru 0–5 → závažnost
_SEVERITY = {0: Severity.info, 1: Severity.info, 2: Severity.warning, 3: Severity.average,
             4: Severity.high, 5: Severity.disaster}


class ZabbixError(Exception):
    pass


class ZabbixIntegration(Integration):
    id = "zabbix"

    def __init__(self, cfg: ZabbixSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.interval_s = cfg.intervalS
        self._client = httpx.AsyncClient(timeout=cfg.timeoutS, verify=cfg.verifyTls, transport=transport)
        self._ids = itertools.count(1)
        self._hostids: dict[str, str] = {}          # host v Zabbixu → hostid
        self._alerts: dict[str, list[Alert]] = {}   # host v Zabbixu → výstrahy

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": next(self._ids)}
        r = await self._client.post(self.cfg.url, json=body, headers={
            "Authorization": f"Bearer {self.cfg.token}", "Content-Type": "application/json-rpc"})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return data.get("result")

    def host_for(self, location: str) -> str:
        return self.cfg.hostMap.get(location, location)

    async def refresh(self, chains: list[Chain], locations: list[Location]) -> None:
        self.last_attempt = utcnow()
        wanted = sorted({self.host_for(loc.name) for loc in locations})
        try:
            missing = [h for h in wanted if h not in self._hostids]
            if missing:
                hosts = await self._call("host.get", {
                    "output": ["hostid", "host", "name"], "filter": {self.cfg.hostField: missing}})
                for h in hosts:
                    self._hostids[h[self.cfg.hostField]] = h["hostid"]
            hostids = [self._hostids[h] for h in wanted if h in self._hostids]
            triggers = []
            if hostids:
                triggers = await self._call("trigger.get", {
                    "output": ["triggerid", "description", "priority", "lastchange"],
                    "hostids": hostids,
                    "filter": {"value": 1},
                    "monitored": True,
                    "skipDependent": True,
                    "expandDescription": True,
                    "min_severity": self.cfg.minSeverity,
                    "selectHosts": ["hostid", "host", "name"],
                    "sortfield": "priority",
                    "sortorder": "DESC",
                })
        except (httpx.HTTPError, ValueError, ZabbixError, KeyError) as e:
            self.last_error = f"Zabbix: {e}"
            log.warning("%s", self.last_error)
            return
        alerts: dict[str, list[Alert]] = {}
        for t in triggers:
            since = datetime.fromtimestamp(int(t.get("lastchange") or 0), tz=timezone.utc)
            url = None
            if self.cfg.webUrl:
                url = f"{self.cfg.webUrl.rstrip('/')}/zabbix.php?action=problem.view&triggerids%5B%5D={t['triggerid']}"
            for h in t.get("hosts", []):
                key = h.get(self.cfg.hostField)
                alerts.setdefault(key, []).append(Alert(
                    source="zabbix", severity=_SEVERITY.get(int(t.get("priority", 0)), Severity.info),
                    name=t.get("description", ""), host=h.get("name") or key, since=since, url=url))
        self._alerts = alerts
        self.last_success, self.last_error = utcnow(), ""
        unmapped = [h for h in wanted if h not in self._hostids]
        if unmapped:
            log.debug("Zabbix: hostitelé bez shody: %s", ", ".join(unmapped[:20]))

    def apply(self, chains: list[Chain], locations: list[Location]) -> None:
        for loc in locations:
            loc.alerts += self._alerts.get(self.host_for(loc.name), [])
        for ch in chains:
            ch.alerts += self._alerts.get(self.host_for(ch.location.name), [])

    def status(self):  # type: ignore[no-untyped-def]
        s = super().status()
        s.details = {"url": self.cfg.url, "mappedHosts": len(self._hostids),
                     "activeProblems": sum(len(v) for v in self._alerts.values())}
        return s

    async def close(self) -> None:
        await self._client.aclose()
