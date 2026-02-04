# Ansible Collection: aursu.ssh_setup

The `aursu.ssh_setup` collection provides advanced tools for managing OpenSSH server configuration (`sshd_config`).

Unlike standard `lineinfile` or rigid `template` approaches, this collection uses a semantic understanding of the SSH configuration structure. It allows you to surgically edit specific options within **Global** or **Match** scopes while preserving the original file structure, comments, and includes.

## Key Features

* **Structure Preservation:** Edits are performed in-place. Comments, indentation, and file organization are strictly preserved.
* **Context Aware:** Supports editing options inside `Match` blocks (e.g., `Match User bob`) or globally.
* **Smart "First Match Wins":** The module understands OpenSSH's priority logic. It updates the effective (first) occurrence of an option and automatically cleans up shadowed duplicates found in other files or lower down in the config.
* **Include Support:** Seamlessly handles configurations split across multiple files via the `Include` directive.

## Requirements

This collection depends on the **aursu.general** collection for core parsing logic.

```bash
ansible-galaxy collection install aursu.general
```