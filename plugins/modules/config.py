#!/usr/bin/python
# pyright: reportMissingImports=false
# pylint: disable=import-error

# Copyright (c) 2026 Alexander Ursu <alexander.ursu@gmail.com>
# SPDX-License-Identifier: MIT


import os
import shlex
import tempfile
from ansible.module_utils.basic import AnsibleModule

# --- PARSER IMPORT (FROM SIBLING COLLECTION) ---
# The parser is used exclusively for reading and decision-making.
try:
    from ansible_collections.aursu.general.plugins.module_utils.ssh_parser import (
        CUMULATIVE_DIRECTIVES,
        SEPARATOR,
        SshConfigParser,
    )
except ImportError:
    # Fallback for local debugging or if the collection is not properly installed.
    # In production, this will trigger an Ansible error, which is the correct behavior.
    SshConfigParser = None
    CUMULATIVE_DIRECTIVES = frozenset()
    SEPARATOR = "="  # only for the import-failure path; main() fails before use

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
    description:
      - The value to set. Required when I(state=present).
      - May be a B(list) for the nine cumulative directives - C(Port),
        C(ListenAddress), C(HostKey), C(AcceptEnv), C(AllowUsers), C(DenyUsers),
        C(AllowGroups), C(DenyGroups) and C(Subsystem) - where every occurrence
        takes effect rather than only the first.
      - A list is declarative and means the COMPLETE set - values not listed are
        removed. That is the operation a single string cannot express. You can add
        one C(ListenAddress), but you cannot say I(these two and no others).
      - A list for any other directive is refused. Those are shadowed, so only the
        first occurrence would apply and the rest would be silently dead.
      - Lists are supported in the C(global) scope only.
    type: raw
  condition:
    description: The Match condition block (e.g., 'User bob'). Use 'global' for global options.
    type: str
    default: "global"
  config_path:
    description: Path to the main sshd configuration file.
    type: path
    default: "/etc/ssh/sshd_config"
  state:
    description:
      - Whether the option should be present or absent.
      - C(absent) removes B(every) occurrence of the directive, in every file it
        appears in. For a cumulative directive that means the whole set goes, not
        one member of it.
    choices: [present, absent]
    default: present
  backup:
    description: Create a backup file.
    type: bool
    default: false
  validate:
    description:
      - Command used to check the candidate file before it replaces the real one.
      - Must contain C(%s), which is replaced by the path of the temporary file.
      - Do not quote C(%s) yourself - the module quotes it, so a path containing
        a space is passed as one argument. Quoting again passes sshd a literal quote.
      - Defaults to C(sshd -t -f %s), located with the module's usual binary search.
      - Set to an empty string to skip validation entirely. Doing so on a host with
        no console is how a configuration that prevents sshd starting ends access
        to that host.
    type: str
notes:
  - Supports check mode. A check run reports the change it would make and writes
    nothing - not the file, not a backup, and not the directory a new drop-in
    would need.
  - This module does NOT reload or restart sshd, deliberately. Writing a file and
    restarting a daemon are different decisions, and only the caller knows whether
    several options are being set in one run or whether this is the last of them.
    Notify a handler instead - the module reports C(changed) precisely so that
    works. Restarting inside the module would also make the blast radius of a bad
    edit immediate rather than deferred, on hosts where the daemon in question is
    the only way back in.
author:
  - Alexander Ursu (@aursu)
"""

EXAMPLES = r"""
- name: Set SSH Port globally
  aursu.ssh_setup.config:
    key: Port
    value: "2222"

- name: Disable PasswordAuthentication for User bob
  aursu.ssh_setup.config:
    key: PasswordAuthentication
    value: "no"
    condition: "User bob"

# The module never reloads sshd itself. Notify a handler, so several option
# changes in one play cause exactly one reload, at the end.
- name: Harden the daemon
  aursu.ssh_setup.config:
    key: "{{ item.key }}"
    value: "{{ item.value }}"
  loop:
    - {key: PermitRootLogin, value: prohibit-password}
    - {key: X11Forwarding, value: "no"}
  notify: reload sshd

