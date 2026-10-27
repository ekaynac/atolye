"""The referee refuses patches that could fool the hidden tests, before running anything."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from atolye.referee import check_patch

ALLOWED = ["src/pkg/*", "tests/*", "CHANGES.md"]
HIDDEN = ["acceptance_hidden"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class CheckPatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        for rel, text in {"src/pkg/mod.py": "x = 1\n", "tests/test_mod.py": "def test(): pass\n",
                          "pyproject.toml": "[project]\nname = 'p'\n", "README.md": "r\n"}.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def reasons(self) -> list[str]:
        git(self.repo, "add", "-A")
        patch = Path(self.tmp.name) / ".." / f"{Path(self.tmp.name).name}.patch"
        patch.write_text(git(self.repo, "diff", "--cached", "--binary", "HEAD"))
        try:
            return check_patch(patch.resolve(), ALLOWED, HIDDEN)
        finally:
            patch.unlink()

    def test_an_ordinary_change_inside_the_allowed_paths_passes(self):
        (self.repo / "src/pkg/mod.py").write_text("x = 2\n")
        (self.repo / "tests/test_new.py").write_text("def test_new(): pass\n")
        self.assertEqual(self.reasons(), [])

    def test_test_runner_config_is_refused(self):  # e.g. addopts = "--co" makes pytest only collect
        (self.repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '--co'\n")
        self.assertTrue(any("pyproject.toml" in r for r in self.reasons()))

    def test_module_shadows_and_startup_hooks_are_refused_even_inside_allowed_paths(self):
        for rel in ("src/pkg/pytest.py", "src/pkg/sitecustomize.py", "tests/conftest.py", "src/pkg/x.pth"):
            with self.subTest(rel=rel):
                (self.repo / rel).write_text("raise SystemExit(0)\n")
                self.assertTrue(any(rel in r for r in self.reasons()), rel)
                (self.repo / rel).unlink()

    def test_paths_outside_the_allowlist_are_refused(self):
        (self.repo / "README.md").write_text("changed\n")
        self.assertTrue(any("outside the allowed paths: README.md" in r for r in self.reasons()))

    def test_a_rename_counts_both_names(self):
        git(self.repo, "mv", "pyproject.toml", "src/pkg/pyproject_moved.toml")
        self.assertTrue(any("pyproject.toml" in r for r in self.reasons()))

    def test_symlinks_are_refused(self):
        (self.repo / "src/pkg/link").symlink_to("../../pyproject.toml")
        self.assertIn("adds a symlink or a submodule", self.reasons())

    def test_writing_into_the_acceptance_path_is_refused(self):
        (self.repo / "acceptance_hidden").mkdir()
        (self.repo / "acceptance_hidden/test_x.py").write_text("def test(): pass\n")
        self.assertTrue(any("acceptance path" in r for r in self.reasons()))


if __name__ == "__main__":
    unittest.main()
