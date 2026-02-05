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

class SshLine:
    """
    Base class and factory for SSH configuration lines.
    """
    def __init__(self, raw_content):
        self.raw = raw_content
        self.modified = False
        self._diff_info = None

    def render(self):
        return self.raw

    @property
    def diff(self):
        return self._diff_info

    @classmethod
    def create(cls, raw_line):
        """
        Factory method. Analyzes the line and returns an instance of the appropriate subclass.
        """
        stripped = raw_line.strip()

        # 1. Quick check for non-parseable content (comments, blank lines)
        if not stripped or stripped.startswith('#'):
            return IgnoredLine(raw_line)

        # 2. Attempt to parse structure to determine line type
        try:
            parts = shlex.split(stripped)
        except ValueError:
            # If quotes are unclosed or input is malformed
            # treat as Ignored, do not break execution
            return IgnoredLine(raw_line)

        if not parts:
            return IgnoredLine(raw_line)

        first_token = parts[0].lower()

        # 3. Route by line type
        if first_token == 'match':
            # Pass parts so MatchLine does not parse again
            return MatchLine(raw_line, parts=parts)

        if first_token == 'include':
            return IgnoredLine(raw_line)

        # 4. All other cases are treated as configuration options
        return ConfigLine(raw_line, parts=parts)

class IgnoredLine(SshLine):
    """Lines that are not modified (comments, blank lines, includes, parse errors)."""
    pass

class MatchLine(SshLine):
    """Match directive. Defines a scope context."""

    def __init__(self, raw_content, parts=None):
        super().__init__(raw_content)

        # If parts are provided (from Factory) — use them.
        # If not provided (manual object creation in tests) — parse ourselves.
        if parts is None:
            try:
                parts = shlex.split(raw_content.strip())
            except ValueError:
                parts = []

        # Now parts are guaranteed to exist, compute scope
        if parts and len(parts) > 1:
            val = " ".join(parts[1:])
            self.scope = "global" if val.lower() == "all" else val
        else:
            # Fallback for malformed lines
            self.scope = "global"

class ConfigLine(SshLine):
    """Configuration option (Key Value pair)."""
    def __init__(self, raw_content, parts=None):
        super().__init__(raw_content)

        # 1. Compute indentation (fast operation, no shlex needed)
        self.indent = raw_content[:len(raw_content) - len(raw_content.lstrip())]

        # 2. Extract tokens
        if parts is None:
            stripped = raw_content.strip()
            try:
                parts = shlex.split(stripped)
            except ValueError:
                parts = []

        # 3. Populate fields
        if parts:
            self.key = parts[0]
            self.key_lower = self.key.lower()
            self.value = " ".join(parts[1:])
        else:
            # Fallback for empty ConfigLine creation (unlikely, but for safety)
            self.key = ""
            self.key_lower = ""
            self.value = ""

    def update(self, new_value):
        if self.value == new_value:
            return False

        old_val = self.value
        self.value = new_value
        self.modified = True
        # Regenerate the line
        self.raw = f"{self.indent}{self.key} {self.value}\n"
        self._diff_info = {'action': 'update', 'val': new_value, 'old_val': old_val}
        return True

    def comment_out(self):
        self.raw = f"# {self.render().rstrip()} # Removed by Ansible\n"
        self.modified = True
        self._diff_info = {'action': 'remove', 'content': self.key}

class FileManipulator:
    """
    File manipulation handler. Responsible exclusively for stream-based file editing.
    Does not make decisions about which file to modify, simply executes commands.
    """
    def __init__(self, module):
        self.module = module
        self.diffs = []

    def process_file(self, filepath, target_scope, target_key, target_value=None, state="present"):
        if not os.path.exists(filepath):
            return False

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                raw_lines = f.readlines()
        except IOError:
            return False

        # Use the factory method from the class
        line_objects = [SshLine.create(line) for line in raw_lines]

        current_scope = "global"
        target_key_lower = target_key.lower()
        found_in_scope = False
        file_modified = False

        for i, obj in enumerate(line_objects):

            # Polymorphism: check object type
            if isinstance(obj, MatchLine):
                current_scope = obj.scope
                continue

            if isinstance(obj, ConfigLine):
                if current_scope == target_scope and obj.key_lower == target_key_lower:
                    # Business logic
                    if state == "absent":
                        obj.comment_out()
                    elif state == "present":
                        found_in_scope = True
                        obj.update(target_value)

                    if obj.modified:
                        file_modified = True
                        if obj.diff:
                            # Add context (file/line number)
                            diff_entry = obj.diff.copy()
                            diff_entry.update({'file': filepath, 'line': i + 1})
                            self.diffs.append(diff_entry)

        if file_modified:
            new_content = [obj.render() for obj in line_objects]
            self._write_atomic(filepath, new_content)

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
        module.fail_json(msg="Could not import SshConfigParser. Is aursu.general collection installed?")

    config_path = module.params["config_path"]
    key = module.params["key"]
    value = module.params["value"]
    condition = module.params["condition"]
    state = module.params["state"]
    base_dir = os.path.dirname(config_path)

    if state == "present" and value is None:
        module.fail_json(msg="parameter \"value\" is required when state is \"present\"")

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

    manipulator = FileManipulator(module)

    if state == "absent":
        if option_appearance:
            # Remove from all locations where found
            for fpath in option_appearance:
                manipulator.process_file(fpath, condition, key, state="absent")

    elif state == "present":
        if option_location:
            # Scenario A: Option ALREADY exists.
            # 1. Update the "winner" (effective location)
            manipulator.process_file(option_location, condition, key, target_value=value, state="present")

            # 2. Remove the "losers" (shadowed duplicates)
            for fpath in option_appearance:
                if fpath != option_location:
                    manipulator.process_file(fpath, condition, key, state="absent")
        else:
            # Scenario B: Option does NOT exist.
            # Insert into the main file (or user-specified file)
            manipulator.insert_new_option(config_path, condition, key, value)

    module.exit_json(changed=bool(manipulator.diffs), diff=manipulator.diffs)

if __name__ == "__main__":
    main()