# handlers:
#   - name: reload sshd
#     ansible.builtin.service:
#       name: sshd
#       state: reloaded

# Cumulative directives take a list, and the list is the complete set:
# exactly these two addresses, any other ListenAddress removed.
- name: Bind sshd to two addresses and no others
  aursu.ssh_setup.config:
    key: ListenAddress
    value:
      - 10.0.0.1
      - 10.0.0.2
  notify: reload sshd

# Skip validation only where sshd is genuinely unavailable - a container image
# being built, say. Never on a host you cannot get a console to.
- name: Set a port in an image with no sshd installed
  aursu.ssh_setup.config:
    key: Port
    value: "2222"
    validate: ""
"""

RETURN = r"""
diff:
  description: List of changes applied.
  returned: changed
  type: list
"""

def as_config_text(value):
    """Render a value the way sshd_config spells it.

    `type: raw` hands the module whatever YAML resolved, so `value: 22` arrives
    as an int and `value: no` as a bool, while everything read back out of a file
    is a string. Comparing the two never matches, and the module would rewrite
    the file and report changed on every run; a mixed list would raise TypeError
    inside sorted().

    Booleans are spelled the way sshd spells them. `value: no` unquoted is False
    in YAML, and "False" is not a thing sshd understands.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


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

    @staticmethod
    def parse_directive(stripped_line):
        """Split one line into its key and the remaining tokens.

        sshd accepts "Key Value", "Key=Value", "Key = Value" and "Key =Value"
        interchangeably. Normalising all four here, once, is what lets the
        factory below route on a clean key - so `Match=User bob` is recognised as
        a Match block by comparing the key to "match", with no prefix matching
        and no special case.

        Returns (None, []) for a line that cannot be lexed.
        """
        try:
            parts = shlex.split(stripped_line)
        except ValueError:
            return None, []

        if not parts:
            return None, []

        key = parts[0]
        rest = parts[1:]

        # "Key=Value" and "Key= Value": the separator is inside the first token.
        if SEPARATOR in key:
            key, _, first_val = key.partition(SEPARATOR)
            if first_val:
                rest = [first_val] + rest

        # "Key = Value" lexes the separator alone; "Key =Value" leads with it.
        elif rest and rest[0].startswith(SEPARATOR):
            first_val = rest[0][len(SEPARATOR):]
            rest = ([first_val] + rest[1:]) if first_val else rest[1:]

        return key, rest

    @classmethod
    def create(cls, raw_line):
        """Return the right subclass for one raw line."""
        stripped = raw_line.strip()

        # Comments and blank lines are never touched.
        if not stripped or stripped.startswith('#'):
            return IgnoredLine(raw_line)

        # Normalise first, then decide. A malformed line - unbalanced quotes, or
        # nothing but a separator - yields no key and is left verbatim.
        key, rest = cls.parse_directive(stripped)
        if not key:
            return IgnoredLine(raw_line)

        first_token = key.lower()

        if first_token == 'match':
            return MatchLine(raw_line, key=key, rest=rest)

        # An Include reads as `Key Value` and would otherwise be editable as an
        # option; it is resolved by the parser, not rewritten here.
        if first_token == 'include':
            return IgnoredLine(raw_line)

        return ConfigLine(raw_line, key=key, rest=rest)


class IgnoredLine(SshLine):
    """Lines that are not modified (comments, blank lines, includes, parse errors)."""
    pass


class MatchLine(SshLine):
    """Match directive. Defines a scope context."""

    def __init__(self, raw_content, key=None, rest=None):
        super().__init__(raw_content)

        # key/rest come from the factory, which has already normalised the
        # separator. Direct construction (tests, ad-hoc use) parses its own.
        if key is None:
            key, rest = SshLine.parse_directive(raw_content.strip())
        rest = rest or []

        if rest:
            val = " ".join(rest)
            # `Match All` returns to global scope; it is not a scope named "All".
            self.scope = "global" if val.lower() == "all" else val
        else:
            # A Match with no condition is malformed - sshd rejects it. Treating
            # it as global is the conservative reading: it cannot silently scope
            # later options to a block that does not exist.
            self.scope = "global"


