"""Unit tests for the file-manipulation layer of aursu.ssh_setup.config.

These exercise the half of the module that rewrites sshd_config. The other half -
deciding *which* file wins under first-match-wins - belongs to aursu.general's
parser and is tested there.

The cases chosen are the ones that lock a person out of a host if they regress:
a global option landing inside a Match block scopes a fleet-wide setting to one
user, and a shadowed duplicate left active silently overrides the edit that was
just made.
"""

import os
import shutil

import pytest

from ansible_collections.aursu.ssh_setup.plugins.modules.config import (
    ConfigLine,
    FileManipulator,
    IgnoredLine,
    MatchLine,
    SshLine,
)


class FakeModule:
    """The slice of AnsibleModule that FileManipulator actually uses.

    atomic_move is a real move rather than a stub: the module writes through a
    temp file and then moves it, and a stub would let a broken write pass.
    """

    def __init__(self, backup=False, check_mode=False):
        self.params = {"backup": backup}
        self.check_mode = check_mode
        self.backups = []
        self.failures = []

    def backup_local(self, path):
        self.backups.append(path)
        return path + ".backup"

    def atomic_move(self, src, dest):
        shutil.move(src, dest)

    def fail_json(self, **kwargs):
        self.failures.append(kwargs)
        raise AssertionError("fail_json called: %s" % kwargs.get("msg"))


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return str(path)


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class TestLineClassification:
    """SshLine.create is the factory every edit depends on."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("# a comment\n", IgnoredLine),
            ("\n", IgnoredLine),
            ("   \n", IgnoredLine),
            ("Include /etc/ssh/sshd_config.d/*.conf\n", IgnoredLine),
            ("Match User bob\n", MatchLine),
            ("match user bob\n", MatchLine),
            ("Port 22\n", ConfigLine),
            ("    PasswordAuthentication no\n", ConfigLine),
        ],
    )
    def test_classification(self, raw, expected):
        assert isinstance(SshLine.create(raw), expected)

    def test_include_is_ignored_not_edited(self):
        """An Include line must never be rewritten as if it were an option.

        It reads as `Key Value` and would otherwise classify as a ConfigLine named
        `Include`, which an edit could then mangle.
        """
        assert isinstance(SshLine.create("Include /etc/ssh/conf.d/*.conf\n"), IgnoredLine)

    def test_unbalanced_quotes_do_not_crash(self):
        """A malformed line is left alone rather than taking the module down.

        shlex.split raises ValueError on an unclosed quote. The factory catches it
        and degrades to IgnoredLine; this proves that rather than assuming it.
        """
        line = SshLine.create('Banner "/etc/unclosed\n')
        assert isinstance(line, IgnoredLine)
        assert line.render() == 'Banner "/etc/unclosed\n'


class TestMatchScope:
    def test_scope_is_the_condition(self):
        assert MatchLine("Match User bob\n").scope == "User bob"

    def test_match_all_resets_to_global(self):
        """`Match All` returns to global scope - it does not create a scope named 'All'."""
        assert MatchLine("Match All\n").scope == "global"
        assert MatchLine("match all\n").scope == "global"

    def test_bare_match_falls_back_to_global(self):
        assert MatchLine("Match\n").scope == "global"


class TestConfigLine:
    def test_update_preserves_indentation(self):
        line = ConfigLine("    PasswordAuthentication yes\n")
        assert line.update("no") is True
        assert line.render() == "    PasswordAuthentication no\n"

    def test_update_to_the_same_value_is_not_a_change(self):
        """Idempotence at the line level: no diff, no rewrite, no reported change."""
        line = ConfigLine("Port 22\n")
        assert line.update("22") is False
        assert line.modified is False
        assert line.diff is None

    def test_update_preserves_the_original_capitalisation_of_the_key(self):
        """sshd is case-insensitive on keys; the file's own spelling should survive."""
        line = ConfigLine("passwordauthentication yes\n")
        line.update("no")
        assert line.render().strip().startswith("passwordauthentication")

    def test_value_keeps_internal_spacing_as_one_value(self):
        line = ConfigLine("AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2\n")
        assert line.value == ".ssh/authorized_keys .ssh/authorized_keys2"

    def test_comment_out_marks_its_origin(self):
        line = ConfigLine("Port 2222\n")
        line.comment_out()
        assert line.render() == "# Port 2222 # Removed by Ansible\n"
        assert line.modified is True


