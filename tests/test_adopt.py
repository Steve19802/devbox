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

import adopt  # noqa: E402
import devbox_config as dc  # noqa: E402


class _FakeResult:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def docker_patch(payload=None, fail=False):
    """Patch ``subprocess.run`` for the docker discovery call."""
    if fail:
        return mock.patch("subprocess.run", side_effect=FileNotFoundError("docker"))
    return mock.patch(
        "subprocess.run",
        return_value=_FakeResult(stdout=json.dumps(payload)),
    )


class AdoptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------- #
    def write(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
        with open(path, mode, **kwargs) as f:
            f.write(content)
        return path

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def read_bytes(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

    def run_main(self, argv, patch=None):
        buf = io.StringIO()
        ctx = patch if patch is not None else docker_patch(fail=True)
        with ctx:
            with redirect_stdout(buf):
                rc = adopt.main(argv)
        return rc, buf.getvalue()

    def snapshot(self):
        data = {}
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                st = os.stat(path)
                with open(path, "rb") as f:
                    data[os.path.relpath(path, self.root)] = (
                        st.st_mtime_ns,
                        f.read(),
                    )
        return data


class SingleRootAppTest(AdoptTestCase):
    def test_root_dockerfile_workdir(self):
        self.write("Dockerfile", "FROM node:24-alpine AS prod\nWORKDIR /usr/src/app\n")
        rc, out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)

        cfg = dc.load_config(self.root)
        self.assertEqual(cfg["mode"], "adopt")
        self.assertEqual(len(cfg["services"]), 1)
        svc = cfg["services"][0]
        self.assertEqual(svc["source"], ".")
        self.assertEqual(svc["workdir"], "/usr/src/app")
        self.assertEqual(svc["context"], ".")
        self.assertFalse(svc["managed"])

        dev = self.read("docker-compose.dev.yml")
        self.assertIn(".:/usr/src/app", dev)
        self.assertIn('user: "${DEVBOX_UID:-1000}:${DEVBOX_GID:-1000}"', dev)
        self.assertNotIn("env_file", dev)
        self.assertNotIn("env_file", self.read("docker-compose.yml"))

        # No env_file anywhere in the project.
        for _dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                with open(os.path.join(_dirpath, name), encoding="utf-8") as f:
                    self.assertNotIn("env_file", f.read(), name)


class ComposeDiscoveryTest(AdoptTestCase):
    def _build_docker_payload(self):
        return {
            "services": {
                "web": {
                    "build": {
                        "context": os.path.join(self.root, "apps", "web"),
                        "dockerfile": "Dockerfile",
                    },
                    "ports": [{"target": 5173, "published": "5173"}],
                    "volumes": [],
                },
                "api": {
                    "build": {
                        "context": os.path.join(self.root, "services", "api"),
                    },
                    "ports": [{"target": 8000, "published": "8000"}],
                },
                "db": {
                    "image": "postgres:16",
                    "ports": [{"target": 5432, "published": "5432"}],
                },
            }
        }

    def _write_monorepo(self):
        self.write(
            "apps/web/Dockerfile",
            "FROM node:24-alpine AS prod\nWORKDIR /app\n"
            "FROM prod AS dev\nCMD [\"sleep\", \"infinity\"]\n",
        )
        self.write("apps/web/package.json", "{}")
        self.write("services/api/Dockerfile", "FROM python:3.11-slim\nWORKDIR /code\n")
        self.write("services/api/requirements.txt", "")

    def test_compose_via_docker_json(self):
        self._write_monorepo()
        compose = self.write("docker-compose.yml", "ORIGINAL COMPOSE\n")
        original = self.read_bytes("docker-compose.yml")

        rc, _out = self.run_main(
            [self.root, "--yes"],
            patch=docker_patch(self._build_docker_payload()),
        )
        self.assertEqual(rc, 0)

        cfg = dc.load_config(self.root)
        expected = dc.new_config(
            "adopt",
            [
                dc.make_service(
                    "web", "apps/web", workdir="/app", context="apps/web",
                    dockerfile="Dockerfile", target="dev", port=5173,
                    runtime="node", managed=False,
                ),
                dc.make_service(
                    "api", "services/api", workdir="/code", context="services/api",
                    dockerfile="Dockerfile", target=None, port=8000,
                    runtime="python", managed=False,
                ),
                dc.make_service("db", None, port=5432, managed=False),
            ],
        )
        self.assertEqual(cfg, expected)

        # User compose file is byte-identical.
        self.assertEqual(self.read_bytes("docker-compose.yml"), original)

        dev = self.read("docker-compose.dev.yml")
        self.assertIn("./apps/web:/app", dev)
        self.assertIn("./services/api:/code", dev)
        self.assertIn("target: dev", dev)
        self.assertNotIn("env_file", dev)
        # db is image-only: no dev override.
        self.assertNotIn("\n  db:", dev)

    def test_compose_via_fallback_parser(self):
        self._write_monorepo()
        self.write(
            "docker-compose.yml",
            """
version: "3.9"

services:
  web-app:
    build: ./apps/web
    ports:
      - "8080:80"
  api-service:
    build:
      context: ./services/api
      dockerfile: Dockerfile
    ports:
      - "127.0.0.1:8000:8000"
  db:
    image: postgres:16
    ports:
      - "5432:5432"
""",
        )
        compose_path = os.path.join(self.root, "docker-compose.yml")

        def no_docker(*args, **kwargs):
            raise FileNotFoundError("docker")

        services = adopt.discover_compose(self.root, compose_path, run=no_docker)
        by_name = {s["name"]: s for s in services}
        self.assertEqual(set(by_name), {"web-app", "api-service", "db"})
        self.assertEqual(by_name["web-app"]["build_context"], "apps/web")
        self.assertEqual(by_name["web-app"]["ports"], [80])
        self.assertEqual(by_name["api-service"]["build_context"], "services/api")
        self.assertEqual(by_name["api-service"]["ports"], [8000])
        self.assertEqual(by_name["db"]["ports"], [5432])

        rc, _out = self.run_main([self.root, "--yes"], patch=docker_patch(fail=True))
        self.assertEqual(rc, 0)
        cfg = dc.load_config(self.root)
        names = [s["name"] for s in cfg["services"]]
        self.assertEqual(names, ["web-app", "api-service", "db"])
        web = dc.get_service(cfg, "web-app")
        self.assertEqual(web["source"], "apps/web")
        self.assertEqual(web["target"], "dev")
        api = dc.get_service(cfg, "api-service")
        self.assertEqual(api["source"], "services/api")
        self.assertEqual(api["workdir"], "/code")
        self.assertIsNone(dc.get_service(cfg, "db")["source"])

    def test_existing_bind_mount_is_reused(self):
        self.write("web/Dockerfile", "FROM node:24-alpine\nWORKDIR /app\n")
        self.write(
            "docker-compose.yml",
            """
services:
  web:
    build: ./web
    working_dir: /srv/app
    volumes:
      - ./web:/srv/app
""",
        )
        rc, _out = self.run_main([self.root, "--yes"], patch=docker_patch(fail=True))
        self.assertEqual(rc, 0)
        cfg = dc.load_config(self.root)
        web = dc.get_service(cfg, "web")
        self.assertEqual(web["source"], "web")
        self.assertEqual(web["workdir"], "/srv/app")
        dev = self.read("docker-compose.dev.yml")
        self.assertNotIn("./web:/srv/app", dev)


class RuntimeFallbackTest(AdoptTestCase):
    def test_package_json_dry_run_writes_nothing(self):
        self.write("package.json", json.dumps({"name": "demo"}))
        before = self.snapshot()
        rc, out = self.run_main([self.root, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "Dockerfile")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "devbox.json")))
        self.assertIn("create (from node template)", out)

    def test_node_package_json_creates_dockerfile(self):
        self.write("package.json", json.dumps({"name": "demo"}))
        rc, _out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "Dockerfile")))
        content = self.read("Dockerfile")
        self.assertIn("WORKDIR /app", content)
        self.assertNotIn(adopt.IDE_INJECTION_MARKER, content)

        cfg = dc.load_config(self.root)
        self.assertEqual(cfg.get("generated_files"), ["Dockerfile"])
        svc = cfg["services"][0]
        self.assertEqual(svc["runtime"], "node")
        self.assertEqual(svc["source"], ".")
        self.assertFalse(svc["managed"])

    def test_unknown_runtime_exits_1(self):
        self.write("README.md", "nothing here")
        rc, out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("devbox-init", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "devbox.json")))