class ConfigLine(SshLine):
    """Configuration option (Key Value pair)."""

    def __init__(self, raw_content, key=None, rest=None):
        super().__init__(raw_content)

        # Indentation is taken from the raw line, so an edit can put it back.
        self.indent = raw_content[:len(raw_content) - len(raw_content.lstrip())]

        if key is None:
            key, rest = SshLine.parse_directive(raw_content.strip())
        rest = rest or []

        self.key = key or ""
        self.key_lower = self.key.lower()
        self.value = " ".join(rest)

    def update(self, new_value):
        """Replace the value, leaving everything else about the line alone.

        The separator is preserved rather than normalised: a file written as
        `Port=22` stays `Port=2222`, and one written `Port = 22` keeps its
        spaces. Regenerating the line instead would silently restyle files the
        caller asked only to change a value in - and this module exists to
        preserve structure.
        """
        # Coerced here as well as at the module boundary: this class is used
        # directly, and a comparison of "22" against 22 would silently report a
        # change on every run.
        new_value = as_config_text(new_value)

        if self.value == new_value:
            return False

        old_val = self.value
        self.value = new_value
        self.modified = True

        # Walk past the key and whatever separates it from the value, so the
        # original spacing and any '=' survive verbatim.
        idx = len(self.indent) + len(self.key)
        length = len(self.raw)
        start = idx

        while idx < length and self.raw[idx] in (' ', '\t'):
            idx += 1
        if idx < length and self.raw[idx] == SEPARATOR:
            idx += 1
        while idx < length and self.raw[idx] in (' ', '\t'):
            idx += 1

        if idx == start:
            # No separator at all - a key on its own line. Concatenating here
            # would emit 'Port2222'; supply the space the line never had.
            self.raw = self.raw[:start].rstrip() + ' ' + str(new_value) + '\n'
        else:
            self.raw = self.raw[:idx] + str(new_value) + '\n'

        self._diff_info = {'action': 'update', 'val': new_value, 'old_val': old_val}
        return True

    def comment_out(self):
        self.raw = f"# {self.render().rstrip()} # Removed by Ansible\n"
        self.modified = True
        self._diff_info = {'action': 'remove', 'content': self.key}

