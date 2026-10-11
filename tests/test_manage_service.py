import io
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
import manage_service as ms  # noqa: E402

MANIFEST = ms.load_manifest(ms.BUILD_DIR)


def read(path):
    with open(path) as f:
        return f.read()


class BaseTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, fn, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*args)
        return buf.getvalue()

    def expect_exit(self, fn, *args):
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                fn(*args)
        self.assertEqual(cm.exception.code, 1)


class AddWithPathTest(BaseTest):
    def test_add_python_service_in_custom_path(self):
        # Architecture already has a DevBox marker block plus user text.
        arch = os.path.join(self.tmp, "ARCHITECTURE.md")
        with open(arch, "w") as f:
            f.write(
                "USER PREAMBLE\n\n"
                + dc.MD_BEGIN
                + "\nold body\n"
                + dc.MD_END
                + "\n\nUSER POSTAMBLE\n"
            )

        args = {
            "--name": "worker",
            "--runtime": "python",
            "--port": "9000",
            "--path": "apps/worker",
        }
        self.run_cmd(ms.cmd_add, args, self.tmp, MANIFEST)

        # Dockerfile + requirements land in the chosen folder.
        self.assertTrue(os.path.isfile("apps/worker/Dockerfile"))
        self.assertTrue(os.path.isfile("apps/worker/requirements.txt"))
        self.assertFalse(os.path.isdir("src/worker"))

        # Compose blocks use the chosen path.
        yml = read("docker-compose.yml")
        self.assertIn("context: ./apps/worker", yml)
        self.assertIn('"9000:9000"', yml)
        dev = read("docker-compose.dev.yml")
        self.assertIn("./apps/worker:/app", dev)
        self.assertIn("DEVBOX_SERVICE=worker", dev)

        # devbox.json records the managed service.
        cfg = dc.load_config(self.tmp)
        self.assertEqual(cfg["mode"], "scaffold")
        svc = dc.get_service(cfg, "worker")
        self.assertEqual(svc["source"], "apps/worker")
        self.assertEqual(svc["context"], "apps/worker")
        self.assertEqual(svc["workdir"], "/app")
        self.assertEqual(svc["runtime"], "python")
        self.assertEqual(svc["port"], 9000)
        self.assertTrue(svc["managed"])

        # Marker block regenerated; user text outside preserved byte-for-byte.
        after = read(arch)
        self.assertTrue(after.startswith("USER PREAMBLE\n"))
        self.assertTrue(after.endswith("USER POSTAMBLE\n"))
        self.assertIn("apps/worker", after)
        self.assertNotIn("old body", after)
        self.assertEqual(after.count(dc.MD_BEGIN), 1)

    def test_inject_ide_places_nvim_dirs_under_path(self):
        args = {
            "--name": "worker",
            "--runtime": "node",
            "--port": "5173",
            "--path": "apps/worker",
            "--inject-ide": True,
        }
        self.run_cmd(ms.cmd_add, args, self.tmp, MANIFEST)

        self.assertTrue(
            os.path.isdir("apps/worker/.nvim/share/nvim")
        )
        self.assertTrue(
            os.path.isdir("apps/worker/.nvim/state/nvim")
        )
        dev = read("docker-compose.dev.yml")
        self.assertIn("./apps/worker/.nvim/share/nvim:", dev)
        self.assertIn("./apps/worker/.nvim/state/nvim:", dev)
        # IDE snippet replaced the marker in the generated Dockerfile.
        self.assertNotIn("@DEVBOX_IDE_INJECTION_POINT@", read("apps/worker/Dockerfile"))

    def test_cli_nonempty_folder_continues_without_prompt(self):
        os.makedirs("apps/worker")
        with open("apps/worker/Dockerfile", "w") as f:
            f.write("EXISTING DOCKERFILE\n")

        args = {
            "--name": "worker",
            "--runtime": "python",
            "--port": "8000",
            "--path": "apps/worker",
        }
        with mock.patch(
            "builtins.input", side_effect=AssertionError("must not prompt")
        ):
            self.run_cmd(ms.cmd_add, args, self.tmp, MANIFEST)

        # Existing Dockerfile is never overwritten.
        self.assertEqual(read("apps/worker/Dockerfile"), "EXISTING DOCKERFILE\n")


