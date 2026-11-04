"""Rozhraní integrace.

Integrace řetězce neprovozuje, jen k nim přidává informace (výstrahy, metriky) nebo
z nich něco poskytuje dalším systémům. Má vlastní interval obnovy a do pohledu se
promítá v `apply`, takže výpadek integrace nikdy nezablokuje zobrazení řetězců.
"""

from __future__ import annotations

import abc
from datetime import datetime

from ..model import Chain, ComponentStatus, Location


class Integration(abc.ABC):
    id: str = "abstract"
    interval_s: float = 30

    def __init__(self) -> None:
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_error: str = ""

    @abc.abstractmethod
    async def refresh(self, chains: list[Chain], locations: list[Location]) -> None:
        """Načte data z externího systému pro aktuální řetězce a lokality."""

    @abc.abstractmethod
    def apply(self, chains: list[Chain], locations: list[Location]) -> None:
        """Doplní načtená data do pohledu (mění objekty na místě)."""

    def status(self) -> ComponentStatus:
        return ComponentStatus(id=self.id, type=self.id, role="integration", ok=not self.last_error,
                               message=self.last_error, lastSuccess=self.last_success,
                               lastAttempt=self.last_attempt)

    async def close(self) -> None:  # noqa: B027
        pass
