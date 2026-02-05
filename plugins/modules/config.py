#!/usr/bin/python
# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false
# pylint: disable=import-error

# Copyright (c) 2026 Alexander Ursu <alexander.ursu@gmail.com>
# SPDX-License-Identifier: MIT

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import shlex
import tempfile
from ansible.module_utils.basic import AnsibleModule

# --- PARSER IMPORT (FROM SIBLING COLLECTION) ---
# The parser is used exclusively for reading and decision-making.
try:
    from ansible_collections.aursu.general.plugins.module_utils.ssh_parser import SshConfigParser
except ImportError:
    # Fallback for local debugging or if the collection is not properly installed.
    # In production, this will trigger an Ansible error, which is the correct behavior.
    SshConfigParser = None

DOCUMENTATION = r"""
module: config
short_description: Manage OpenSSH server configuration preserving structure
version_added: "1.0.0"
description:
  - Manages SSH configuration parameters in sshd_config and included files.
  - Uses a shared parser (from aursu.general) to determine effective values and scopes.
  - Preserves comments, spacing, and file structure during edits.
options:
  key:
    description: The SSH option name (e.g., Port).
    type: str
    required: true
  value:
    description: The value to set. Required if state is present.
    type: str
  condition:
    description: The Match condition block (e.g., 'User bob'). Use 'global' for global options.
    type: str
    default: "global"
  config_path:
    description: Path to the main sshd configuration file.
    type: path
    default: "/etc/ssh/sshd_config"
  state:
    description: Whether the option should be present or absent.
    choices: [present, absent]
    default: present
  backup:
    description: Create a backup file.
    type: bool
    default: false
author:
  - Alexander Ursu (@aursu)
"""

EXAMPLES = r"""
- name: Set SSH Port globally
  aursu.sshd_setup.config:
    key: Port
    value: "2222"

- name: Disable PasswordAuthentication for User bob
  aursu.sshd_setup.config:
    key: PasswordAuthentication
    value: "no"
    condition: "User bob"
"""

RETURN = r"""
diff:
  description: List of changes applied.
  returned: changed
  type: list
"""

class FileManipulator:
    """
    File manipulation handler. Responsible exclusively for stream-based file editing.
    Does not make decisions about which file to modify, simply executes commands.
    """
    def __init__(self, module):
        self.module = module
        self.diffs = []

    def update_file(self, filepath, target_scope, key, value=None, mode="ensure_present"):
        """
        Scans the file line by line.
        mode='ensure_present': Updates the key value in the target scope.
        mode='ensure_absent': Comments out the key in the target scope.
        Returns True if the key was found in this file.
        """
        if not os.path.exists(filepath):
            return False

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except IOError:
            return False

        new_lines = []
        modified = False
        current_scope = "global"
        target_key_lower = key.lower()
        found_in_scope = False

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Preserve file structure (comments, blank lines)
            if not stripped or stripped.startswith('#'):
                new_lines.append(line)
                continue

            try:
                parts = shlex.split(stripped)
            except ValueError:
                new_lines.append(line)
                continue

            if not parts:
                new_lines.append(line)
                continue

            row_key = parts[0].lower()

            # --- Context management (Match directives) ---
            if row_key == "match":
                val = " ".join(parts[1:])
                current_scope = "global" if val.lower() == "all" else val
                new_lines.append(line)
                continue

            if row_key == "include":
                new_lines.append(line)
                continue

            # --- Option processing ---
            if current_scope == target_scope and row_key == target_key_lower:
                if mode == "ensure_absent":
                    # Remove by commenting out
                    new_lines.append(f"# {line.rstrip()} # Removed by Ansible\n")
                    modified = True
                    self.diffs.append({'file': filepath, 'action': 'remove', 'line': i+1, 'content': stripped})

                elif mode == "ensure_present":
                    found_in_scope = True
                    current_val = " ".join(parts[1:])

                    if current_val != value:
                        # Update while preserving original indentation
                        indent = line[:len(line) - len(line.lstrip())]
                        new_lines.append(f"{indent}{key} {value}\n")
                        modified = True
                        self.diffs.append({'file': filepath, 'action': 'update', 'line': i+1, 'val': value})
                    else:
                        # Value is already correct
                        new_lines.append(line)
            else:
                new_lines.append(line)

        if modified:
            self._write_atomic(filepath, new_lines)

        return found_in_scope

    def insert_new_option(self, filepath, condition, key, value):
        """
        Inserts a new option if it was not found during scanning.
        Global -> inserts before the first Match directive or at the end.
        Match -> locates the block and inserts inside, or creates a new block.
        """
        if not os.path.exists(filepath):
            # If file does not exist (e.g., new include), create it
            lines = []
        else:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
            except IOError:
                self.module.fail_json(msg=f"Cannot read file for insertion: {filepath}")

        new_line = f"{key} {value}\n"
        inserted = False

        if condition == "global":
            # Determine insertion point (before first Match to avoid inserting inside a block)
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                if line.strip().lower().startswith('match '):
                    insert_idx = i
                    break
            lines.insert(insert_idx, new_line)
            self.diffs.append({'file': filepath, 'action': 'insert_global', 'val': value})
        else:
            # Search for Match block header
            match_found = False
            for i, line in enumerate(lines):
                stripped = line.strip().lower()
                # Simplified header check
                if stripped.startswith('match '):
                     # Parse precisely to verify condition
                     try:
                         parts = shlex.split(line.strip())
                         block_scope = " ".join(parts[1:])
                     except ValueError:
                         continue

                     if block_scope == condition:
                         # Block found! Insert immediately after header with indentation
                         lines.insert(i + 1, f"    {new_line}")
                         match_found = True
                         inserted = True
                         self.diffs.append({'file': filepath, 'action': 'insert_match', 'val': value})
                         break

            if not match_found:
                # Block does not exist -> create new block at end of file
                prefix = "\n" if lines and lines[-1].strip() else ""
                lines.append(f"{prefix}Match {condition}\n    {new_line}")
                self.diffs.append({'file': filepath, 'action': 'new_block', 'val': value})

        self._write_atomic(filepath, lines)

    def _write_atomic(self, filepath, lines):
        if self.module.params['backup']:
            self.module.backup_local(filepath)

        dir_path = os.path.dirname(filepath)
        # If creating a new file in conf.d/
        if not os.path.exists(dir_path):
            try:
                os.makedirs(dir_path)
            except OSError as e:
                self.module.fail_json(msg=f"Failed to create directory {dir_path}: {e}")

        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path, text=True)
        try:
            with os.fdopen(tmp_fd, 'w') as f:
                f.writelines(lines)
            self.module.atomic_move(tmp_path, filepath)
        except (IOError, OSError) as e:
            os.remove(tmp_path)
            self.module.fail_json(msg=f"Failed to write config: {e}")