class UserFilePreservationTest(AdoptTestCase):
    def test_user_architecture_and_gitignore_preserved(self):
        self.write("Dockerfile", "FROM node:24-alpine\nWORKDIR /app\n")
        self.write("ARCHITECTURE.md", "# My own doc\n")
        self.write(".gitignore", "node_modules/\n")
        rc, out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)

        self.assertEqual(self.read("ARCHITECTURE.md"), "# My own doc\n")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "DEVBOX.md")))
        self.assertIn(dc.MD_BEGIN, self.read("DEVBOX.md"))

        gitignore = self.read(".gitignore")
        self.assertTrue(gitignore.startswith("node_modules/\n"))
        self.assertIn(dc.HASH_BEGIN, gitignore)
        self.assertIn(".opencode/", gitignore)

        # Statuses must reflect that existing files were appended to, not created.
        self.assertIn("block added", out)
        self.assertIn("block created", out)  # DEVBOX.md did not exist
        self.assertNotIn(".gitignore created", out)

    def test_existing_blocks_report_updated_or_unchanged(self):
        self.write("Dockerfile", "FROM node:24-alpine\nWORKDIR /app\n")
        # First run creates the .gitignore block and ARCHITECTURE.md.
        rc, _out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        # Second run (keep existing config) must report unchanged, not recreated.
        rc, out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)


