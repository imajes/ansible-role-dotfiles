"""Exercise preservation with real Git repositories, without a live account."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dotfiles_state", ROOT / "library/dotfiles_state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", "/dev/null")


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dotfiles-state-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "home"
        self.root = self.home / ".dotfiles"
        self.saved = self.home / ".local/state/dotfiles"
        initialize(self.root)
        self.files = [".config/zsh", ".gitconfig"]
        self.special = [{"src": ".config/zsh/.zshenv", "dest": ".zshenv"}]
        for name, text in {".config/zsh/.zshenv": "export ZDOTDIR=$HOME/.config/zsh\n",
                           ".config/zsh/.zshrc": "# canonical\n", ".gitconfig": "[init]\n defaultBranch=main\n",
                           "private-unmanaged": "original\n"}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.root, "add", ".")
        git(self.root, "commit", "--quiet", "-m", "fixture")

    def deployment(self, check=False):
        return state.Deployment(str(self.home), str(self.root), str(self.saved),
                                self.files, self.special, check=check)

    def test_real_directories_and_private_neighbors_survive(self):
        original = self.home / ".config/zsh/.zshrc"
        original.parent.mkdir(parents=True, mode=0o700)
        original.write_text("local config\n")
        markers = [".config/zsh/.zim/modules/plugin/runtime", ".config/zsh/.zshz-data",
                   ".config/gh/hosts.yml", ".cargo/credentials.toml", ".cargo/bin/custom",
                   ".gnupg/private-keys-v1.d/key", ".bash_history", ".zsh_history",
                   ".cache/nvim/cache", ".local/share/nvim/lazy/plugin/runtime"]
        for name in markers:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("retained:" + name)
            path.chmod(0o600)
        result = self.deployment().link()
        self.assertTrue(result["changed"])
        self.assertTrue(original.is_symlink())
        self.assertEqual(original.read_text(), "# canonical\n")
        self.assertEqual((Path(result["backup"]) / "replaced/.config/zsh/.zshrc").read_text(), "local config\n")
        self.assertEqual((Path(result["backup"]).stat().st_mode & 0o777), 0o700)
        for name in markers:
            self.assertEqual((self.home / name).read_text(), "retained:" + name)
            self.assertEqual((self.home / name).stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.deployment().link()["changed"])

    def test_existing_directory_links_and_checkout_runtime_survive(self):
        target = self.home / ".config/zsh"
        target.parent.mkdir()
        target.symlink_to(self.root / ".config/zsh", target_is_directory=True)
        runtime = target / ".zim/modules/example"
        runtime.mkdir(parents=True)
        (runtime / "keep").write_text("installed plugin")
        self.deployment().link()
        self.assertTrue(target.is_symlink())
        self.assertFalse((target / ".zshrc").is_symlink())
        self.assertEqual((runtime / "keep").read_text(), "installed plugin")
        self.assertFalse(self.deployment().link()["changed"])

    def test_managed_checkout_edits_are_backed_up_before_restore(self):
        changed = self.root / ".config/zsh/.zshrc"
        changed.write_text("ad hoc fix\n")
        result = self.deployment().prepare()
        self.assertEqual(changed.read_text(), "# canonical\n")
        self.assertEqual((Path(result["backup"]) / "checkout/.config/zsh/.zshrc").read_text(), "ad hoc fix\n")
        self.assertFalse(self.deployment().prepare()["changed"])

    def test_unmanaged_or_staged_edits_abort_before_managed_restore(self):
        managed = self.root / ".config/zsh/.zshrc"
        managed.write_text("keep until all checks pass\n")
        unmanaged = self.root / "private-unmanaged"
        unmanaged.write_text("private change\n")
        with self.assertRaisesRegex(ValueError, "Unmanaged tracked"):
            self.deployment().prepare()
        self.assertEqual(managed.read_text(), "keep until all checks pass\n")
        git(self.root, "restore", "private-unmanaged")
        git(self.root, "add", ".config/zsh/.zshrc")
        with self.assertRaisesRegex(ValueError, "staged changes"):
            self.deployment().prepare()
        self.assertFalse(self.saved.exists())

    def test_submodule_edits_are_preserved_without_changing_the_pin(self):
        upstream = self.home / "plugin-source"
        initialize(upstream)
        (upstream / "init.lua").write_text("return {}\n")
        git(upstream, "add", ".")
        git(upstream, "commit", "--quiet", "-m", "child")
        git(self.root, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(upstream), ".config/nvim")
        git(self.root, "commit", "--quiet", "-m", "submodule")
        self.files.append(".config/nvim")
        child = self.root / ".config/nvim"
        old = git(child, "rev-parse", "HEAD")
        (child / "init.lua").write_text("local edit\n")
        result = self.deployment().prepare()
        self.assertEqual((Path(result["backup"]) / "checkout/.config/nvim/init.lua").read_text(), "local edit\n")
        self.assertEqual(git(child, "rev-parse", "HEAD"), old)
        self.deployment().link()
        self.assertEqual((self.home / ".config/nvim/init.lua").read_text(), "return {}\n")

    def test_check_mode_does_not_write_or_restore(self):
        managed = self.root / ".config/zsh/.zshrc"
        managed.write_text("local edit\n")
        self.assertTrue(self.deployment(check=True).prepare()["changed"])
        self.assertTrue(self.deployment(check=True).link()["changed"])
        self.assertEqual(managed.read_text(), "local edit\n")
        self.assertFalse(self.saved.exists())
        self.assertFalse((self.home / ".zshenv").exists())

    def test_private_selection_and_escaping_parents_are_rejected(self):
        for name in (".cargo", ".config", ".gnupg", "../outside", "/absolute", ".config/gh/hosts.yml"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                state.Deployment(str(self.home), str(self.root), str(self.saved), [name])
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.home / ".config").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "leaves the selected"):
            self.deployment().link()
        self.assertFalse((self.home / ".gitconfig").exists())

    def test_removed_owned_links_are_retired_to_backup(self):
        self.deployment().link()
        self.files.remove(".gitconfig")
        result = self.deployment().link()
        self.assertFalse(os.path.lexists(self.home / ".gitconfig"))
        self.assertTrue((Path(result["backup"]) / "retired/.gitconfig").is_symlink())
        self.assertFalse(self.deployment().link()["changed"])

    def test_old_link_target_and_unselected_real_files_are_not_modified(self):
        original = self.home / "private-git-config"
        original.write_text("retained private include\n")
        (self.home / ".gitconfig").symlink_to(original)
        result = self.deployment().link()
        self.assertEqual(original.read_text(), "retained private include\n")
        self.assertEqual((Path(result["backup"]) / "replaced/.gitconfig").readlink(), original)

    def test_backup_directory_cannot_escape_private_state(self):
        self.saved.mkdir(parents=True)
        outside = self.home / "outside-backup"
        outside.mkdir()
        (self.saved / "backups").symlink_to(outside, target_is_directory=True)
        managed = self.root / ".config/zsh/.zshrc"
        managed.write_text("retained edit\n")
        with self.assertRaisesRegex(ValueError, "Backup directory leaves"):
            self.deployment().prepare()
        self.assertEqual(managed.read_text(), "retained edit\n")
        self.assertEqual(list(outside.iterdir()), [])

    def test_edited_parent_link_cannot_write_outside_checkout(self):
        actual = self.root / ".config/zsh"
        outside = self.home / "other-zsh"
        actual.rename(outside)
        actual.symlink_to(outside, target_is_directory=True)
        (outside / ".zshrc").write_text("retain external edit\n")
        with self.assertRaisesRegex(ValueError, "parent leaves"):
            self.deployment().prepare()
        self.assertEqual((outside / ".zshrc").read_text(), "retain external edit\n")


if __name__ == "__main__":
    unittest.main()
