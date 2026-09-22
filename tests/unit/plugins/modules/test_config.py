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
    SshConfigEditor,
    IgnoredLine,
    MatchLine,
    SshLine,
)


class FakeModule:
    """The slice of AnsibleModule that SshConfigEditor actually uses.

    atomic_move is a real move rather than a stub: the module writes through a
    temp file and then moves it, and a stub would let a broken write pass.
    """

    def __init__(self, backup=False, check_mode=False, validate=None, rc=0, stderr=""):
        self.params = {"backup": backup, "validate": validate}
        self.check_mode = check_mode
        self.backups = []
        self.failures = []
        self.commands = []
        self._rc = rc
        self._stderr = stderr

    def get_bin_path(self, name, opt_dirs=None):
        return "/usr/sbin/" + name

    def run_command(self, command):
        self.commands.append(command)
        return self._rc, "", self._stderr

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
        SshConfigEditor(FakeModule()).process_file(path, "global", "PasswordAuthentication",
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
        SshConfigEditor(FakeModule()).process_file(path, "User bob", "PasswordAuthentication",
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
        SshConfigEditor(FakeModule()).process_file(path, "global", "X11Forwarding",
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
        SshConfigEditor(FakeModule()).process_file(path, "global", "Port",
                                                   target_value="2222", state="present")
        out = read(path)
        assert out == original.replace("Port 22\n", "Port 2222\n")

    def test_absent_comments_the_line_out(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\nPermitRootLogin yes\n")
        SshConfigEditor(FakeModule()).process_file(path, "global", "PermitRootLogin",
                                                   state="absent")
        out = read(path)
        assert "# PermitRootLogin yes # Removed by Ansible" in out
        assert "Port 22\n" in out

    def test_absent_across_three_files_comments_all_three(self, tmp_path):
        """The shadowed-duplicate case: every occurrence must go, not just the winner."""
        manipulator = SshConfigEditor(FakeModule())
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
        manipulator = SshConfigEditor(FakeModule())
        winner = write(tmp_path / "50-cloud-init.conf", "PasswordAuthentication yes\n")
        loser = write(tmp_path / "sshd_config", "PasswordAuthentication yes\n")

        manipulator.process_file(winner, "global", "PasswordAuthentication",
                                 target_value="no", state="present")
        manipulator.process_file(loser, "global", "PasswordAuthentication", state="absent")

        assert read(winner) == "PasswordAuthentication no\n"
        assert read(loser) == "# PasswordAuthentication yes # Removed by Ansible\n"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert SshConfigEditor(FakeModule()).process_file(
            str(tmp_path / "nope.conf"), "global", "Port", target_value="22") is False

    def test_a_malformed_line_does_not_stop_the_edit(self, tmp_path):
        """An unparseable line earlier in the file must not prevent a later edit."""
        path = write(tmp_path / "sshd_config", 'Banner "/etc/unclosed\nPort 22\n')
        SshConfigEditor(FakeModule()).process_file(path, "global", "Port",
                                                   target_value="2222", state="present")
        out = read(path)
        assert 'Banner "/etc/unclosed\n' in out, "the malformed line must be left verbatim"
        assert "Port 2222\n" in out

    def test_backup_is_taken_only_when_requested(self, tmp_path):
        module = FakeModule(backup=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.backups == [path]

    def test_no_write_when_nothing_changes(self, tmp_path):
        """If the value already matches, the file must not be rewritten at all."""
        module = FakeModule(backup=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
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
        SshConfigEditor(FakeModule()).insert_new_option(path, "global", "PermitRootLogin", "no")
        out = read(path).splitlines()
        assert out[0] == "PermitRootLogin no"
        assert out[1] == "Match User bob"

    def test_global_option_appended_when_there_is_no_match_block(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(path, "global", "PermitRootLogin", "no")
        assert read(path) == "Port 22\nPermitRootLogin no\n"

    def test_inserted_into_an_existing_match_block(self, tmp_path):
        path = write(tmp_path / "sshd_config", (
            "Port 22\n"
            "Match User bob\n"
            "    X11Forwarding no\n"
        ))
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User bob", "PasswordAuthentication", "no")
        out = read(path).splitlines()
        assert out[1] == "Match User bob"
        assert out[2] == "    PasswordAuthentication no"
        assert out[3] == "    X11Forwarding no"

    def test_a_missing_match_block_is_created(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User carol", "PasswordAuthentication", "no")
        out = read(path)
        assert "Match User carol\n    PasswordAuthentication no" in out

    def test_new_block_is_separated_from_preceding_content(self, tmp_path):
        """Without a blank line the new header can end up glued to the last option."""
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User carol", "X11Forwarding", "no")
        assert "\nMatch User carol" in read(path)

    def test_creates_the_file_when_it_does_not_exist(self, tmp_path):
        """Used for a new drop-in under sshd_config.d/."""
        path = str(tmp_path / "conf.d" / "90-ansible.conf")
        SshConfigEditor(FakeModule()).insert_new_option(path, "global", "Port", "2222")
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
        manipulator = SshConfigEditor(module)
        manipulator.process_file(path, "global", "PasswordAuthentication",
                                 target_value="no", state="present")
        assert read(path) == "PasswordAuthentication yes\n", "the file must be untouched"
        assert len(manipulator.diffs) == 1, "but the change must still be reported"
        assert manipulator.diffs[0]["action"] == "update"
        assert manipulator.diffs[0]["val"] == "no"

    def test_removal_is_reported_but_not_written(self, tmp_path):
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "PermitRootLogin yes\n")
        manipulator = SshConfigEditor(module)
        manipulator.process_file(path, "global", "PermitRootLogin", state="absent")
        assert read(path) == "PermitRootLogin yes\n"
        assert manipulator.diffs[0]["action"] == "remove"

    def test_insertion_is_reported_but_not_written(self, tmp_path):
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        manipulator = SshConfigEditor(module)
        manipulator.insert_new_option(path, "global", "PermitRootLogin", "no")
        assert read(path) == "Port 22\n"
        assert manipulator.diffs[0]["action"] == "insert_global"

    def test_check_mode_creates_no_file_and_no_directory(self, tmp_path):
        """The insertion path creates a directory for a new drop-in. Under check
        mode it must create neither - a preview that leaves an empty conf.d/
        behind has already changed the host."""
        module = FakeModule(check_mode=True)
        target = tmp_path / "conf.d" / "90-ansible.conf"
        SshConfigEditor(module).insert_new_option(str(target), "global", "Port", "2222")
        assert not target.exists()
        assert not target.parent.exists(), "not even the directory"

    def test_check_mode_takes_no_backup(self, tmp_path):
        module = FakeModule(backup=True, check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.backups == []

    def test_no_change_reports_nothing_in_check_mode(self, tmp_path):
        """Idempotence must survive check mode: already-correct reports no change."""
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        manipulator = SshConfigEditor(module)
        manipulator.process_file(path, "global", "Port", target_value="22", state="present")
        assert manipulator.diffs == []


class TestValidation:
    """The candidate file is checked before it replaces the real one.

    The exit-code handling is the substance here. sshd checks syntax before it
    looks for host keys, so a run that reaches "no hostkeys available" has already
    accepted the configuration. A validator that treated any non-zero exit as
    failure would reject every valid file whenever host keys are unreadable, which
    is the normal case unprivileged.
    """

    def test_validation_runs_against_the_candidate_not_the_real_file(self, tmp_path):
        module = FakeModule()
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert len(module.commands) == 1
        command = module.commands[0]
        assert command.startswith("/usr/sbin/sshd -t -f ")
        assert path not in command, "it must check the temp file, not the live one"

    def test_a_rejected_configuration_leaves_the_file_untouched(self, tmp_path):
        """rc 255 is sshd saying the config is wrong. The original must survive."""
        module = FakeModule(rc=255, stderr="line 1: Bad configuration option: Nonsense")
        path = write(tmp_path / "sshd_config", "Port 22\n")
        with pytest.raises(AssertionError):
            SshConfigEditor(module).process_file(path, "global", "Port",
                                                 target_value="2222", state="present")
        assert read(path) == "Port 22\n", "the live file must not have been replaced"
        assert "Bad configuration option" in module.failures[0]["msg"]

    def test_missing_host_keys_is_accepted_not_rejected(self, tmp_path):
        """rc 1 with 'no hostkeys available' means the syntax passed.

        Measured on OpenSSH 9.6p1: a valid file run unprivileged exits 1 here.
        Rejecting it would make the module unusable without root.
        """
        module = FakeModule(rc=1, stderr="sshd: no hostkeys available -- exiting.")
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert read(path) == "Port 2222\n", "a valid change must still be applied"
        assert module.failures == []

    def test_validation_can_be_disabled(self, tmp_path):
        module = FakeModule(validate="")
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.commands == []
        assert read(path) == "Port 2222\n"

    def test_a_custom_command_is_used_verbatim(self, tmp_path):
        module = FakeModule(validate="/opt/sshd -t -f %s")
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.commands[0].startswith("/opt/sshd -t -f ")

    def test_a_command_without_the_placeholder_is_refused(self, tmp_path):
        """Without %s the check would silently validate the wrong file."""
        module = FakeModule(validate="/usr/sbin/sshd -t")
        path = write(tmp_path / "sshd_config", "Port 22\n")
        with pytest.raises(AssertionError):
            SshConfigEditor(module).process_file(path, "global", "Port",
                                                 target_value="2222", state="present")
        assert "%s" in module.failures[0]["msg"]

    def test_check_mode_does_not_validate(self, tmp_path):
        """Nothing is written, so there is no candidate to check."""
        module = FakeModule(check_mode=True)
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        assert module.commands == []


class TestCumulativeValues:
    """A list of values for a directive where every occurrence takes effect.

    The writer must emit one line per value. Joining them onto a single line
    would either mean something different to sshd or nothing at all, and is the
    obvious wrong implementation.
    """

    def test_a_list_becomes_one_line_per_value(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "global", "ListenAddress", ["10.0.0.1", "10.0.0.2"])
        out = read(path)
        assert "ListenAddress 10.0.0.1\n" in out
        assert "ListenAddress 10.0.0.2\n" in out
        assert "ListenAddress 10.0.0.1 10.0.0.2" not in out, "must not join onto one line"

    def test_a_single_string_still_works(self, tmp_path):
        """The shadowed case is unchanged - this is an addition, not a migration."""
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "global", "PermitRootLogin", "no")
        assert read(path) == "Port 22\nPermitRootLogin no\n"

    def test_a_list_lands_before_the_first_match_too(self, tmp_path):
        """The scoping trap applies to every value, not just the first."""
        path = write(tmp_path / "sshd_config", "Match User bob\n    X11Forwarding no\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "global", "ListenAddress", ["10.0.0.1", "10.0.0.2"])
        out = read(path).splitlines()
        assert out[0] == "ListenAddress 10.0.0.1"
        assert out[1] == "ListenAddress 10.0.0.2"
        assert out[2] == "Match User bob"

    def test_a_list_inside_a_new_match_block_is_indented(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User bob", "AcceptEnv", ["LANG", "LC_ALL"])
        out = read(path)
        assert "Match User bob\n    AcceptEnv LANG\n    AcceptEnv LC_ALL" in out

    def test_a_list_inside_an_existing_match_block(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Match User bob\n    X11Forwarding no\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User bob", "AcceptEnv", ["LANG", "LC_ALL"])
        out = read(path).splitlines()
        assert out[1] == "    AcceptEnv LANG"
        assert out[2] == "    AcceptEnv LC_ALL"
        assert out[3] == "    X11Forwarding no"

    def test_removing_a_cumulative_directive_takes_every_occurrence(self, tmp_path):
        """state: absent is the whole set, which is what was decided - not one member."""
        path = write(tmp_path / "sshd_config", (
            "ListenAddress 10.0.0.1\n"
            "ListenAddress 10.0.0.2\n"
            "Port 22\n"
        ))
        SshConfigEditor(FakeModule()).process_file(path, "global", "ListenAddress",
                                                   state="absent")
        out = read(path)
        assert out.count("# ListenAddress") == 2
        assert "Port 22\n" in out


class TestCumulativeSetSemantics:
    """The set-level reconciliation, tested through the same helpers main() uses.

    These assert the property that makes a list declarative: after the module
    runs, the file contains exactly the declared members and nothing else.
    """

    def test_replacing_a_set_removes_what_is_no_longer_declared(self, tmp_path):
        manipulator = SshConfigEditor(FakeModule())
        path = write(tmp_path / "sshd_config", (
            "ListenAddress 10.0.0.1\n"
            "ListenAddress 10.0.0.2\n"
            "ListenAddress 10.0.0.3\n"
        ))
        # what main() does: clear every occurrence, then write the declared set
        manipulator.process_file(path, "global", "ListenAddress", state="absent")
        manipulator.insert_new_option(path, "global", "ListenAddress",
                                      ["10.0.0.1", "10.0.0.9"])
        active = [l for l in read(path).splitlines()
                  if l.startswith("ListenAddress")]
        assert active == ["ListenAddress 10.0.0.1", "ListenAddress 10.0.0.9"]
        assert read(path).count("# ListenAddress") == 3, "the old three are commented out"


class TestEqualsSpellings:
    """sshd accepts `Key Value`, `Key=Value`, `Key = Value` and `Key =Value`.

    aursu.general's parser handles all four. Until now the writer handled only
    the first, which made the two halves disagree: the parser reported the
    directive present, the writer could not find it, and the module reported
    changed: false having done nothing. Silently doing nothing is the worst
    failure mode for a tool you trust to harden a host.
    """

    @pytest.mark.parametrize("raw", [
        "Port 22\n", "Port=22\n", "Port = 22\n", "Port =22\n", "Port= 22\n",
    ])
    def test_key_and_value_are_extracted_from_every_spelling(self, raw):
        line = SshLine.create(raw)
        assert isinstance(line, ConfigLine)
        assert line.key == "Port"
        assert line.value == "22"

    def test_match_with_equals_is_a_match_line(self, raw=None):
        """`Match=User bob` used to classify as a ConfigLine, so scope never
        changed and a global edit could rewrite a Match-scoped option."""
        line = SshLine.create("Match=User bob\n")
        assert isinstance(line, MatchLine)
        assert line.scope == "User bob"

    def test_a_directive_merely_starting_with_match_is_not_a_match_line(self):
        """Routing happens on the normalised key, so this is correct by
        construction rather than by a special case. The test stays because the
        obvious implementations - matching a prefix, or looking at the raw token
        before the separator is normalised - both get it wrong."""
        assert isinstance(SshLine.create("MatchingFoo yes\n"), ConfigLine)


class TestUpdatePreservesStyle:
    """An edit must not restyle the line it touches.

    Recognising `Port=22` is only half the fix: writing it back as `Port 2222`
    would silently reformat files the module was asked only to change a value in.
    """

    @pytest.mark.parametrize("raw,expected", [
        ("Port 22\n", "Port 2222\n"),
        ("Port=22\n", "Port=2222\n"),
        ("Port = 22\n", "Port = 2222\n"),
        ("Port =22\n", "Port =2222\n"),
        ("Port= 22\n", "Port= 2222\n"),
        ("    Port 22\n", "    Port 2222\n"),
        ("Port\t22\n", "Port\t2222\n"),
    ])
    def test_separator_and_indentation_survive(self, raw, expected):
        line = SshLine.create(raw)
        line.update("2222")
        assert line.render() == expected

    def test_a_key_with_no_value_still_gets_a_separator(self):
        """The edge case the index walk falls into: with nothing to skip past, a
        naive implementation concatenates and emits `Port2222`."""
        line = SshLine.create("Port\n")
        line.update("2222")
        assert line.render() == "Port 2222\n"

    def test_the_key_capitalisation_in_the_file_is_kept(self):
        line = SshLine.create("PORT=22\n")
        line.update("2222")
        assert line.render() == "PORT=2222\n"


class TestEqualsSpellingsEndToEnd:
    """The bug as it actually bit: through process_file and insert_new_option."""

    def test_an_equals_written_directive_is_found_and_updated(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port=22\n")
        SshConfigEditor(FakeModule()).process_file(path, "global", "Port",
                                                   target_value="2222", state="present")
        assert read(path) == "Port=2222\n"

    def test_scope_is_tracked_across_a_match_with_equals(self, tmp_path):
        """A global edit must not reach into `Match=User bob`."""
        path = write(tmp_path / "sshd_config", (
            "X11Forwarding yes\n"
            "Match=User bob\n"
            "    X11Forwarding yes\n"
        ))
        SshConfigEditor(FakeModule()).process_file(path, "global", "X11Forwarding",
                                                   target_value="no", state="present")
        out = read(path).splitlines()
        assert out[0] == "X11Forwarding no"
        assert out[2] == "    X11Forwarding yes", "the Match-scoped one must survive"

    def test_insertion_finds_a_match_block_written_with_equals(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Match=User bob\n    X11Forwarding no\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User bob", "PasswordAuthentication", "no")
        out = read(path).splitlines()
        assert out[1] == "    PasswordAuthentication no", "must go inside the block"
        assert "Match User bob" not in read(path), "must not create a second block"

    def test_removal_finds_an_equals_written_directive(self, tmp_path):
        path = write(tmp_path / "sshd_config", "PermitRootLogin=yes\n")
        SshConfigEditor(FakeModule()).process_file(path, "global", "PermitRootLogin",
                                                   state="absent")
        assert read(path).startswith("# PermitRootLogin=yes # Removed by Ansible")


class TestFileEndings:
    """Appending a Match block to the end of a file.

    Two concerns that look like one: a file whose last line has no newline must
    get one, and a blank line belongs before the new block only when there is
    content to separate it from. Handling only the second concatenates the header
    onto the last line; handling only the first loses the separator.
    """

    def test_a_file_with_no_trailing_newline_is_terminated_first(self, tmp_path):
        path = write(tmp_path / "sshd_config", "# end of file")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User carol", "X11Forwarding", "no")
        out = read(path)
        assert "fileMatch" not in out, "the header must not be glued to the last line"
        assert out.startswith("# end of file\n")

    def test_a_whitespace_only_unterminated_last_line(self, tmp_path):
        """The case the obvious `if lines[-1].strip()` check gets wrong: falsy,
        so no separator is added, and the header lands on the same line."""
        path = write(tmp_path / "sshd_config", "Port 22\n   ")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User carol", "X11Forwarding", "no")
        out = read(path)
        assert "   Match" not in out, "the header must not be glued to the blank line"
        assert "Match User carol" in out

    def test_a_blank_line_still_separates_the_new_block(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User carol", "X11Forwarding", "no")
        assert read(path) == "Port 22\n\nMatch User carol\n    X11Forwarding no\n"

    def test_an_empty_file_gets_no_leading_blank_line(self, tmp_path):
        path = write(tmp_path / "sshd_config", "")
        SshConfigEditor(FakeModule()).insert_new_option(
            path, "User carol", "X11Forwarding", "no")
        assert read(path).startswith("Match User carol")


class TestValidationCommandQuoting:
    def test_a_candidate_path_with_a_space_is_passed_as_one_argument(self, tmp_path):
        """config_path may sit in a directory with a space. The temp file is
        created beside it, so an unquoted path splits into two arguments and sshd
        is handed a directory - refusing a change that was perfectly valid."""
        import shlex as _shlex
        d = tmp_path / "ssh config"
        d.mkdir()
        module = FakeModule()
        path = write(d / "sshd_config", "Port 22\n")
        SshConfigEditor(module).process_file(path, "global", "Port",
                                             target_value="2222", state="present")
        argv = _shlex.split(module.commands[0])
        assert len(argv) == 4, "expected sshd -t -f <one path>, got %r" % (argv,)
        assert " " in argv[3], "the path really does contain the space"


class TestNonAsciiContent:
    """A config may legitimately contain non-ASCII - a name in a comment, say.

    The read side has always forced utf-8; the write side used the locale
    default. On a host under the C locale with PEP 538 coercion disabled that is
    ASCII, so such a file reads fine and crashes on write. This test passes
    either way under a utf-8 locale; it bites when the suite is run with
    PYTHONCOERCECLOCALE=0 and LC_ALL=C, which is how the bug was reproduced.
    """

    def test_a_non_ascii_comment_survives_an_edit(self, tmp_path):
        original = "# Kontakt: Björn Müller\nPort 22\n"
        path = write(tmp_path / "sshd_config", original)
        SshConfigEditor(FakeModule()).process_file(path, "global", "Port",
                                                   target_value="2222", state="present")
        out = read(path)
        assert "Björn Müller" in out
        assert "Port 2222\n" in out


class TestNonStringValues:
    """`type: raw` hands us whatever YAML resolved, so `value: 22` arrives as an
    int while everything parsed out of the file is a string.

    The loud failure is a TypeError, and it needs a mixed list. The quiet one is
    worse and far more likely: an int never compares equal to the string in the
    file, so the module rewrites it and reports changed on every single run.
    """

    def test_an_integer_value_is_idempotent(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        manipulator = SshConfigEditor(FakeModule())
        manipulator.process_file(path, "global", "Port", target_value=22, state="present")
        assert manipulator.diffs == [], "22 and '22' are the same setting"
        assert read(path) == "Port 22\n"

    def test_an_integer_value_that_differs_is_still_applied(self, tmp_path):
        path = write(tmp_path / "sshd_config", "Port 22\n")
        SshConfigEditor(FakeModule()).process_file(path, "global", "Port",
                                                   target_value=2222, state="present")
        assert read(path) == "Port 2222\n"

    def test_a_boolean_value_is_rendered_as_yaml_wrote_it(self, tmp_path):
        """`value: no` unquoted is a bool in YAML. It must not land as 'False'."""
        path = write(tmp_path / "sshd_config", "X11Forwarding yes\n")
        SshConfigEditor(FakeModule()).process_file(path, "global", "X11Forwarding",
                                                   target_value=False, state="present")
        assert "False" not in read(path), "a Python bool must not reach the file"
