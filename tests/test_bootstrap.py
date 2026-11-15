"""Offline tests for the distributable installer; no model or network calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("bootstrap", Path(__file__).parents[1] / "scripts/bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_settings_use_bundled_runtime_and_preserve_existing_file_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config"
            config.mkdir()
            template = {"runtime": {}, "rewriter": {"local": {}}, "media": {}}
            (config / "skill-runtime.example.json").write_text(json.dumps(template))
            (config / "api-keys.example.json").write_text('{"X2V_API_KEY":"","QWEN_API_KEY":""}')
            with patch.object(bootstrap, "ROOT", root):
                settings = bootstrap.write_local_settings(root / "external-models")
                data = json.loads(settings.read_text(encoding="utf-8"))
                keys = Path(data["credentials_file"])
                self.assertEqual(json.loads(keys.read_text())["X2V_API_KEY"], "")
                self.assertEqual(Path(data["rewriter"]["local"]["script"]), root / "runtime/prompt-rewriter/rewrite_local.py")
                keys.write_text('{"X2V_API_KEY":"test-only-preserved"}')
                bootstrap.write_local_settings(root / "external-models")
                self.assertEqual(json.loads(keys.read_text())["X2V_API_KEY"], "test-only-preserved")

    def test_dry_run_has_no_install_or_file_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "runtime-manifest.json").write_text(json.dumps({"ffmpeg": {"url": "https://example.test/ffmpeg.zip"}}))
            before = sorted(str(path) for path in root.rglob("*"))
            output = io.StringIO()
            with patch.object(bootstrap, "ROOT", root), patch.object(bootstrap.subprocess, "run", side_effect=AssertionError("Unexpected process")), patch.object(bootstrap.urllib.request, "urlopen", side_effect=AssertionError("Unexpected download")), contextlib.redirect_stdout(output):
                self.assertEqual(bootstrap.main(["--dry-run"]), 0)
            self.assertEqual(before, sorted(str(path) for path in root.rglob("*")))
            plan = json.loads(output.getvalue())
            self.assertEqual(set(plan["components"]), {"video-harness", "prompt-rewriter"})
            self.assertFalse(plan["loads_models"])

    def test_archive_hash_and_selected_member_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("build/bin/ffmpeg.exe", b"synthetic ffmpeg")
                output.writestr("build/bin/ffprobe.exe", b"synthetic ffprobe")
                output.writestr("build/LICENSE", b"license fixture")
                output.writestr("build/README.txt", b"readme fixture")
                output.writestr("../escaped.txt", b"unselected")
            with patch.object(bootstrap, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    bootstrap.install_ffmpeg(archive, {"sha256": "wrong"})
                self.assertFalse((root / "runtime").exists())
                tools = bootstrap.install_ffmpeg(archive, {"sha256": bootstrap.digest(archive)})
            self.assertEqual({path.name for path in tools.iterdir()}, {"ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"})
            self.assertEqual((tools / "ffmpeg.exe").read_bytes(), b"synthetic ffmpeg")
            self.assertFalse((root.parent / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()
