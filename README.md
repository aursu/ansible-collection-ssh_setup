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

## Modules

### `aursu.ssh_setup.config`

The primary module for managing configuration entries.

#### Parameters

| Parameter | Required | Default | Description |
| --- | --- | --- | --- |
| `key` | Yes | - | The SSH option name (e.g., `Port`, `PasswordAuthentication`). |
| `value` | No | - | The value to set. Required if `state` is `present`. |
| `condition` | No | `global` | The Match condition (e.g., `User bob`, `Address 192.168.*`). Use `global` for main config. |
| `state` | No | `present` | `present` to set/update, `absent` to remove (comment out). |
| `config_path` | No | `/etc/ssh/sshd_config` | Path to the main configuration file. |
| `backup` | No | `false` | Create a backup file before modifying. |

## Usage Examples

### 1. Set Global Options

Change the SSH port in the main configuration file. If the option exists in an included file, it will be updated there.

```yaml
- name: Set SSH Port to 2222
  aursu.ssh_setup.config:
    key: Port
    value: "2222"
    state: present
```

### 2. Configure a Match Block

Disable password authentication only for a specific user. If the `Match User bob` block does not exist, it will be created.

```yaml
- name: Disable Password Auth for User bob
  aursu.ssh_setup.config:
    key: PasswordAuthentication
    value: "no"
    condition: "User bob"
```

### 3. Remove an Option

Remove (comment out) the `PermitRootLogin` option globally. This ensures the default SSH behavior applies.

```yaml
- name: Unset PermitRootLogin (use default)
  aursu.ssh_setup.config:
    key: PermitRootLogin
    state: absent
```

### 4. Complex Setup with Backup

Configure SFTP subsystem and create a backup of the config file.

```yaml
- name: Configure internal SFTP
  aursu.ssh_setup.config:
    key: ForceCommand
    value: "internal-sftp"
    condition: "Group sftp_users"
    backup: true
```

## How it works

1. **Scan:** The module uses the parser from `aursu.general` to scan `sshd_config` and all files included via the `Include` directive.
2. **Analyze:** It builds a map of where every option is defined and identifies the "effective" value (the first occurrence).
3. **Edit:**
* If the option exists, the module updates the effective instance in its original file.
* If the option is duplicated (shadowed) in other files, those duplicates are removed/commented out to prevent confusion.
* If the option is new, it is inserted into the appropriate location (Global or specific Match block).

## License

MIT

## Author

Alexander Ursu [alexander.ursu@gmail.com](mailto:alexander.ursu@gmail.com)
