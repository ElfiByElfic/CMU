"""Konfigurace aplikací: skládání vrstev, tajné hodnoty a výstup jako INI nebo JSON.

Vnitřní tvar je slovník; INI sekce odpovídá vnořenému objektu:

    {"logLevel": "INFO", "database": {"host": "db1", "port": 5432}}

    [General]            ; skalární hodnoty nejvyšší úrovně (QSettings je čte jako kořen)
    logLevel=INFO
    [database]
    host=db1
    port=5432

Pořadí vrstev (pozdější přepisují dřívější):
  1. výchozí konfigurace verze aplikace z katalogu (defaultConfig v manifestu)
  2. prostředí VM (envConfigDir/<Type>.json|.ini)
  3. řetězec/instance (AppSpec.config – edituje se z GUI)
  4. runtime – sekce "chain" (port, sousedé, ID); doplňuje agent, nejde přepsat
"""

from __future__ import annotations

import configparser
import copy
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SECRET_PREFIX = "secret://"
MASK = "***"
RUNTIME_SECTION = "chain"
INI_ROOT_SECTION = "General"


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_file(path: Path) -> dict[str, Any]:
    """Načte .json nebo .ini do vnitřního tvaru."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".ini":
        return parse_ini(text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: očekáván JSON objekt")
    return data


def parse_ini(text: str) -> dict[str, Any]:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # zachovat velikost písmen
    cp.read_string(text)
    out: dict[str, Any] = {}
    for section in cp.sections():
        values = dict(cp.items(section))
        if section == INI_ROOT_SECTION:
            out.update(values)
        else:
            out[section] = values
    return out


def build(layers: Sequence[Mapping[str, Any]], runtime: Mapping[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for layer in layers:
        cfg = deep_merge(cfg, layer)
    cfg[RUNTIME_SECTION] = dict(runtime)
    return cfg


def secret_refs(cfg: Any) -> list[str]:
    """Jména všech secret://<name> odkazů v konfiguraci."""
    if isinstance(cfg, Mapping):
        return [n for v in cfg.values() for n in secret_refs(v)]
    if isinstance(cfg, list):
        return [n for v in cfg for n in secret_refs(v)]
    if isinstance(cfg, str) and cfg.startswith(SECRET_PREFIX):
        return [cfg[len(SECRET_PREFIX):]]
    return []


def resolve_secrets(cfg: Any, secrets_dir: Path | None) -> Any:
    """secrets_dir=None → hodnoty se jen zamaskují (pro zobrazení v GUI)."""
    if isinstance(cfg, Mapping):
        return {k: resolve_secrets(v, secrets_dir) for k, v in cfg.items()}
    if isinstance(cfg, list):
        return [resolve_secrets(v, secrets_dir) for v in cfg]
    if isinstance(cfg, str) and cfg.startswith(SECRET_PREFIX):
        if secrets_dir is None:
            return MASK
        name = cfg[len(SECRET_PREFIX):]
        return (secrets_dir / name).read_text(encoding="utf-8").strip()
    return cfg


def _ini_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _flatten(prefix: str, d: Mapping[str, Any], out: dict[str, str]) -> None:
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            _flatten(f"{key}.", v, out)
        else:
            out[key] = _ini_value(v)


def render(cfg: Mapping[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if fmt != "ini":
        raise ValueError(f"neznámý formát {fmt}")
    root = {k: _ini_value(v) for k, v in cfg.items() if not isinstance(v, Mapping)}
    sections: dict[str, dict[str, str]] = {}
    for k, v in cfg.items():
        if isinstance(v, Mapping):
            flat: dict[str, str] = {}
            _flatten("", v, flat)  # hlubší úrovně jako klíče "a.b"
            sections[k] = flat
    buf = io.StringIO()
    for name, values in ([(INI_ROOT_SECTION, root)] if root else []) + sorted(sections.items()):
        buf.write(f"[{name}]\n")
        for key in sorted(values):
            buf.write(f"{key}={values[key]}\n")
        buf.write("\n")
    return buf.getvalue()


def etag(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