class ModesTest(AdoptTestCase):
    def setUp(self):
        super().setUp()
        self.write("Dockerfile", "FROM node:24-alpine AS prod\nWORKDIR /app\n")

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        rc, out = self.run_main([self.root, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "devbox.json")))

    def test_yes_is_non_interactive_and_idempotent(self):
        with mock.patch("builtins.input", side_effect=AssertionError("no prompt")):
            rc, _out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        first = self.snapshot()

        with mock.patch("builtins.input", side_effect=AssertionError("no prompt")):
            rc, _out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        second = self.snapshot()
        self.assertEqual(first, second)

    def test_existing_dev_override_skipped_with_yes(self):
        self.write("docker-compose.dev.yml", "ORIGINAL\n")
        rc, out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.read("docker-compose.dev.yml"), "ORIGINAL\n")
        self.assertIn("skip", out)

    def test_eof_input_uses_defaults_without_crashing(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            rc, _out = self.run_main([self.root])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "devbox.json")))

    def test_existing_config_interactive_offer_keeps(self):
        rc, _out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        before = self.snapshot()
        # Decline "re-run discovery", accept the .gitignore prompt.
        fake_input = mock.Mock(side_effect=["n", ""])
        with mock.patch("builtins.input", fake_input):
            rc, _out = self.run_main([self.root])
        self.assertEqual(rc, 0)
        self.assertTrue(
            any("Re-run discovery" in str(c) for c in fake_input.call_args_list)
        )
        self.assertEqual(self.snapshot(), before)

    def test_v1_config_is_loaded_not_crashing(self):
        v1 = {
            "version": 1,
            "service": "web",
            "dockerfile": "Dockerfile",
            "dockerfile_context": ".",
            "port": 3000,
            "stages": {"dev": "dev"},
        }
        original = json.dumps(v1, indent=2) + "\n"
        self.write("devbox.json", original)
        rc, out = self.run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("devbox.json", out)
        self.assertIn("normaliz", out.lower())
        # Existing (v1) file is kept untouched.
        self.assertEqual(self.read("devbox.json"), original)
        # But the missing dev override is generated.
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "docker-compose.dev.yml"))
        )


