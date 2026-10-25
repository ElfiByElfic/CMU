import tempfile
import unittest
from pathlib import Path

from chain_agent import appconfig
from chain_agent.catalog import version_key
from chain_agent.manager import merge_patch


class AppConfigTest(unittest.TestCase):
    def test_layers_and_runtime_section(self):
        cfg = appconfig.build(
            [{"logLevel": "INFO", "db": {"host": "a", "port": 1}}, {"db": {"host": "b"}}, {"logLevel": "DEBUG"}],
            {"port": 8500},
        )
        self.assertEqual(cfg, {"logLevel": "DEBUG", "db": {"host": "b", "port": 1}, "chain": {"port": 8500}})

    def test_ini_render_and_parse_roundtrip(self):
        cfg = {"logLevel": "INFO", "verbose": True, "db": {"host": "x", "pool": {"max": 5}}}
        text = appconfig.render(cfg, "ini")
        self.assertTrue(text.startswith("[General]\n"))
        self.assertIn("verbose=true", text)
        self.assertIn("pool.max=5", text)
        self.assertEqual(appconfig.parse_ini(text),
                         {"logLevel": "INFO", "verbose": "true", "db": {"host": "x", "pool.max": "5"}})

    def test_json_render_is_stable(self):
        a = appconfig.render({"b": 1, "a": {"y": 2, "x": 1}}, "json")
        b = appconfig.render({"a": {"x": 1, "y": 2}, "b": 1}, "json")
        self.assertEqual(appconfig.etag(a), appconfig.etag(b))

    def test_secrets_resolve_and_mask(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pw").write_text("tajne\n")
            cfg = {"db": {"password": "secret://pw", "user": "u"}}
            self.assertEqual(appconfig.secret_refs(cfg), ["pw"])
            self.assertEqual(appconfig.resolve_secrets(cfg, Path(d))["db"]["password"], "tajne")
            self.assertEqual(appconfig.resolve_secrets(cfg, None)["db"]["password"], appconfig.MASK)

    def test_merge_patch(self):
        self.assertEqual(
            merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"a": None, "b": {"c": 9}, "e": 5}),
            {"b": {"c": 9, "d": 3}, "e": 5},
        )

    def test_version_order(self):
        vs = ["2.4.0", "2.10.0", "2.5.0-rc1", "2.5.0"]
        self.assertEqual(sorted(vs, key=version_key, reverse=True), ["2.10.0", "2.5.0", "2.5.0-rc1", "2.4.0"])


if __name__ == "__main__":
    unittest.main()
