import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import devbox_config as dc  # noqa: E402


class SanitizeServiceNameTest(unittest.TestCase):
    def test_lowercases_and_replaces_invalid_chars(self):
        self.assertEqual(dc.sanitize_service_name("Web"), "web")
        self.assertEqual(dc.sanitize_service_name("my app!"), "my-app-")
        self.assertEqual(dc.sanitize_service_name("my.app"), "my-app")
        self.assertEqual(dc.sanitize_service_name("  api  "), "api")
        self.assertEqual(dc.sanitize_service_name("my_app"), "my_app")

    def test_rejects_invalid(self):
        for bad in ["!!!", "-web", "_web", "", "   ", 123, None]:
            with self.assertRaises(dc.ConfigError, msg=repr(bad)):
                dc.sanitize_service_name(bad)


class NormalizeRelPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_root_dot(self):
        self.assertEqual(dc.normalize_rel_path(".", self.tmp), ".")
        self.assertEqual(dc.normalize_rel_path(self.tmp, self.tmp), ".")

    def test_relative_with_dot_slash_and_trailing_slash(self):
        self.assertEqual(dc.normalize_rel_path("./apps/web/", self.tmp), "apps/web")
        self.assertEqual(dc.normalize_rel_path("apps/web", self.tmp), "apps/web")

    def test_absolute_inside_root(self):
        absolute = os.path.join(self.tmp, "apps", "web")
        self.assertEqual(dc.normalize_rel_path(absolute, self.tmp), "apps/web")

    def test_escape_rejected(self):
        with self.assertRaises(dc.ConfigError):
            dc.normalize_rel_path("../x", self.tmp)
        with self.assertRaises(dc.ConfigError):
            dc.normalize_rel_path(os.path.join(self.tmp, "..", "x"), self.tmp)
        outside = os.path.abspath(os.path.join(self.tmp, "..", "elsewhere"))
        with self.assertRaises(dc.ConfigError):
            dc.normalize_rel_path(outside, self.tmp)


class MakeServiceTest(unittest.TestCase):
    def test_defaults(self):
        svc = dc.make_service("web", "src/web")
        self.assertEqual(
            svc,
            {
                "name": "web",
                "source": "src/web",
                "workdir": "/app",
                "context": "src/web",
                "dockerfile": "Dockerfile",
                "target": None,
                "port": None,
                "runtime": None,
                "managed": True,
            },
        )

    def test_image_only(self):
        svc = dc.make_service("db", None)
        self.assertIsNone(svc["source"])
        self.assertIsNone(svc["workdir"])
        self.assertIsNone(svc["context"])
        self.assertIsNone(svc["dockerfile"])
        self.assertTrue(svc["managed"])

    def test_explicit_values_and_context_override(self):
        svc = dc.make_service(
            "web",
            "apps/web",
            workdir="/srv",
            context=".",
            dockerfile="app.Dockerfile",
            target="prod",
            port=3000,
            runtime="node",
            managed=False,
        )
        self.assertEqual(svc["context"], ".")
        self.assertEqual(svc["workdir"], "/srv")
        self.assertEqual(svc["dockerfile"], "app.Dockerfile")
        self.assertEqual(svc["target"], "prod")
        self.assertEqual(svc["port"], 3000)
        self.assertEqual(svc["runtime"], "node")
        self.assertFalse(svc["managed"])