class SshConfigEditor:
    """
    File manipulation handler. Responsible exclusively for stream-based file editing.
    Does not make decisions about which file to modify, simply executes commands.
    """
    def __init__(self, module):
        self.module = module
        self.diffs = []
        # Check mode is handled in exactly one place - _write_atomic - because
        # that is the only method that touches the filesystem. Everything else
        # reads, decides and records a diff, which is precisely what a check run
        # should still do.
        self.check_mode = getattr(module, "check_mode", False)

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

        # A list, for the cumulative directives where every occurrence takes
        # effect. Each element becomes its own line: that is how sshd expresses a
        # set, and joining them onto one line would mean something else entirely.
        values = value if isinstance(value, list) else [value]
        new_lines = [f"{key} {as_config_text(v)}\n" for v in values]

        if condition == "global":
            # Determine insertion point (before first Match to avoid inserting inside a block)
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                if isinstance(SshLine.create(line), MatchLine):
                    insert_idx = i
                    break
            lines[insert_idx:insert_idx] = new_lines
            self.diffs.append({'file': filepath, 'action': 'insert_global', 'val': value})
        else:
            # Search for Match block header
            match_found = False
            for i, line in enumerate(lines):
                # Classify with the same factory the rest of the module uses,
                # rather than re-implementing the parse here. That removes a
                # second place to keep in step, and it recognises `Match=cond`
                # for free.
                header = SshLine.create(line)
                if isinstance(header, MatchLine):
                    if header.scope == condition:
                        # Block found! Insert immediately after header with indentation
                        lines[i + 1:i + 1] = [f"    {line}" for line in new_lines]
                        match_found = True
                        self.diffs.append({'file': filepath, 'action': 'insert_match', 'val': value})
                        break

            if not match_found:
                # Block does not exist -> create new block at end of file
                # Two separate concerns, and conflating them is how this goes wrong.
                # First: a file whose last line has no newline must get one, or the
                # header is concatenated onto it. Second: a blank line before the
                # block, but only when there is content to separate it from.
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                
                prefix = "\n" if lines and lines[-1].strip() else ""
                body = "".join(f"    {line}" for line in new_lines)
                lines.append(f"{prefix}Match {condition}\n{body}")
                self.diffs.append({'file': filepath, 'action': 'new_block', 'val': value})

        self._write_atomic(filepath, lines)

    def _validate(self, candidate_path):
        """Ask sshd whether the candidate file is legal, before it replaces anything.

        Exit codes, measured on OpenSSH 9.6p1 rather than assumed:

            good config, no host keys  ->  1    "sshd: no hostkeys available -- exiting."
            unknown directive          ->  255  "line 2: Bad configuration option: ..."
            bad value                  ->  255  "line 1: Badly formatted port number."

        The distinction is the whole point. sshd checks syntax BEFORE it looks for
        host keys, so a run that gets as far as complaining about host keys has
        already accepted the configuration. Treating any non-zero exit as failure
        would reject every valid file whenever host keys are unreadable - which is
        the normal case when running unprivileged.
        """
        command = self.module.params.get('validate')

        if command is None:
            sshd = self.module.get_bin_path('sshd', opt_dirs=['/usr/sbin', '/sbin'])
            if not sshd:
                self.module.fail_json(
                    msg="sshd not found, so the candidate configuration cannot be "
                        "validated. Install it, pass an explicit 'validate' command, "
                        "or set validate='' to skip the check.")
            command = "%s -t -f %%s" % sshd

        if not command:
            return  # explicitly disabled by the caller

        if '%s' not in command:
            self.module.fail_json(msg="validate must contain %s, the candidate file path")

        # Quoted because the temp file sits beside config_path, and a path
        # containing a space would otherwise split into two arguments - sshd
        # would be handed a directory and refuse a change that was valid.
        # Callers must therefore NOT quote %s in their own validate template;
        # quoting twice passes sshd a literal quote.
        rc, _stdout, stderr = self.module.run_command(command % shlex.quote(candidate_path))
        if rc == 0:
            return

        # sshd got past parsing and stopped for want of host keys: the file is fine.
        if 'no hostkeys available' in (stderr or ''):
            return

        self.module.fail_json(
            msg="the configuration sshd would have been given is invalid, so "
                "%s was left unchanged: %s" % (self.target_path, (stderr or '').strip()))

    def _write_atomic(self, filepath, lines):
        # The single gate for check mode. Returning here leaves self.diffs
        # populated, so the run still reports what it would have done - and it
        # returns BEFORE backup_local and before makedirs, so a check run creates
        # nothing at all, not even a directory.
        if self.check_mode:
            return

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
            # encoding forced to match the read side. Without it the stream uses
            # the locale default, which is ASCII under the C locale with PEP 538
            # coercion disabled - measured. A config with a non-ASCII comment then
            # reads fine and crashes on write.
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            # Validate the CANDIDATE while the real file is still in place, so a
            # rejected change is a change that never happened - rather than one
            # rolled back after sshd has already been handed a broken file.
            self.target_path = filepath
            self._validate(tmp_path)
            self.module.atomic_move(tmp_path, filepath)
        except (IOError, OSError) as e:
            os.remove(tmp_path)
            self.module.fail_json(msg=f"Failed to write config: {e}")