class AddValidationTest(BaseTest):
    def test_rejects_parent_escape(self):
        self.expect_exit(
            ms.cmd_add,
            {"--name": "x", "--runtime": "python", "--port": "8000", "--path": "../x"},
            self.tmp,
            MANIFEST,
        )
        self.assertFalse(os.path.exists("docker-compose.yml"))

    def test_rejects_dot(self):
        self.expect_exit(
            ms.cmd_add,
            {"--name": "x", "--runtime": "python", "--port": "8000", "--path": "."},
            self.tmp,
            MANIFEST,
        )
        self.assertFalse(os.path.exists("docker-compose.yml"))

    def test_rejects_duplicate_name(self):
        cfg = dc.new_config(
            "scaffold",
            [dc.make_service("web", "src/web", port=5173, runtime="node", target="dev")],
        )
        dc.save_config(self.tmp, cfg)
        self.expect_exit(
            ms.cmd_add,
            {"--name": "web", "--runtime": "node", "--port": "5174", "--path": "src/web2"},
            self.tmp,
            MANIFEST,
        )

    def test_rejects_duplicate_source(self):
        cfg = dc.new_config(
            "scaffold",
            [dc.make_service("web", "apps/web", port=5173, runtime="node", target="dev")],
        )
        dc.save_config(self.tmp, cfg)
        self.expect_exit(
            ms.cmd_add,
            {"--name": "worker", "--runtime": "python", "--port": "8000", "--path": "apps/web"},
            self.tmp,
            MANIFEST,
        )

    def test_rejects_invalid_port(self):
        self.expect_exit(
            ms.cmd_add,
            {"--name": "x", "--runtime": "python", "--port": "abc", "--path": "apps/x"},
            self.tmp,
            MANIFEST,
        )

    def test_rejects_unknown_runtime(self):
        self.expect_exit(
            ms.cmd_add,
            {"--name": "x", "--runtime": "cobol", "--port": "8000", "--path": "apps/x"},
            self.tmp,
            MANIFEST,
        )

    def test_rejects_invalid_name(self):
        self.expect_exit(
            ms.cmd_add,
            {"--name": "!!!", "--runtime": "python", "--port": "8000", "--path": "apps/x"},
            self.tmp,
            MANIFEST,
        )


class AddInteractiveTest(BaseTest):
    def test_default_location_is_src_name(self):
        # name, runtime, port(default), location(default), inject(no)
        answers = ["worker", "python", "", "", ""]
        with mock.patch("builtins.input", side_effect=answers):
            self.run_cmd(ms.cmd_add, {}, self.tmp, MANIFEST)

        cfg = dc.load_config(self.tmp)
        svc = dc.get_service(cfg, "worker")
        self.assertEqual(svc["source"], "src/worker")
        self.assertTrue(svc["managed"])
        self.assertTrue(os.path.isfile("src/worker/Dockerfile"))
        self.assertIn("context: ./src/worker", read("docker-compose.yml"))

    def test_interactive_reraises_invalid_port(self):
        # name, runtime, invalid port, valid port, location, inject
        answers = ["worker", "python", "notaport", "9100", "", ""]
        with mock.patch("builtins.input", side_effect=answers):
            self.run_cmd(ms.cmd_add, {}, self.tmp, MANIFEST)

        cfg = dc.load_config(self.tmp)
        self.assertEqual(dc.get_service(cfg, "worker")["port"], 9100)


class RemoveManagedTest(BaseTest):
    def _seed(self):
        os.makedirs("src/web")
        with open("src/web/Dockerfile", "w") as f:
            f.write("FROM scratch\n")
        with open("src/web/main.py", "w") as f:
            f.write("print(1)\n")
        with open("docker-compose.yml", "w") as f:
            f.write("services:\n")
            f.write(ms.build_service_block_yaml("web", 8000, "src/web"))
        with open("docker-compose.dev.yml", "w") as f:
            f.write("services:\n")
            f.write(
                ms.build_service_dev_block_yaml(
                    "web", MANIFEST["runtimes"]["python"], False, "src/web"
                )
            )
        cfg = dc.new_config(
            "scaffold",
            [dc.make_service("web", "src/web", port=8000, runtime="python", target="dev")],
        )
        dc.save_config(self.tmp, cfg)

    def test_remove_managed_keeps_files_when_answered_no(self):
        self._seed()
        with mock.patch("builtins.input", side_effect=["n", "n"]):
            self.run_cmd(ms.cmd_remove, "web", self.tmp)

        self.assertTrue(os.path.exists("src/web/Dockerfile"))
        self.assertTrue(os.path.exists("src/web/main.py"))
        self.assertNotIn("web:", read("docker-compose.yml"))
        self.assertNotIn("web:", read("docker-compose.dev.yml"))
        self.assertEqual(dc.load_config(self.tmp)["services"], [])

    def test_remove_managed_deletes_when_answered_yes(self):
        self._seed()
        with mock.patch("builtins.input", side_effect=["y", "y"]):
            self.run_cmd(ms.cmd_remove, "web", self.tmp)

        self.assertFalse(os.path.exists("src/web"))
        self.assertEqual(dc.load_config(self.tmp)["services"], [])

    def test_remove_managed_eof_means_no(self):
        self._seed()
        with mock.patch("builtins.input", side_effect=EOFError):
            self.run_cmd(ms.cmd_remove, "web", self.tmp)

        self.assertTrue(os.path.exists("src/web/Dockerfile"))
        self.assertTrue(os.path.exists("src/web/main.py"))

    def test_remove_managed_uses_recorded_source_path(self):
        # Service lives outside src/ -- removal must use the devbox.json path.
        os.makedirs("apps/web")
        with open("apps/web/Dockerfile", "w") as f:
            f.write("D\n")
        with open("apps/web/app.py", "w") as f:
            f.write("x=1\n")
        with open("docker-compose.yml", "w") as f:
            f.write("services:\n")
            f.write(ms.build_service_block_yaml("web", 8000, "apps/web"))
        with open("docker-compose.dev.yml", "w") as f:
            f.write("services:\n")
            f.write(
                ms.build_service_dev_block_yaml(
                    "web", MANIFEST["runtimes"]["python"], False, "apps/web"
                )
            )
        cfg = dc.new_config(
            "scaffold",
            [dc.make_service("web", "apps/web", port=8000, runtime="python", target="dev")],
        )
        dc.save_config(self.tmp, cfg)

        with mock.patch("builtins.input", side_effect=["y", "y"]):
            self.run_cmd(ms.cmd_remove, "web", self.tmp)

        self.assertFalse(os.path.exists("apps/web"))
        self.assertFalse(os.path.exists("src/web"))