class NormalizeConfigTest(unittest.TestCase):
    def test_v1_adopt_exact(self):
        raw = {
            "version": 1,
            "service": "web",
            "dockerfile": "Dockerfile",
            "dockerfile_context": "apps/web",
            "port": 3000,
            "stages": {"dev": "dev", "prod": "prod"},
            "inject_ide": True,
            "has_firebase": False,
            "preserve_env": ["FOO"],
            "custom_key": {"a": 1},
            "compose_files": ["docker-compose.yml"],
            "mcp": {"custom": True},
        }
        expected = {
            "version": 2,
            "mode": "adopt",
            "services": [
                {
                    "name": "web",
                    "source": ".",
                    "workdir": "/app",
                    "context": "apps/web",
                    "dockerfile": "Dockerfile",
                    "target": "dev",
                    "port": 3000,
                    "runtime": None,
                    "managed": False,
                }
            ],
            "inject_ide": True,
            "has_firebase": False,
            "preserve_env": ["FOO"],
            "custom_key": {"a": 1},
            "compose_files": ["docker-compose.yml"],
            "mcp": {"custom": True},
        }
        self.assertEqual(dc.normalize_config(raw), expected)

    def test_v1_adopt_without_dev_stage(self):
        raw = {"service": "api", "stages": {"prod": "prod"}}
        cfg = dc.normalize_config(raw)
        svc = cfg["services"][0]
        self.assertEqual(svc["target"], None)
        self.assertEqual(svc["context"], ".")
        self.assertEqual(svc["managed"], False)
        self.assertEqual(cfg["mode"], "adopt")
        self.assertEqual(cfg["compose_files"], dc.DEFAULT_COMPOSE_FILES)
        self.assertEqual(cfg["mcp"], dc.DEFAULT_MCP_CONFIG)

    def test_v1_scaffold_exact(self):
        raw = {
            "version": 1,
            "services": [
                {"name": "web", "runtime": "node", "port": 5173},
                {"name": "api", "runtime": "python", "port": 8000},
            ],
            "inject_ide": False,
        }
        expected = {
            "version": 2,
            "mode": "scaffold",
            "services": [
                {
                    "name": "web",
                    "source": "src/web",
                    "workdir": "/app",
                    "context": "src/web",
                    "dockerfile": "Dockerfile",
                    "target": "dev",
                    "port": 5173,
                    "runtime": "node",
                    "managed": True,
                },
                {
                    "name": "api",
                    "source": "src/api",
                    "workdir": "/app",
                    "context": "src/api",
                    "dockerfile": "Dockerfile",
                    "target": "dev",
                    "port": 8000,
                    "runtime": "python",
                    "managed": True,
                },
            ],
            "inject_ide": False,
            "compose_files": dc.DEFAULT_COMPOSE_FILES,
            "mcp": dc.DEFAULT_MCP_CONFIG,
        }
        self.assertEqual(dc.normalize_config(raw), expected)

    def test_v1_scaffold_missing_fields(self):
        cfg = dc.normalize_config({"version": 1, "services": [{"name": "web"}]})
        svc = cfg["services"][0]
        self.assertIsNone(svc["runtime"])
        self.assertIsNone(svc["port"])

    def test_v2_keeps_unknown_service_keys(self):
        raw = {
            "version": 2,
            "mode": "adopt",
            "services": [
                {
                    "name": "web",
                    "source": "apps/web",
                    "managed": False,
                    "custom": "yes",
                }
            ],
            "unknown_top": 5,
        }
        cfg = dc.normalize_config(raw)
        svc = cfg["services"][0]
        self.assertEqual(svc["custom"], "yes")
        self.assertEqual(svc["workdir"], "/app")
        self.assertEqual(svc["context"], "apps/web")
        self.assertEqual(cfg["unknown_top"], 5)

    def test_v2_managed_defaults_per_mode(self):
        adopt = dc.normalize_config(
            {
                "version": 2,
                "mode": "adopt",
                "services": [{"name": "web", "source": "apps/web"}],
            }
        )
        self.assertFalse(adopt["services"][0]["managed"])
        scaffold_cfg = dc.normalize_config(
            {
                "version": 2,
                "mode": "scaffold",
                "services": [{"name": "web", "source": "src/web"}],
            }
        )
        self.assertTrue(scaffold_cfg["services"][0]["managed"])

    def test_invalid_shapes_rejected(self):
        for bad in [
            "nope",
            [],
            {"version": 2, "services": "nope"},
            {"version": 2, "services": [123]},
            {"version": 2, "services": [{"name": "!!!"}]},
            {"version": 2, "services": [{}]},
            {"version": 2, "mode": "weird", "services": []},
            {"version": 1, "services": [{"name": "!!!"}]},
            {"service": "!!!"},
        ]:
            with self.assertRaises(dc.ConfigError, msg=repr(bad)):
                dc.normalize_config(bad)

    def test_new_config_extra_merged_and_normalized(self):
        cfg = dc.new_config(
            "adopt",
            [dc.make_service("web", ".", workdir="/app", managed=False)],
            extra={"preserve_env": ["X"]},
        )
        self.assertEqual(cfg["version"], 2)
        self.assertEqual(cfg["mode"], "adopt")
        self.assertEqual(cfg["preserve_env"], ["X"])
        self.assertEqual(cfg["services"][0]["source"], ".")


class LoadSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_none(self):
        self.assertIsNone(dc.load_config(self.tmp))

    def test_roundtrip_and_atomic(self):
        cfg = dc.new_config(
            "scaffold",
            [dc.make_service("web", "src/web", port=5173, runtime="node")],
        )
        path = dc.save_config(self.tmp, cfg)
        self.assertEqual(path, os.path.join(self.tmp, "devbox.json"))
        with open(path) as f:
            raw = f.read()
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(json.loads(raw), cfg)
        # Atomic: no temp files left behind.
        self.assertEqual(os.listdir(self.tmp), ["devbox.json"])
        self.assertEqual(dc.load_config(self.tmp), cfg)

    def test_invalid_json_raises(self):
        with open(os.path.join(self.tmp, "devbox.json"), "w") as f:
            f.write("{not json")
        with self.assertRaises(dc.ConfigError):
            dc.load_config(self.tmp)

    def test_load_normalizes_paths(self):
        raw = {
            "version": 2,
            "mode": "adopt",
            "services": [
                {
                    "name": "web",
                    "source": os.path.join(self.tmp, "apps", "web"),
                    "workdir": "/app",
                    "context": ".",
                    "dockerfile": "Dockerfile",
                }
            ],
        }
        with open(os.path.join(self.tmp, "devbox.json"), "w") as f:
            json.dump(raw, f)
        cfg = dc.load_config(self.tmp)
        self.assertEqual(cfg["services"][0]["source"], "apps/web")
        self.assertEqual(cfg["services"][0]["context"], ".")


class ServiceHelpersTest(unittest.TestCase):
    def test_upsert_get_remove(self):
        cfg = dc.new_config("scaffold", [])
        dc.upsert_service(cfg, dc.make_service("web", "src/web"))
        dc.upsert_service(cfg, dc.make_service("api", "src/api"))
        self.assertEqual(len(cfg["services"]), 2)
        self.assertEqual(dc.get_service(cfg, "api")["source"], "src/api")
        self.assertIsNone(dc.get_service(cfg, "nope"))

        dc.upsert_service(cfg, dc.make_service("web", "src/web2"))
        self.assertEqual(len(cfg["services"]), 2)
        self.assertEqual(dc.get_service(cfg, "web")["source"], "src/web2")

        self.assertTrue(dc.remove_service(cfg, "web"))
        self.assertFalse(dc.remove_service(cfg, "web"))
        self.assertIsNone(dc.get_service(cfg, "web"))

    def test_find_compose_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(dc.find_compose_file(d))
            with open(os.path.join(d, "compose.yml"), "w") as f:
                f.write("services: {}\n")
            self.assertEqual(
                dc.find_compose_file(d), os.path.join(d, "compose.yml")
            )


class UpsertMarkedBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create(self):
        path = os.path.join(self.tmp, "new.md")
        status = dc.upsert_marked_block(path, "hello", dc.MD_BEGIN, dc.MD_END)
        self.assertEqual(status, "created")
        with open(path) as f:
            self.assertEqual(
                f.read(), dc.MD_BEGIN + "\nhello\n" + dc.MD_END + "\n"
            )

    def test_append_preserves_original_bytes(self):
        path = os.path.join(self.tmp, "notes.md")
        original = "user line 1\nuser line 2"
        with open(path, "w") as f:
            f.write(original)
        status = dc.upsert_marked_block(path, "BODY", dc.MD_BEGIN, dc.MD_END)
        self.assertEqual(status, "created")
        with open(path) as f:
            content = f.read()
        self.assertTrue(content.startswith(original))
        self.assertIn("user line 2\n\n" + dc.MD_BEGIN, content)
        self.assertIn("BODY", content)
        self.assertIn(dc.MD_END, content)

    def test_update_in_place_preserves_surrounding_text(self):
        path = os.path.join(self.tmp, "arch.md")
        content = "BEFORE\n" + dc.MD_BEGIN + "\nold body\n" + dc.MD_END + "\nAFTER\n"
        with open(path, "w") as f:
            f.write(content)
        status = dc.upsert_marked_block(path, "new body", dc.MD_BEGIN, dc.MD_END)
        self.assertEqual(status, "updated")
        with open(path) as f:
            updated = f.read()
        self.assertTrue(updated.startswith("BEFORE\n"))
        self.assertTrue(updated.endswith("AFTER\n"))
        self.assertIn("new body", updated)
        self.assertNotIn("old body", updated)

        self.assertEqual(
            dc.upsert_marked_block(path, "new body", dc.MD_BEGIN, dc.MD_END),
            "unchanged",
        )

    def test_create_false_skips_missing_file(self):
        path = os.path.join(self.tmp, "missing.md")
        self.assertEqual(
            dc.upsert_marked_block(
                path, "x", dc.MD_BEGIN, dc.MD_END, create=False
            ),
            "skipped",
        )
        self.assertFalse(os.path.exists(path))


class ArchitectureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_target_absent(self):
        self.assertEqual(
            os.path.basename(dc.architecture_target(self.tmp)), "ARCHITECTURE.md"
        )

    def test_target_user_file_without_markers(self):
        with open(os.path.join(self.tmp, "ARCHITECTURE.md"), "w") as f:
            f.write("# My own doc\n")
        self.assertEqual(
            os.path.basename(dc.architecture_target(self.tmp)), "DEVBOX.md"
        )

    def test_target_with_markers(self):
        with open(os.path.join(self.tmp, "ARCHITECTURE.md"), "w") as f:
            f.write("# Doc\n" + dc.MD_BEGIN + "\nbody\n" + dc.MD_END + "\n")
        self.assertEqual(
            os.path.basename(dc.architecture_target(self.tmp)), "ARCHITECTURE.md"
        )

    def test_render_scaffold_mentions_mapping(self):
        cfg = dc.new_config(
            "scaffold",
            [
                dc.make_service(
                    "web", "src/web", port=5173, runtime="node", target="dev"
                ),
                dc.make_service(
                    "api", "src/api", port=8000, runtime="python", target="dev"
                ),
            ],
        )
        body = dc.render_architecture(cfg)
        for token in ["src/web", "src/api", "/app", "5173", "8000", "web", "api"]:
            self.assertIn(token, body)
        for token in [
            "source build/envsetup.sh",
            "devbox-ai-setup",
            "devbox-up",
            "devbox-run web",
            "devbox-run api",
        ]:
            self.assertIn(token, body)

    def test_render_adopt_does_not_hardcode_src(self):
        cfg = dc.new_config(
            "adopt",
            [
                dc.make_service(
                    "web",
                    ".",
                    workdir="/app",
                    context=".",
                    dockerfile="Dockerfile",
                    target="dev",
                    port=3000,
                    managed=False,
                )
            ],
        )
        body = dc.render_architecture(cfg)
        self.assertNotIn("src/", body)
        self.assertIn("/app", body)
        self.assertIn("user", body)

    def test_write_architecture_is_idempotent_and_writes_target(self):
        cfg = dc.new_config(
            "scaffold", [dc.make_service("web", "src/web", port=5173)]
        )
        path = dc.write_architecture(self.tmp, cfg)
        self.assertEqual(path, os.path.join(self.tmp, "ARCHITECTURE.md"))
        with open(path) as f:
            first = f.read()
        self.assertIn(dc.MD_BEGIN, first)
        self.assertIn("/app", first)
        self.assertEqual(dc.write_architecture(self.tmp, cfg), path)
        with open(path) as f:
            self.assertEqual(f.read(), first)

    def test_write_architecture_respects_user_file(self):
        with open(os.path.join(self.tmp, "ARCHITECTURE.md"), "w") as f:
            f.write("# Mine\n")
        cfg = dc.new_config(
            "scaffold", [dc.make_service("web", "src/web", port=5173)]
        )
        path = dc.write_architecture(self.tmp, cfg)
        self.assertEqual(path, os.path.join(self.tmp, "DEVBOX.md"))
        with open(path) as f:
            self.assertIn("src/web", f.read())
        with open(os.path.join(self.tmp, "ARCHITECTURE.md")) as f:
            self.assertEqual(f.read(), "# Mine\n")


class WriteFileSafelyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_created_skipped_overwritten_unchanged(self):
        path = os.path.join(self.tmp, "f.txt")
        self.assertEqual(dc.write_file_safely(path, "hello"), "created")
        with open(path) as f:
            self.assertEqual(f.read(), "hello")

        self.assertEqual(dc.write_file_safely(path, "other"), "skipped")
        with open(path) as f:
            self.assertEqual(f.read(), "hello")
        self.assertFalse(os.path.exists(path + ".bak"))

        self.assertEqual(
            dc.write_file_safely(path, "other", overwrite=True), "overwritten"
        )
        with open(path) as f:
            self.assertEqual(f.read(), "other")
        with open(path + ".bak") as f:
            self.assertEqual(f.read(), "hello")

        self.assertEqual(
            dc.write_file_safely(path, "other", overwrite=True), "unchanged"
        )

    def test_backup_false_does_not_create_bak(self):
        path = os.path.join(self.tmp, "f.txt")
        dc.write_file_safely(path, "one")
        self.assertEqual(
            dc.write_file_safely(path, "two", overwrite=True, backup=False),
            "overwritten",
        )
        self.assertFalse(os.path.exists(path + ".bak"))


if __name__ == "__main__":
    unittest.main()
