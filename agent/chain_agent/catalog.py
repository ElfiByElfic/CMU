"""Katalog aplikací na VM.

Rozložení:  <catalogDir>/<Type>/<version>/manifest.json
Manifest:
    {
      "type": "App2",
      "version": "2.4.1",
      "executable": "bin/App2",
      "args": [],
      "configFormat": "ini",                 # ini | json (výchozí json)
      "defaultConfig": "config/default.ini"  # volitelné, vrstva 1
    }
Instalaci verzí do katalogu dělá deploy (CI), agent katalog jen čte.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import appconfig

log = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    type: str
    version: str
    executable: Path
    args: list[str] = field(default_factory=list)
    config_format: str = "json"
    default_config: dict[str, Any] = field(default_factory=dict)


def version_key(v: str) -> tuple:
    """2.10.0 > 2.9.1; vydaná verze > předběžná (2.5.0 > 2.5.0-rc1)."""
    base, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", base))
    return (nums, pre == "", pre)


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._entries: dict[tuple[str, str], CatalogEntry] = {}

    def rescan(self) -> None:
        entries: dict[tuple[str, str], CatalogEntry] = {}
        if self.root.is_dir():
            for manifest in sorted(self.root.glob("*/*/manifest.json")):
                try:
                    entry = self._load(manifest)
                except Exception as e:  # vadný manifest nesmí shodit celý katalog
                    log.warning("katalog: %s přeskočen: %s", manifest, e)
                    continue
                entries[(entry.type, entry.version)] = entry
        self._entries = entries

    @staticmethod
    def _load(manifest: Path) -> CatalogEntry:
        base = manifest.parent
        m = json.loads(manifest.read_text(encoding="utf-8"))
        exe = (base / m["executable"]).resolve()
        if not exe.is_file():
            raise FileNotFoundError(exe)
        fmt = m.get("configFormat", "json")
        if fmt not in ("json", "ini"):
            raise ValueError(f"configFormat {fmt}")
        default: dict[str, Any] = {}
        if m.get("defaultConfig"):
            default = appconfig.load_file(base / m["defaultConfig"])
        return CatalogEntry(
            type=m.get("type", base.parent.name),
            version=m.get("version", base.name),
            executable=exe,
            args=[str(a) for a in m.get("args", [])],
            config_format=fmt,
            default_config=default,
        )

    def has(self, type_: str, version: str) -> bool:
        return (type_, version) in self._entries

    def get(self, type_: str, version: str) -> CatalogEntry | None:
        return self._entries.get((type_, version))

    def to_json(self) -> list[dict[str, Any]]:
        by_type: dict[str, list[str]] = {}
        for t, v in self._entries:
            by_type.setdefault(t, []).append(v)
        return [
            {"type": t, "versions": sorted(vs, key=version_key, reverse=True)}
            for t, vs in sorted(by_type.items())
        ]