class RemoveUnmanagedTest(BaseTest):
    def test_remove_adopt_service_keeps_user_files(self):
        os.makedirs("apps/web")
        with open("apps/web/Dockerfile", "w") as f:
            f.write("USER DOCKERFILE\n")
        with open("apps/web/app.py", "w") as f:
            f.write("x = 1\n")
        user_compose = (
            "services:\n"
            "  web:\n"
            "    build:\n"
            "      context: ./apps/web\n"
            "    image: my/web:local\n"
        )
        with open("docker-compose.yml", "w") as f:
            f.write(user_compose)
        with open("docker-compose.dev.yml", "w") as f:
            f.write("services:\n")
            f.write(
                ms.build_service_dev_block_yaml(
                    "web", MANIFEST["runtimes"]["python"], False, "apps/web"
                )
            )
        cfg = dc.new_config(
            "adopt",
            [
                dc.make_service(
                    "web",
                    "apps/web",
                    workdir="/app",
                    context="apps/web",
                    dockerfile="Dockerfile",
                    target="dev",
                    managed=False,
                )
            ],
        )
        dc.save_config(self.tmp, cfg)

        before = read("docker-compose.yml")
        out = self.run_cmd(ms.cmd_remove, "web", self.tmp)

        # User-owned files byte-for-byte untouched.
        self.assertEqual(read("docker-compose.yml"), before)
        self.assertEqual(read("apps/web/Dockerfile"), "USER DOCKERFILE\n")
        self.assertEqual(read("apps/web/app.py"), "x = 1\n")
        # Dev override + devbox.json entry removed.
        self.assertNotIn("web:", read("docker-compose.dev.yml"))
        self.assertEqual(dc.load_config(self.tmp)["services"], [])
        # The user is told what was left in place.
        self.assertIn("user-owned", out)
        self.assertIn("docker-compose.yml", out)


class RemoveLegacyNoDevboxTest(BaseTest):
    def test_legacy_without_devbox_is_unmanaged(self):
        with open("docker-compose.yml", "w") as f:
            f.write("services:\n")
            f.write(ms.build_service_block_yaml("web", 8000, "src/web"))
        with open("docker-compose.dev.yml", "w") as f:
            f.write("services:\n")
            f.write(
                ms.build_service_dev_block_yaml(
                    "web", MANIFEST["runtimes"]["python"], False, "src/web"
                )
            )
        before = read("docker-compose.yml")

        out = self.run_cmd(ms.cmd_remove, "web", self.tmp)

        self.assertEqual(read("docker-compose.yml"), before)
        self.assertNotIn("web:", read("docker-compose.dev.yml"))
        self.assertIsNone(dc.load_config(self.tmp))
        self.assertIn("user-owned", out)


class ListTest(BaseTest):
    def test_list_shows_devbox_mapping(self):
        with open("docker-compose.yml", "w") as f:
            f.write("services:\n")
            f.write(ms.build_service_block_yaml("web", 8000, "apps/web"))
        with open("docker-compose.dev.yml", "w") as f:
            f.write("services:\n")
        cfg = dc.new_config(
            "scaffold",
            [
                dc.make_service(
                    "web", "apps/web", port=8000, runtime="python", target="dev"
                )
            ],
        )
        dc.save_config(self.tmp, cfg)

        out = self.run_cmd(ms.cmd_list, self.tmp)
        self.assertIn("docker-compose.yml", out)
        self.assertIn("apps/web", out)
        self.assertIn("/app", out)
        self.assertIn("managed", out)

    def test_list_marks_adopt_service_as_user(self):
        with open("docker-compose.dev.yml", "w") as f:
            f.write("services:\n")
        cfg = dc.new_config(
            "adopt",
            [dc.make_service("web", "apps/web", managed=False)],
        )
        dc.save_config(self.tmp, cfg)

        out = self.run_cmd(ms.cmd_list, self.tmp)
        self.assertIn("apps/web", out)
        self.assertIn("user", out)


if __name__ == "__main__":
    unittest.main()
