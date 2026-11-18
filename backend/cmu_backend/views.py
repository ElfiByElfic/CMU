"""Uložené pohledy dashboardu – sdílené pro celý tým, uložené v dataDir/views.json."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qsl, urlencode

from .model import SavedView, SavedViewIn, utcnow
from .store import JsonStore


def normalize_query(query: str) -> str:
    """Stejné filtry v jiném pořadí = stejný pohled (frontend tak pozná, který pohled je otevřený)."""
    pairs = sorted((k, v) for k, v in parse_qsl(query.lstrip("?"), keep_blank_values=False) if v)
    return urlencode(pairs)


class ViewConflict(Exception):
    pass


class ViewStore:
    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._views: dict[str, SavedView] = {
            v["id"]: SavedView.model_validate(v) for v in store.load([]) if isinstance(v, dict)
        }

    def list(self) -> list[SavedView]:
        return sorted(self._views.values(), key=lambda v: v.name.casefold())

    def get(self, view_id: str) -> SavedView | None:
        return self._views.get(view_id)

    def _check_name(self, name: str, exclude: str | None = None) -> str:
        name = name.strip()
        if not name:
            raise ViewConflict("název pohledu nesmí být prázdný")
        if any(v.name.casefold() == name.casefold() and v.id != exclude for v in self._views.values()):
            raise ViewConflict(f"pohled „{name}“ už existuje")
        return name

    def create(self, data: SavedViewIn, created_by: str | None = None) -> SavedView:
        now = utcnow()
        v = SavedView(id=uuid.uuid4().hex[:10], name=self._check_name(data.name),
                      query=normalize_query(data.query), createdAt=now, updatedAt=now, createdBy=created_by)
        self._views[v.id] = v
        self._save()
        return v

    def update(self, view_id: str, data: SavedViewIn) -> SavedView | None:
        v = self._views.get(view_id)
        if v is None:
            return None
        v.name = self._check_name(data.name, exclude=view_id)
        v.query = normalize_query(data.query)
        v.updatedAt = utcnow()
        self._save()
        return v

    def delete(self, view_id: str) -> bool:
        if self._views.pop(view_id, None) is None:
            return False
        self._save()
        return True

    def _save(self) -> None:
        self._store.save([v.model_dump(mode="json") for v in self.list()])
