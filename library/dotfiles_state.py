#!/usr/bin/python
"""Back up managed config and reconcile file links without purging account state."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile


DOCUMENTATION = r"""
---
module: dotfiles_state
short_description: Preserve account state while deploying tracked configuration
description:
  - Back up edited managed source files before a Git update, or reconcile tracked
    configuration links after it. Does not delete account directories or runtime state.
options:
  action:
    type: str
    choices: [prepare, link]
    required: true
  home:
    type: path
    required: true
  checkout:
    type: path
    required: true
  state:
    type: path
    required: true
  files:
    type: list
    elements: str
    required: true
  special_links:
    type: list
    elements: dict
    default: []
author: Homelab maintainers
"""

EXAMPLES = r"""
- name: Reconcile reviewed configuration
  dotfiles_state:
    action: link
    home: /home/james
    checkout: /home/james/.dotfiles
    state: /home/james/.local/state/dotfiles
    files: [.config/zsh, .gitignore_global]
    special_links:
      - {src: .config/zsh/.zshenv, dest: .zshenv}
"""

RETURN = r"""
changed:
  description: Whether managed configuration or its deployment record changed.
  type: bool
  returned: always
backup:
  description: Private directory containing replaced configuration, when needed.
  type: str
  returned: when a backup was made
"""

# These are never configuration deployment targets, even via a broad selection.
PRIVATE = (
    ".gnupg", ".ssh", ".cargo", ".rustup", ".npm", ".npmrc", ".netrc",
    ".bash_history", ".zsh_history", ".local", ".cache", ".agents", ".codex",
    ".claude", ".config/1password", ".config/op", ".config/gh/hosts.yml",
    ".config/wandb",
)


def relative(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError("Expected a normalized relative configuration path: " + name)
    if any(part == ".git" for part in path.parts):
        raise ValueError("Git metadata cannot be deployed")
    return name


def selected(name, roots):
    return any(name == root or name.startswith(root + "/") for root in roots)


def public_path(name):
    relative(name)
    if selected(name, PRIVATE):
        raise ValueError("Runtime or private path is not a deployment target: " + name)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        # Never include raw diff contents or credential-bearing Git diagnostics.
        raise ValueError("Git operation failed in " + str(root) + ": " + args[0])
    return result.stdout


def entries(root, recursive=False):
    flags = ["--recurse-submodules"] if recursive else []
    records = {}
    for item in git(root, "ls-files", "--stage", "-z", *flags).split(b"\0"):
        if not item:
            continue
        metadata, name = item.decode().split("\t", 1)
        mode, revision, stage = metadata.split()
        if stage != "0":
            raise ValueError("Resolve the checkout's Git conflicts before deployment")
        records[relative(name)] = (mode, revision)
    return records


def repositories(root, boundary=None):
    boundary = boundary or root.resolve()
    if not root.resolve().is_relative_to(boundary):
        raise ValueError("Submodule worktree leaves the source checkout")
    yield root, ""
    for name, (mode, revision) in entries(root).items():
        if mode != "160000":
            continue
        child = root / name
        if not (child / ".git").exists():
            continue
        if git(child, "rev-parse", "HEAD").decode().strip() != revision:
            raise ValueError("Submodule has a locally selected revision: " + name)
        for repo, prefix in repositories(child, boundary):
            yield repo, name + ("/" + prefix if prefix else "")


class Deployment:
    def __init__(self, home, checkout, state, files, special_links=(), check=False):
        self.home, self.checkout, self.state = map(Path, (home, checkout, state))
        if not all(p.is_absolute() for p in (self.home, self.checkout, self.state)):
            raise ValueError("Account home, checkout and state paths must be absolute")
        self.home = self.home.resolve()
        for path in (self.checkout, self.state):
            if not path.resolve().is_relative_to(self.home) or path.resolve() == self.home:
                raise ValueError("Checkout and state must remain inside the selected account home")
        if self.state.resolve().is_relative_to(self.checkout.resolve()):
            raise ValueError("Private deployment state must be outside the source checkout")
        self.files = [relative(name) for name in files]
        self.special = list(special_links)
        for name in self.files:
            public_path(name)
            if any(secret.startswith(name + "/") for secret in PRIVATE):
                raise ValueError("Select individual public files within this mixed directory: " + name)
        for item in self.special:
            public_path(item["src"])
            public_path(item["dest"])
        self.roots = self.files + [item["src"] for item in self.special]
        self.check = check
        self.backup = None

    def backup_path(self, name):
        if self.backup is None:
            directory = self.state / "backups"
            if not directory.resolve().is_relative_to(self.state.resolve()):
                raise ValueError("Backup directory leaves the private deployment state")
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.backup = Path(tempfile.mkdtemp(prefix="config-", dir=directory))
        target = self.backup / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        return target

    def prepare(self):
        if not self.checkout.exists():
            return {"changed": False}
        if git(self.checkout, "rev-parse", "--show-toplevel").decode().strip() != str(self.checkout.resolve()):
            raise ValueError("Existing checkout path is not its own Git repository")
        changes = []
        # Plan every repository before changing any of them. Staged/unmanaged
        # changes and locally selected submodule commits require human review.
        for repo, prefix in repositories(self.checkout):
            if git(repo, "diff", "--cached", "--name-only", "-z"):
                raise ValueError("Checkout contains staged changes; preserve/review them before deployment")
            tracked = entries(repo)
            names = git(repo, "diff", "--name-only", "-z").decode().strip("\0").split("\0")
            for name in filter(None, names):
                if tracked[name][0] == "160000":
                    continue
                full = prefix + "/" + name if prefix else name
                if not selected(full, self.roots):
                    raise ValueError("Unmanaged tracked edits require review before checkout update: " + full)
                public_path(full)
                if not (repo / name).parent.resolve().is_relative_to(self.checkout.resolve()):
                    raise ValueError("Edited configuration parent leaves the source checkout: " + full)
                changes.append((repo, name, full))
        if not self.check:
            for repo, name, full in changes:
                source = repo / name
                if source.is_file() or source.is_symlink():
                    shutil.copy2(source, self.backup_path("checkout/" + full), follow_symlinks=False)
                elif source.exists():
                    raise ValueError("Managed tracked path became a directory: " + full)
            for repo, name, _ in changes:
                git(repo, "restore", "--worktree", "--", name)
        return self.result(bool(changes), edited_files=len(changes))

    def link(self):
        if not self.checkout.exists() and self.check:
            return {"changed": True, "msg": "Checkout must be fetched before links can be previewed"}
        tracked = entries(self.checkout, recursive=True)
        pairs = {name: name for name, (mode, _) in tracked.items()
                 if mode != "160000" and selected(name, self.files)}
        for root in self.files:
            if not any(selected(name, [root]) for name in pairs):
                raise ValueError("Selected configuration is absent from the checkout: " + root)
        for item in self.special:
            if item["src"] not in tracked or tracked[item["src"]][0] == "160000":
                raise ValueError("Special-link source must be a tracked file: " + item["src"])
            if item["dest"] in pairs and pairs[item["dest"]] != item["src"]:
                raise ValueError("Conflicting destination: " + item["dest"])
            pairs[item["dest"]] = item["src"]
        changes = []
        for name, source_name in sorted(pairs.items()):
            public_path(name)
            source, target = self.checkout / source_name, self.home / name
            if not source.is_file() or not source.resolve().is_relative_to(self.checkout.resolve()):
                raise ValueError("Configuration source is missing or leaves the checkout: " + source_name)
            if not target.parent.resolve().is_relative_to(self.home):
                raise ValueError("Destination parent leaves the selected account home: " + name)
            # Keep existing directory links to this checkout. Its tracked files
            # already occupy these paths, and its untracked runtime state stays put.
            if target.resolve() == source.resolve():
                continue
            if target.is_dir() and not target.is_symlink():
                raise ValueError("Refusing to replace a directory with a file link: " + name)
            changes.append((name, source, target))
        record = self.state / "links.json"
        previous = json.loads(record.read_text()) if record.exists() else {}
        stale = []
        for name, source_name in previous.get("links", {}).items():
            public_path(name)
            public_path(source_name)
            if name not in pairs:
                target = self.home / name
                if not target.parent.resolve().is_relative_to(self.home):
                    raise ValueError("Previous destination leaves the account home: " + name)
                if target.is_symlink() and target.resolve() == (self.checkout / source_name).resolve():
                    stale.append((name, target))
        current = {"revision": git(self.checkout, "rev-parse", "HEAD").decode().strip(), "links": pairs}
        if not self.check:
            for name, target in stale:
                target.rename(self.backup_path("retired/" + name))
            for name, source, target in changes:
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if os.path.lexists(target):
                    target.rename(self.backup_path("replaced/" + name))
                target.symlink_to(source)
            if previous != current:
                self.state.mkdir(parents=True, mode=0o700, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".links-", dir=self.state)
                with os.fdopen(fd, "w") as stream:
                    json.dump(current, stream, indent=2)
                    stream.write("\n")
                os.replace(temporary, record)
        return self.result(bool(changes or stale or previous != current),
                           linked_files=len(changes), retired_links=len(stale))

    def result(self, changed, **extra):
        result = {"changed": changed, **extra}
        if self.backup is not None:
            result["backup"] = str(self.backup)
        return result


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(argument_spec={
        "action": {"type": "str", "choices": ["prepare", "link"], "required": True},
        "home": {"type": "path", "required": True},
        "checkout": {"type": "path", "required": True},
        "state": {"type": "path", "required": True},
        "files": {"type": "list", "elements": "str", "required": True},
        "special_links": {"type": "list", "elements": "dict", "default": []},
    }, supports_check_mode=True)
    os.umask(0o077)
    params = dict(module.params)
    action = params.pop("action")
    try:
        deployment = Deployment(**params, check=module.check_mode)
        module.exit_json(**getattr(deployment, action)())
    except (ValueError, OSError, KeyError) as exc:
        module.fail_json(msg=str(exc))


if __name__ == "__main__":
    main()
