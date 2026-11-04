"""Jádro backendu: periodicky obnovuje zdroje a integrace a skládá z nich jednotný pohled.

Sběr dat je oddělený od obsluhy požadavků – API čte jen stav v paměti, nikdy se při
požadavku nedotazuje stovek agentů. Každý zdroj i integrace má vlastní smyčku, takže
pomalý nebo nedostupný systém nebrzdí ostatní.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable

import httpx

from . import health, integrations, sources
from .integrations import Integration
from .model import HEALTH_ORDER, Chain, ComponentStatus, Location, Summary, utcnow
from .operations import LocalOperations
from .settings import Settings
from .sources import Source

log = logging.getLogger(__name__)


class Core:
    def __init__(self, settings: Settings,
                 transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> None:
        self.settings = settings
        self.local_ops = LocalOperations()
        self.sources: list[Source] = sources.build(settings, self.local_ops, transports)
        self.integrations: list[Integration] = integrations.build(settings, transports)
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ životní cyklus
    async def start(self) -> None:
        # první načtení synchronně, aby API po startu nevracelo prázdný pohled
        await asyncio.gather(*(self._safe(s.refresh, s.id) for s in self.sources))
        await asyncio.gather(*(self._safe(self._integration_refresher(i), i.id) for i in self.integrations))
        for s in self.sources:
            self._tasks.append(asyncio.create_task(self._loop(s.refresh, self.settings.pollIntervalS, s.id)))
        for i in self.integrations:
            self._tasks.append(asyncio.create_task(self._loop(self._integration_refresher(i), i.interval_s, i.id)))

    def _integration_refresher(self, integration: Integration) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            chains, locations = self._raw()
            await integration.refresh(chains, locations)
        return run

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        components: list[Source | Integration] = [*self.sources, *self.integrations]
        for c in components:
            await c.close()

    @staticmethod
    async def _safe(fn: Callable[[], Awaitable[None]], name: str) -> None:
        try:
            await fn()
        except Exception:  # chyba jednoho zdroje nesmí shodit ostatní
            log.exception("obnova %s selhala", name)

    async def _loop(self, fn: Callable[[], Awaitable[None]], interval: float, name: str) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._safe(fn, name)

    async def refresh_now(self) -> None:
        await asyncio.gather(*(self._safe(s.refresh, s.id) for s in self.sources))

    # ------------------------------------------------------------------ pohled
    def _raw(self) -> tuple[list[Chain], list[Location]]:
        now = utcnow()
        chains: list[Chain] = []
        locations: list[Location] = []
        for s in self.sources:
            chains += s.chains(now)
            locations += s.locations(now)
        return chains, locations

    def view(self) -> tuple[list[Chain], list[Location]]:
        chains, locations = self._raw()
        for i in self.integrations:
            try:
                i.apply(chains, locations)
            except Exception:
                log.exception("integrace %s: apply selhalo", i.id)
        for ch in chains:
            ch.health = health.compute(ch)
        chains.sort(key=lambda c: (HEALTH_ORDER[c.health], c.name.casefold(), c.location.name))
        return chains, locations

    def chain(self, chain_id: str) -> Chain | None:
        return next((c for c in self.view()[0] if c.id == chain_id), None)

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def summary(self) -> Summary:
        chains, locations = self.view()
        by_health = Counter(c.health.value for c in chains)
        return Summary(
            chains=len(chains),
            byHealth={h: by_health.get(h, 0) for h in ("problem", "warning", "unknown", "stopped", "ok")},
            byKind=dict(Counter(c.kind.value for c in chains)),
            locations=len(locations),
            unreachableLocations=sum(1 for loc in locations if not loc.reachable),
            generatedAt=utcnow(),
        )

    def components(self) -> list[ComponentStatus]:
        return [s.status() for s in self.sources] + [i.status() for i in self.integrations]