def main():
    module = AnsibleModule(
        argument_spec=dict(
            config_path=dict(type="path", default="/etc/ssh/sshd_config"),
            key=dict(type="str", required=True),
            value=dict(type="raw", required=False),
            condition=dict(type="str", default="global"),
            state=dict(type="str", choices=["present", "absent"], default="present"),
            backup=dict(type="bool", default=False),
            validate=dict(type="str"),
        ),
        # The hosts where a preview matters most are the ones that cannot be
        # recovered without one: dev-web-013..017 are Proxmox guests with no
        # console. Refusing to run under --check was backwards.
        supports_check_mode=True
    )

    if SshConfigParser is None:
        module.fail_json(msg="Could not import SshConfigParser. Is aursu.general collection installed?")

    config_path = module.params["config_path"]
    key = module.params["key"]
    value = module.params["value"]
    condition = module.params["condition"]
    state = module.params["state"]
    base_dir = os.path.dirname(config_path)

    # `type: raw` hands us whatever YAML resolved, so `value: 22` arrives as an
    # int and `value: no` as a bool, while everything read out of the file is a
    # string. Comparing the two never matches, so the module would rewrite the
    # file and report changed on EVERY run - and a mixed list would raise
    # TypeError inside sorted(). Normalising once here covers both the scalar and
    # the list path; doing it only where the lists are compared would leave the
    # commoner scalar case broken.
    #
    # Booleans are rendered the way sshd spells them, not the way Python does:
    # `value: no` unquoted is False in YAML and must not reach the file as "False".
    if isinstance(value, list):
        value = [as_config_text(v) for v in value]
    elif value is not None:
        value = as_config_text(value)

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
        entry = full_data.get(key)
        if isinstance(entry, dict):
            option_location = entry.get('location')
            option_appearance = entry.get('appearance', [])
    else:
        # Search within Match blocks
        match_blocks = full_data.get('Match', [])
        target_block = next((b for b in match_blocks if b.get('condition') == condition), None)
        if target_block:
            opts = target_block.get('options', {})
            if key in opts:
                option_location = opts[key].get('location')
                option_appearance = opts[key].get('appearance', [])

    # Whether this directive is one of the nine where every occurrence applies.
    # Taken from aursu.general, which measured the list with `sshd -T` rather than
    # inferring it - SetEnv, PermitOpen and PermitListen read as list-like and are
    # NOT in it.
    is_cumulative = key.lower() in CUMULATIVE_DIRECTIVES

    if isinstance(value, list) and not is_cumulative:
        module.fail_json(
            msg="value may only be a list for a cumulative directive; %s is "
                "shadowed, so only its first occurrence would take effect. "
                "Cumulative directives are: %s"
                % (key, ", ".join(sorted(CUMULATIVE_DIRECTIVES))))

    if isinstance(value, list) and condition != "global":
        module.fail_json(
            msg="a list value is supported in the global scope only; sshd permits "
                "few of the cumulative directives inside a Match block, and "
                "reconciling a set there is not implemented rather than guessed at")

    existing_values = []
    if condition == "global":
        entry = full_data.get(key)
        if isinstance(entry, dict):
            raw = entry.get('value')
            existing_values = raw if isinstance(raw, list) else ([raw] if raw else [])

    manipulator = SshConfigEditor(module)

    if state == "absent":
        if option_appearance:
            # Remove from all locations where found
            for fpath in option_appearance:
                manipulator.process_file(fpath, condition, key, state="absent")

    elif state == "present":
        # Cumulative directives are not shadowed: every occurrence takes effect,
        # so a single "the value" is the wrong shape for them. The declared list
        # is the COMPLETE set - anything else present is removed - which is the
        # operation an audit wants and the one a string value cannot express.
        if is_cumulative:
            declared = value if isinstance(value, list) else [value]

            # Idempotence is at the level of the set, not the file. Same members,
            # in any order, means nothing to do - otherwise every run would churn
            # the file and report changed forever.
            if sorted(existing_values) == sorted(declared):
                module.exit_json(changed=False)

            for fpath in option_appearance:
                manipulator.process_file(fpath, condition, key, state="absent")

            target = option_location or config_path
            manipulator.insert_new_option(target, condition, key, declared)

        elif option_location:
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
