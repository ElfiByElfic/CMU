"""Implementace agenta musí pokrývat všechny cesty z api/agent-api.yaml."""

import re
import unittest
from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from chain_agent.api import create_app
from chain_agent.config import AgentConfig

SPEC = Path(__file__).resolve().parents[2] / "api" / "agent-api.yaml"


def norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


class SpecTest(unittest.TestCase):
    def test_all_spec_operations_implemented(self):
        spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
        prefix = "/agent/v1"
        expected = {
            (m.upper(), norm(prefix + p))
            for p, item in spec["paths"].items()
            for m in item
            if m in ("get", "post", "put", "patch", "delete")
        }
        app = create_app(AgentConfig(vmName="x", controlTokens=["t"]))
        implemented = {
            (m, norm(r.path)) for r in app.routes if isinstance(r, APIRoute) for m in r.methods
        }
        self.assertEqual(sorted(expected - implemented), [], "ve specifikaci, ale neimplementováno")
        self.assertEqual(sorted(implemented - expected), [], "implementováno, ale chybí ve specifikaci")


if __name__ == "__main__":
    unittest.main()