class TestProcessFile:
    def test_updates_only_the_matching_scope(self, tmp_path):
        """A global edit must not touch the same key inside a Match block."""
        path = write(tmp_path / "sshd_config", (
            "PasswordAuthentication yes\n"
            "Match User bob\n"
            "    PasswordAuthentication yes\n"
        ))
        FileManipulator(FakeModule()).process_file(path, "global", "PasswordAuthentication",
                                                   target_value="no", state="present")
        out = read(path)
        assert out.splitlines()[0] == "PasswordAuthentication no"
        assert out.splitlines()[2] == "    PasswordAuthentication yes"

    def test_updates_inside_a_match_block_when_asked(self, tmp_path):
        path = write(tmp_path / "sshd_config", (
            "PasswordAuthentication yes\n"
            "Match User bob\n"
            "    PasswordAuthentication yes\n"
        ))
        FileManipulator(FakeModule()).process_file(path, "User bob", "PasswordAuthentication",
                                                   target_value="no", state="present")
        out = read(path).splitlines()
        assert out[0] == "PasswordAuthentication yes"
        assert out[2] == "    PasswordAuthentication no"

    def test_match_all_returns_to_global_scope(self, tmp_path):
        """An option after `Match All` is global again, and must be editable as such."""
        path = write(tmp_path / "sshd_config", (
            "Match User bob\n"
            "    X11Forwarding yes\n"
            "Match All\n"
            "X11Forwarding yes\n"
        ))
        FileManipulator(FakeModule()).process_file(path, "global", "X11Forwarding",
                                                   target_value="no", state="present")
        out = read(path).splitlines()
        assert out[1] == "    X11Forwarding yes", "the Match-scoped one must be untouched"
        assert out[3] == "X11Forwarding no"

    def test_comments_blank_lines_and_layout_survive(self, tmp_path):
        original = (
            "# Managed by hand\n"
            "\n"
            "Port 22\n"
            "\n"
            "# trailing note\n"
        )
        path = write(tmp_path / "sshd_config", original)
        FileManipulator(FakeModule()).process_file(path, "global", "Port",
                                                   target_value="2222", state="present")
        out = read(path)
        assert out == original.replace("Port 22\n", "Port 2222\n")

    def test_absent_comments_the_line_out(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\nPermitRootLogin yes\n")
        FileManipulator(FakeModule()).process_file(path, "global", "PermitRootLogin",
                                                   state="absent")
        out = read(path)
        assert "# PermitRootLogin yes # Removed by Ansible" in out
        assert "Port 22\n" in out

    def test_absent_across_three_files_comments_all_three(self, tmp_path):
        """The shadowed-duplicate case: every occurrence must go, not just the winner."""
        manipulator = FileManipulator(FakeModule())
        paths = []
        for name in ("a.conf", "b.conf", "c.conf"):
            paths.append(write(tmp_path / name, "PasswordAuthentication yes\n"))
        for path in paths:
            manipulator.process_file(path, "global", "PasswordAuthentication", state="absent")
        for path in paths:
            assert read(path).startswith("# PasswordAuthentication yes # Removed by Ansible")
        assert len(manipulator.diffs) == 3

    def test_winner_updated_and_shadowed_duplicate_commented(self, tmp_path):
        """What the module does for `state: present` when the option exists twice.

        The file the parser named `location` is edited; every other file has the
        directive commented out. Leaving a duplicate active is the failure that
        makes an edit silently ineffective.
        """
        manipulator = FileManipulator(FakeModule())
        winner = write(tmp_path / "50-cloud-init.conf", "PasswordAuthentication yes\n")
        loser = write(tmp_path / "sshd_config", "PasswordAuthentication yes\n")

        manipulator.process_file(winner, "global", "PasswordAuthentication",
                                 target_value="no", state="present")
        manipulator.process_file(loser, "global", "PasswordAuthentication", state="absent")

        assert read(winner) == "PasswordAuthentication no\n"
        assert read(loser) == "# PasswordAuthentication yes # Removed by Ansible\n"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert FileManipulator(FakeModule()).process_file(
            str(tmp_path / "nope.conf"), "global", "Port", target_value="22") is False

    def test_a_malformed_line_does_not_stop_the_edit(self, tmp_path):
        """An unparseable line earlier in the file must not prevent a later edit."""
        path = write(tmp_path / "sshd_config", 'Banner "/etc/unclosed\nPort 22\n')
        FileManipulator(FakeModule()).process_file(path, "global", "Port",
                                                   target_value="2222", state="present")
        out = read(path)
        assert 'Banner "/etc/unclosed\n' in out, "the malformed line must be left verbatim"
        assert "Port 2222\n" in out

    def test_backup_is_taken_only_when_requested(self, tmp_path):
        module = FakeModule(backup=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        FileManipulator(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.backups == [path]

    def test_no_write_when_nothing_changes(self, tmp_path):
        """If the value already matches, the file must not be rewritten at all."""
        module = FakeModule(backup=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        FileManipulator(module).process_file(path, "global", "Port",
                                             target_value="22", state="present")
        assert module.backups == [], "a no-op must not even take a backup"


class TestInsertNewOption:
    def test_global_option_lands_before_the_first_match(self, tmp_path):
        """THE case. A global option inserted after `Match User bob` would be scoped
        to bob - a fleet-wide setting silently applying to one user, which reads
        correctly in the file and fails only when someone cannot log in.
        """
        path = write(tmp_path / "sshd_config", (
            "Match User bob\n"
            "    PasswordAuthentication yes\n"
        ))
        FileManipulator(FakeModule()).insert_new_option(path, "global", "PermitRootLogin", "no")
        out = read(path).splitlines()
        assert out[0] == "PermitRootLogin no"
        assert out[1] == "Match User bob"

    def test_global_option_appended_when_there_is_no_match_block(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        FileManipulator(FakeModule()).insert_new_option(path, "global", "PermitRootLogin", "no")
        assert read(path) == "Port 22\nPermitRootLogin no\n"

    def test_inserted_into_an_existing_match_block(self, tmp_path):
        path = write(tmp_path / "sshd_config", (
            "Port 22\n"
            "Match User bob\n"
            "    X11Forwarding no\n"
        ))
        FileManipulator(FakeModule()).insert_new_option(
            path, "User bob", "PasswordAuthentication", "no")
        out = read(path).splitlines()
        assert out[1] == "Match User bob"
        assert out[2] == "    PasswordAuthentication no"
        assert out[3] == "    X11Forwarding no"

    def test_a_missing_match_block_is_created(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        FileManipulator(FakeModule()).insert_new_option(
            path, "User carol", "PasswordAuthentication", "no")
        out = read(path)
        assert "Match User carol\n    PasswordAuthentication no" in out

    def test_new_block_is_separated_from_preceding_content(self, tmp_path):
        """Without a blank line the new header can end up glued to the last option."""
        path = write(tmp_path / "sshd_config", "Port 22\n")
        FileManipulator(FakeModule()).insert_new_option(
            path, "User carol", "X11Forwarding", "no")
        assert "\nMatch User carol" in read(path)

    def test_creates_the_file_when_it_does_not_exist(self, tmp_path):
        """Used for a new drop-in under sshd_config.d/."""
        path = str(tmp_path / "conf.d" / "90-ansible.conf")
        FileManipulator(FakeModule()).insert_new_option(path, "global", "Port", "2222")
        assert os.path.exists(path)
        assert read(path) == "Port 2222\n"


class TestCheckMode:
    """A check run must report exactly what a real run would do, and write nothing.

    The value is not the reporting - it is that these hosts often cannot be
    recovered if a write goes wrong, so the preview has to be trustworthy in both
    directions: it must not write, and it must not stay silent about a change.
    """

    def test_update_is_reported_but_not_written(self, tmp_path):
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "PasswordAuthentication yes\n")
        manipulator = FileManipulator(module)
        manipulator.process_file(path, "global", "PasswordAuthentication",
                                 target_value="no", state="present")
        assert read(path) == "PasswordAuthentication yes\n", "the file must be untouched"
        assert len(manipulator.diffs) == 1, "but the change must still be reported"
        assert manipulator.diffs[0]["action"] == "update"
        assert manipulator.diffs[0]["val"] == "no"

    def test_removal_is_reported_but_not_written(self, tmp_path):
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "PermitRootLogin yes\n")
        manipulator = FileManipulator(module)
        manipulator.process_file(path, "global", "PermitRootLogin", state="absent")
        assert read(path) == "PermitRootLogin yes\n"
        assert manipulator.diffs[0]["action"] == "remove"

    def test_insertion_is_reported_but_not_written(self, tmp_path):
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        manipulator = FileManipulator(module)
        manipulator.insert_new_option(path, "global", "PermitRootLogin", "no")
        assert read(path) == "Port 22\n"
        assert manipulator.diffs[0]["action"] == "insert_global"

    def test_check_mode_creates_no_file_and_no_directory(self, tmp_path):
        """The insertion path creates a directory for a new drop-in. Under check
        mode it must create neither - a preview that leaves an empty conf.d/
        behind has already changed the host."""
        module = FakeModule(check_mode=True)
        target = tmp_path / "conf.d" / "90-ansible.conf"
        FileManipulator(module).insert_new_option(str(target), "global", "Port", "2222")
        assert not target.exists()
        assert not target.parent.exists(), "not even the directory"

    def test_check_mode_takes_no_backup(self, tmp_path):
        module = FakeModule(backup=True, check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        FileManipulator(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.backups == []

    def test_no_change_reports_nothing_in_check_mode(self, tmp_path):
        """Idempotence must survive check mode: already-correct reports no change."""
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        manipulator = FileManipulator(module)
        manipulator.process_file(path, "global", "Port", target_value="22", state="present")
        assert manipulator.diffs == []