def main():
    module = AnsibleModule(
        argument_spec=dict(
            config_path=dict(type="path", default="/etc/ssh/sshd_config"),
            key=dict(type="str", required=True),
            value=dict(type="str", required=False),
            condition=dict(type="str", default="global"),
            state=dict(type="str", choices=["present", "absent"], default="present"),
            backup=dict(type="bool", default=False),
        ),
        supports_check_mode=False
    )

    if SshConfigParser is None:
        module.fail_json(msg="Could not import SshConfigParser from aursu.general. Is the collection installed?")

    config_path = module.params["config_path"]
    key = module.params["key"]
    value = module.params["value"]
    condition = module.params["condition"]
    state = module.params["state"]
    base_dir = os.path.dirname(config_path)

    if state == "present" and value is None:
        module.fail_json(msg="parameter \"value\" is required when state is \"present\"")

    # --- 1. ANALYSIS (Using Parser) ---
    if not os.path.exists(config_path) and state == "absent":
        # If config does not exist and we want to remove - already satisfied, no action needed
        module.exit_json(changed=False)

    parser = SshConfigParser(base_dir=base_dir)
    # If file exists, parse it. If not (creating from scratch), parser will skip.
    if os.path.exists(config_path):
        parser.parse(config_path, "global")

    full_data = parser.get_structured_data()

    # Locate where the option currently exists
    option_location = None
    option_appearance = []

    # Helper to extract metadata from the structure
    if condition == "global":
        if key in full_data and isinstance(full_data[key], dict):
            option_location = full_data[key].get('location')
            option_appearance = full_data[key].get('appearance', [])
    else:
        # Search within Match blocks
        match_blocks = full_data.get('Match', [])
        target_block = next((b for b in match_blocks if b.get('condition') == condition), None)
        if target_block:
            opts = target_block.get('options', {})
            if key in opts:
                option_location = opts[key].get('location')
                option_appearance = opts[key].get('appearance', [])

    # --- 2. EXECUTION (Using Manipulator) ---
    manipulator = FileManipulator(module)

    if state == "absent":
        if option_appearance:
            # Remove from all locations where found
            for fpath in option_appearance:
                manipulator.update_file(fpath, condition, key, mode="ensure_absent")

    elif state == "present":
        if option_location:
            # Scenario A: Option ALREADY exists.
            # 1. Update the "winner" (effective location)
            manipulator.update_file(option_location, condition, key, value, mode="ensure_present")

            # 2. Remove the "losers" (shadowed duplicates)
            for fpath in option_appearance:
                if fpath != option_location:
                    manipulator.update_file(fpath, condition, key, mode="ensure_absent")
        else:
            # Scenario B: Option does NOT exist.
            # Insert into the main file (or user-specified file)
            manipulator.insert_new_option(config_path, condition, key, value)

    module.exit_json(changed=bool(manipulator.diffs), diff=manipulator.diffs)

if __name__ == "__main__":
    main()