class CliTest(AdoptTestCase):
    def test_unknown_flag_exit_2(self):
        rc, out = self.run_main([self.root, "--nope"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", out.lower())

    def test_invalid_service_spec_exit_2(self):
        rc, _out = self.run_main([self.root, "--service", "onlyname"])
        self.assertEqual(rc, 2)

    def test_service_override(self):
        self.write("web/Dockerfile", "FROM node:24-alpine AS dev\nWORKDIR /app\n")
        rc, _out = self.run_main(
            [self.root, "--yes", "--service", "web:web:/srv/myapp"]
        )
        self.assertEqual(rc, 0)
        cfg = dc.load_config(self.root)
        web = dc.get_service(cfg, "web")
        self.assertEqual(web["source"], "web")
        self.assertEqual(web["workdir"], "/srv/myapp")
        dev = self.read("docker-compose.dev.yml")
        self.assertIn("./web:/srv/myapp", dev)


class DockerfileAnalysisTest(AdoptTestCase):
    def _analysis(self, content):
        path = self.write("Dockerfile", content)
        return adopt.parse_dockerfile(path)

    def test_multistage_workdir_inheritance(self):
        analysis = self._analysis(
            "FROM node:24 AS base\nWORKDIR /srv\nFROM base AS dev\n"
        )
        self.assertEqual(adopt.dockerfile_dev_target(analysis), "dev")
        self.assertEqual(adopt.dockerfile_workdir(analysis, "dev"), "/srv")

    def test_relative_workdir_joins(self):
        analysis = self._analysis(
            "FROM node:24 AS dev\nWORKDIR /app\nWORKDIR sub\n"
        )
        self.assertEqual(adopt.dockerfile_workdir(analysis, "dev"), "/app/sub")

    def test_variable_workdir_falls_back(self):
        analysis = self._analysis("FROM node:24 AS dev\nWORKDIR $APP_DIR/src\n")
        self.assertEqual(adopt.dockerfile_workdir(analysis, "dev"), "/app")
        self.assertTrue(any("variable" in w for w in analysis["warnings"]))

    def test_expose_parsing(self):
        analysis = self._analysis(
            "FROM node:24 AS dev\nEXPOSE 3000 8080/tcp\n"
        )
        self.assertEqual(analysis["stages"][0]["expose"], [3000, 8080])
        self.assertEqual(adopt.dockerfile_expose(analysis, "dev"), 3000)

    def test_dev_stage_detection_case_insensitive(self):
        analysis = self._analysis("FROM node:24 AS DEV\n")
        self.assertEqual(adopt.dockerfile_dev_target(analysis), "DEV")

    def test_missing_file_is_empty(self):
        analysis = adopt.parse_dockerfile(os.path.join(self.root, "nope"))
        self.assertFalse(analysis["exists"])
        self.assertEqual(analysis["stages"], [])


class ArgParsingTest(AdoptTestCase):
    def test_parse_service_spec(self):
        spec = adopt.parse_service_spec("web:apps/web:/srv", self.root)
        self.assertEqual(spec, {"name": "web", "source": "apps/web", "workdir": "/srv"})

    def test_parse_service_spec_relative_workdir_rejected(self):
        with self.assertRaises(adopt.UsageError):
            adopt.parse_service_spec("web:apps/web:relative", self.root)

    def test_parse_service_spec_escape_rejected(self):
        with self.assertRaises(adopt.UsageError):
            adopt.parse_service_spec("web:../outside", self.root)


class FallbackParserTest(AdoptTestCase):
    def test_long_form_ports_build_and_comments(self):
        text = """
# a comment
version: "3"
services:
  "web-app":            # quoted, hyphenated
    build:
      context: "./apps/web"
      target: dev
    ports:
      - target: 80
        published: 8080
      - "127.0.0.1:9000:9000"
    volumes:
      - "./apps/web:/app:ro"
      - named_volume:/data
  api:
    image: python:3.11-slim
"""
        services = adopt.parse_compose_fallback(text)
        self.assertEqual(set(services), {"web-app", "api"})
        web = services["web-app"]
        self.assertEqual(web["build"]["context"], "./apps/web")
        self.assertEqual(web["build"]["target"], "dev")
        ports = adopt._parse_ports(web["ports"])
        self.assertEqual(ports, [80, 9000])
        binds = adopt._parse_binds(web["volumes"], self.root)
        self.assertEqual(len(binds), 1)  # named_volume is skipped
        self.assertEqual(binds[0]["target"], "/app")
        self.assertEqual(binds[0]["source"], "apps/web")
        self.assertEqual(services["api"]["image"], "python:3.11-slim")


if __name__ == "__main__":
    unittest.main()
