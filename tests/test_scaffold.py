import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import devbox_config as dc  # noqa: E402
import scaffold  # noqa: E402


class ScaffoldMainTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._argv = sys.argv
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        sys.argv = self._argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        sys.argv = ["scaffold.py"] + list(argv)
        with redirect_stdout(io.StringIO()):
            scaffold.main()

    def test_cli_scaffold_creates_v2_devbox_json(self):
        self._run(["--service", "web:node:5173", "--service", "api:python"])

        path = os.path.join(self.tmp, "devbox.json")
        self.assertTrue(os.path.isfile(path))
        with open(path) as f:
            cfg = json.load(f)

        expected = dc.new_config(
            "scaffold",
            [
                dc.make_service(
                    "web",
                    "src/web",
                    workdir="/app",
                    context="src/web",
                    dockerfile="Dockerfile",
                    target="dev",
                    port=5173,
                    runtime="node",
                    managed=True,
                ),
                dc.make_service(
                    "api",
                    "src/api",
                    workdir="/app",
                    context="src/api",
                    dockerfile="Dockerfile",
                    target="dev",
                    port=8000,
                    runtime="python",
                    managed=True,
                ),
            ],
            inject_ide=False,
        )
        self.assertEqual(cfg, expected)
        self.assertEqual(cfg["version"], 2)
        self.assertEqual(cfg["mode"], "scaffold")
        self.assertEqual(cfg["compose_files"], dc.DEFAULT_COMPOSE_FILES)
        self.assertEqual(cfg["mcp"], dc.DEFAULT_MCP_CONFIG)

        with open(os.path.join(self.tmp, "ARCHITECTURE.md")) as f:
            arch = f.read()
        self.assertIn(dc.MD_BEGIN, arch)
        self.assertIn(dc.MD_END, arch)
        self.assertEqual(arch.count(dc.MD_BEGIN), 1)
        for token in ["src/web", "src/api", "/app", "5173", "8000", "web", "api"]:
            self.assertIn(token, arch)

        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "docker-compose.yml")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.tmp, "docker-compose.dev.yml"))
        )

    def test_generate_architecture_twice_preserves_user_text(self):
        services = [{"name": "web", "runtime": "node", "port": 5173}]
        with redirect_stdout(io.StringIO()):
            scaffold.generate_architecture(services, self.tmp)
        path = os.path.join(self.tmp, "ARCHITECTURE.md")
        with open(path) as f:
            first = f.read()

        modified = "USER PREAMBLE\n\n" + first + "\nUSER POSTAMBLE\n"
        with open(path, "w") as f:
            f.write(modified)

        with redirect_stdout(io.StringIO()):
            scaffold.generate_architecture(services, self.tmp)
        with open(path) as f:
            after = f.read()

        self.assertEqual(after, modified)
        self.assertTrue(after.startswith("USER PREAMBLE\n"))
        self.assertTrue(after.endswith("USER POSTAMBLE\n"))
        self.assertEqual(after.count(dc.MD_BEGIN), 1)
        self.assertIn("src/web", after)

    def test_guard_existing_compose_file(self):
        compose = os.path.join(self.tmp, "docker-compose.yml")
        with open(compose, "w") as f:
            f.write("services: {}\n")

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(buf):
                sys.argv = ["scaffold.py", "--service", "web:node:5173"]
                scaffold.main()
        self.assertEqual(cm.exception.code, 1)
        out = buf.getvalue()
        self.assertIn("devbox-adopt", out)
        self.assertIn("--force", out)
        self.assertIn("docker-compose.yml", out)

        self.assertFalse(os.path.exists(os.path.join(self.tmp, "devbox.json")))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "src")))
        with open(compose) as f:
            self.assertEqual(f.read(), "services: {}\n")

    def test_guard_existing_devbox_json(self):
        with open(os.path.join(self.tmp, "devbox.json"), "w") as f:
            f.write("{}\n")
        with self.assertRaises(SystemExit) as cm:
            self._run(["--service", "web:node:5173"])
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "src")))

    def test_force_proceeds_without_overwriting_compose(self):
        compose = os.path.join(self.tmp, "docker-compose.yml")
        with open(compose, "w") as f:
            f.write("ORIGINAL\n")

        self._run(["--service", "web:node:5173", "--force"])

        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "devbox.json")))
        with open(compose) as f:
            self.assertEqual(f.read(), "ORIGINAL\n")
        self.assertTrue(
            os.path.isfile(os.path.join(self.tmp, "docker-compose.dev.yml"))
        )

    def test_interactive_guard_runs_before_wizard(self):
        with open(os.path.join(self.tmp, "devbox.json"), "w") as f:
            f.write("{}\n")
        sys.argv = ["scaffold.py"]
        with mock.patch(
            "builtins.input", side_effect=AssertionError("wizard must not run")
        ):
            with self.assertRaises(SystemExit) as cm:
                with redirect_stdout(io.StringIO()):
                    scaffold.main()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
