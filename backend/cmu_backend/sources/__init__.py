"""Registr zdrojů řetězců. Nový typ zdroje: třída odvozená od Source + větev v build()."""

from __future__ import annotations

import httpx

from ..operations import LocalOperations
from ..settings import AgentSourceSettings, DemoSourceSettings, KomodoSourceSettings, Settings
from .agent import AgentSource
from .base import NotSupported, Source, SourceError
from .demo import DemoSource
from .komodo import KomodoSource

__all__ = ["AgentSource", "DemoSource", "KomodoSource", "NotSupported", "Source", "SourceError", "build"]


def build(settings: Settings, local_ops: LocalOperations,
          transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> list[Source]:
    """transports: id zdroje → HTTP transport (pro testy se simulovanými API)."""
    transports = transports or {}
    out: list[Source] = []
    ids: set[str] = set()
    for cfg in settings.sources:
        if cfg.id in ids:
            raise ValueError(f"zdroj s id {cfg.id} je v konfiguraci dvakrát")
        if ":" in cfg.id:
            raise ValueError(f"id zdroje nesmí obsahovat ':' ({cfg.id})")
        ids.add(cfg.id)
        stale = settings.stale_after
        if isinstance(cfg, AgentSourceSettings):
            out.append(AgentSource(cfg, stale, local_ops, transports.get(cfg.id)))
        elif isinstance(cfg, KomodoSourceSettings):
            out.append(KomodoSource(cfg, stale, local_ops, transports.get(cfg.id)))
        elif isinstance(cfg, DemoSourceSettings):
            out.append(DemoSource(cfg, stale, local_ops))
    return out
