# Ansible Role: Dotfiles

Deploy a reviewed portable-env revision using Git and symlinks. The checkout is
updated in place; this role does not purge home directories or replace whole
Cargo, GnuPG, authentication, history, cache or plugin trees. No chezmoi dependency.

Run the role as the account owner, with an explicit absolute home and full commit
SHA. The caller supplies Git and Python 3.9+; shell/editor/tool preparation belongs
to the host baseline and its explicit preparation workflow.

```yaml
- name: Deploy one account
  hosts: servers
  become: true
  become_user: james
  roles:
    - role: ansible-role-dotfiles
      dotfiles_home: /home/james
      dotfiles_repo_version: REVIEWED_40_CHARACTER_COMMIT
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `dotfiles_repo` | `https://github.com/imajes/portable-env.git` | Source repository. |
| `dotfiles_repo_version` | empty, required | Full 40-character reviewed commit SHA; publish referenced submodule commits first. |
| `dotfiles_home` | empty, required | Absolute existing account home. Run as this account. |
| `dotfiles_repo_local_destination` | `{{ dotfiles_home }}/.dotfiles` | Persistent per-account source checkout. |
| `dotfiles_state_directory` | `{{ dotfiles_home }}/.local/state/dotfiles` | Private backup and managed-link records, outside the source checkout. |
| `dotfiles_repo_accept_hostkey` | `false` | Explicit Git SSH host-key policy; the default source uses HTTPS. |
| `dotfiles_files` | reviewed list in `defaults/main.yml` | Exact tracked files or tracked-file directory selections. Directory selections expand through pinned submodules. |
| `dotfiles_special_links` | `.zshenv` to `.config/zsh/.zshenv` | Destination/source mappings for tracked files. |
| `dotfiles_files_to_purge` | `[]` | Compatibility input. Nonempty values are rejected; no purge action exists. |

The default selection includes the reviewed shell/editor, Git, theme and small
utility preferences. It excludes `.gnupg`, `.cargo`, `.npmrc`, GitHub account
metadata, agent homes, histories, installed binaries and workstation runtime state.
The Git checkout can still contain historical tracked material; this role does not
certify or remove that history. Unselected files are not installed into account paths.

## Existing account behavior

- Real config directories stay real. Only selected tracked files become links;
  neighboring Zim modules, zsh-z data, credentials and other untracked state stay put.
- Existing directory links into this checkout remain valid. Updating the checkout
  retains their ignored/untracked runtime files. The role never removes the checkout.
- Replaced managed files and old links move to private timestamp-independent unique
  backup directories. Existing permissions and external targets of old file links
  are preserved. Newly created parent directories use the account's private umask.
- Edits to managed tracked checkout files are copied into private backups before
  restoring the recorded version and updating Git. Configuration remains authoritative.
- Staged changes, unmanaged tracked edits, locally selected submodule commits,
  escaping parent links and file/directory conflicts fail before those edits are
  overwritten. Resolve the reported path deliberately; do not force-reset the checkout.
- Links previously installed by this role that leave the selection move into backup.
  Unselected real files and unrelated links are not removed.
- Check mode plans link/backup changes without applying them. A missing checkout
  needs a normal fetch before its complete link plan can be inspected.

`links.json` records only managed paths and the selected revision. Backups are under
`backups/config-*/{checkout,replaced,retired}/`; they can contain private old config
and remain on the account. They are not source-sync inputs or automatically deleted.

The small `dotfiles_state` module implements backup/link handling. Ansible's Git
module owns clone/fetch/checkout/submodule operations with `force: false`; no broad
reset, clean, recursive deletion or directory-copy operation is used.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

Tests use disposable real Git repositories and homes. They cover real directories,
legacy directory links, private neighbors, managed-edit backups, nested submodules,
check mode, state-preserving failure paths and repeat convergence. The containing
Ansible repository adds actual-role and combined appearance fixtures for synthetic
James/root homes without privilege escalation. Real root ownership and SSH shell
acceptance still need an explicitly scoped host pilot.

## License and provenance

MIT / BSD. Originally created by Jeff Geerling; this fork retains the Git/symlink
approach with bounded account-state preservation for the lab's portable environment.
