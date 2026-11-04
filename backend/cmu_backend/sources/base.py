"""Rozhraní zdroje řetězců.

Zdroj je systém, který řetězce skutečně provozuje (chain-agent na VM, Komodo, …).
Backend se zdroje periodicky ptá (`refresh`) a mezi dotazy drží poslední stav v paměti.
Nový typ zdroje = nová třída odvozená od `Source` + položka v `sources/__init__.py`.
"""

from __future__ import annotations

import abc
from datetime import datetime

from ..model import (
    CatalogEntry,
    Chain,
    ChainCreate,
    ChainValidation,
    ComponentStatus,
    Location,
    Operation,
    SourceRef,
    Target,
)
from ..operations import LocalOperations


class SourceError(Exception):
    """Zdroj odmítl požadavek nebo neodpovídá. `status` je HTTP kód pro klienta backendu."""

    def __init__(self, message: str, status: int = 502, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []


class NotSupported(SourceError):
    def __init__(self, what: str) -> None:
        super().__init__(f"zdroj nepodporuje: {what}", status=409)


class Source(abc.ABC):
    type: str = "abstract"

    def __init__(self, source_id: str, stale_after_s: float, local_ops: LocalOperations | None = None) -> None:
        self.id = source_id
        self.stale_after_s = stale_after_s
        self.local_ops = local_ops or LocalOperations()
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.last_error: str = ""

    @property
    def ref(self) -> SourceRef:
        return SourceRef(id=self.id, type=self.type)

    # ------------------------------------------------------------------ čtení (z paměti)
    @abc.abstractmethod
    async def refresh(self) -> None:
        """Načte aktuální stav ze systému. Chyby jednotlivých lokalit nesmí shodit ostatní."""

    @abc.abstractmethod
    def chains(self, now: datetime) -> list[Chain]:
        """Řetězce z posledního stavu. Zastaralé lokality → stale=True, state=unknown."""

    @abc.abstractmethod
    def locations(self, now: datetime) -> list[Location]: ...

    def status(self) -> ComponentStatus:
        return ComponentStatus(
            id=self.id, type=self.type, role="source", ok=not self.last_error,
            message=self.last_error, lastSuccess=self.last_success, lastAttempt=self.last_attempt,
        )

    async def close(self) -> None:  # noqa: B027 – výchozí implementace nic nedělá
        pass

    # ------------------------------------------------------------------ akce
    async def chain_action(self, chain: Chain, action: str) -> Operation:
        raise NotSupported(f"akce řetězce {action}")

    async def delete_chain(self, chain: Chain) -> Operation:
        raise NotSupported("odstranění řetězce")

    async def app_action(self, chain: Chain, app_id: str, action: str) -> Operation:
        raise NotSupported(f"akce aplikace {action}")

    async def app_logs(self, chain: Chain, app_id: str, tail: int) -> list[str]:
        raise NotSupported("logy")

    async def operation(self, location: str, op_id: str) -> Operation:
        raise NotSupported("sledování operací")

    # ------------------------------------------------------------------ vytváření
    def targets(self, now: datetime) -> list[Target]:
        return []

    async def catalog(self, location: str) -> list[CatalogEntry]:
        raise NotSupported("katalog")

    async def validate_chain(self, req: ChainCreate) -> ChainValidation:
        raise NotSupported("vytváření řetězců")

    async def create_chain(self, req: ChainCreate) -> Operation:
        raise NotSupported("vytváření řetězců")
