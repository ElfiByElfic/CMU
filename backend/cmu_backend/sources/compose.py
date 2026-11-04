"""Generátor docker-compose pro řetězec kontejnerů.

Pořadí aplikací = směr toku dat. Každá služba dostane stejný spouštěcí kontrakt jako
procesy u agenta (CHAIN_*, APP_*), jen sousedé jsou adresovaní jménem služby v síti
compose. Start probíhá odzadu: služba závisí na své downstream službě
(`depends_on … condition: service_healthy`), takže vstup řetězce startuje poslední.
Labely `cmu.*` nesou pořadí a jméno řetězce, aby je backend uměl zpětně přečíst z Komoda.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import yaml

from ..model import ContainerAppSpec

RESERVED_ENV = {
    "CHAIN_ID", "CHAIN_INSTANCE_ID", "CHAIN_POSITION", "CHAIN_UPSTREAM_URL", "CHAIN_DOWNSTREAM_URL",
    "APP_BIND_ADDRESS", "APP_PORT",
}
LABEL_CHAIN_ID = "cmu.chain.id"
LABEL_CHAIN_NAME = "cmu.chain.name"
LABEL_POSITION = "cmu.chain.position"
LABEL_APP = "cmu.app.name"


def slugify(text: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:40]


def stack_name(prefix: str, chain_name: str) -> str:
    return f"{prefix}{slugify(chain_name) or 'chain'}"


def validate(apps: list[ContainerAppSpec]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if len(apps) < 2:
        errors.append("řetězec potřebuje alespoň 2 kontejnery")
    names = [a.name for a in apps]
    for n in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"služba {n} je v řetězci vícekrát")
    published = [a.publish for a in apps if a.publish]
    for p in sorted({p for p in published if published.count(p) > 1}):
        errors.append(f"port hostitele {p} je použitý vícekrát")
    for a in apps:
        reserved = sorted(set(a.env) & RESERVED_ENV)
        if reserved:
            warnings.append(f"{a.name}: proměnné {', '.join(reserved)} nastavuje CMU, budou přepsány")
        tag = a.image.rsplit("/", 1)[-1]
        if ":" not in tag and "@" not in tag:
            warnings.append(f"{a.name}: image {a.image} nemá tag – použije se latest")
        elif tag.endswith(":latest"):
            warnings.append(f"{a.name}: image s tagem latest není reprodukovatelný")
        if not a.healthcheck:
            warnings.append(f"{a.name}: bez healthchecku se další služba spustí, aniž by čekala na připravenost")
    return errors, warnings


def build(chain_name: str, stack: str, server: str, apps: list[ContainerAppSpec]) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for i, a in enumerate(apps):
        prev = apps[i - 1] if i > 0 else None
        nxt = apps[i + 1] if i + 1 < len(apps) else None
        env = {k: v for k, v in a.env.items() if k not in RESERVED_ENV}
        env.update({
            "CHAIN_ID": stack,
            "CHAIN_INSTANCE_ID": f"{server}/{stack}/{a.name}",
            "CHAIN_POSITION": str(i),
            "CHAIN_UPSTREAM_URL": f"http://{prev.name}:{prev.port}" if prev else "",
            "CHAIN_DOWNSTREAM_URL": f"http://{nxt.name}:{nxt.port}" if nxt else "",
            "APP_BIND_ADDRESS": "0.0.0.0",
            "APP_PORT": str(a.port),
        })
        svc: dict[str, Any] = {
            "image": a.image,
            "restart": "unless-stopped",
            "environment": env,
            "labels": {
                LABEL_CHAIN_ID: stack,
                LABEL_CHAIN_NAME: chain_name,
                LABEL_POSITION: str(i),
                LABEL_APP: a.name,
            },
        }
        if a.healthcheck:
            svc["healthcheck"] = {
                "test": ["CMD-SHELL", f"curl -fsS http://127.0.0.1:{a.port}/api/v1/health/ready >/dev/null || exit 1"],
                "interval": "10s",
                "timeout": "3s",
                "retries": 3,
                "start_period": "20s",
            }
        if nxt is not None:
            cond = "service_healthy" if nxt.healthcheck else "service_started"
            svc["depends_on"] = {nxt.name: {"condition": cond}}
        if a.publish:
            svc["ports"] = [f"{a.publish}:{a.port}"]
        services[a.name] = svc
    return {"name": stack, "services": services}


def render(compose: dict[str, Any]) -> str:
    header = "# Vygenerováno CMU – změny dělejte v CMU, ne ručně v Komodu.\n"
    return header + yaml.safe_dump(compose, sort_keys=False, allow_unicode=True, width=120)
