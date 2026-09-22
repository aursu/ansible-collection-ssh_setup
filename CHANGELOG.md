# Changelog

All notable changes to `aursu.ssh_setup` are documented here.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Check mode.** `--check` previously refused to run the module at all, which was backwards: the
  hosts where a preview matters most are the ones with no console. A check run now reports the
  change it would make and writes nothing - not the file, not a backup, and not the directory a
  new drop-in would need.

- **`sshd -t` validation of the candidate file**, spelled `validate` after
  `ansible.builtin.template`/`copy`/`lineinfile`. It runs against the temporary file while the
  original is still in place, so a rejected change is one that never happened rather than one
  rolled back after sshd has been handed a broken file.

  The exit-code handling is the substance. Measured on OpenSSH 9.6p1: a syntactically valid file
  run unprivileged exits **1** with `no hostkeys available`, while a bad directive or value exits
  **255**. sshd checks syntax before it looks for host keys, so exit 1 there means the
  configuration was accepted. The obvious implementation - treat any non-zero exit as failure -
  would reject every valid file whenever host keys are unreadable.

- **Cumulative directives accept a list.** `value` may be a list for the nine directives where
  every occurrence takes effect, and the list is **declarative**: it is the complete set, and
  values not listed are removed. Previously you could add one `ListenAddress` but could not say
  *these two and no others*, which is the operation an audit actually wants.

  The shape mirrors `aursu.general` 1.7.0's read side deliberately - one field, a string when the
  directive is shadowed and a list when it is cumulative - so the reader and the writer do not
  disagree. A list on a shadowed directive is refused rather than silently writing lines that
  would never take effect. Idempotence is at the level of the set, so the same members in a
  different order report no change.

- **Unit tests**, 55 of them, over the file-manipulation layer. Each of four deliberate mutants
  was checked to fail them before the suite was trusted.

### Changed

- **`FileManipulator` is now `SshConfigEditor`.** The old name described a generic file utility;
  this class is the write half of an sshd_config editor, and the name should say so.

- **Python 2 compatibility headers removed** - `# -*- coding: utf-8 -*-`, the `__future__` import
  and `__metaclass__ = type`. The collection requires ansible-core 2.15, so they were inert.


- **`aursu.general` floor raised to `>=1.7.0`** from `>=1.5.0`. The module now imports
  `CUMULATIVE_DIRECTIVES` from the parser and reads the list-typed `value` that 1.7.0 introduced.
  A floor lower than what the code needs is the same class of defect as declaring no dependency.

- **`state: absent` documented as removing every occurrence**, in every file the directive appears
  in. That was already the behaviour; for a cumulative directive it means the whole set goes.

- **The module still does not reload sshd, and now says so.** Writing a file and restarting a
  daemon are different decisions, and only the caller knows whether this is the last of several
  options being set. Notify a handler - `changed` is reported so that works.

### Fixed

- **The writer did not understand `Key=Value`, and silently did nothing.** sshd accepts four
  spellings of a directive and `aursu.general`'s parser reads all four; this module's writer read
  only `Key Value`. On a file written as `Port=22` the parser reported the directive present, the
  writer failed to match it, and the module returned **`changed: false` having made no change** -
  the worst failure mode for a tool trusted to harden a host. All four spellings now parse.

- **`Match=User bob` was not recognised as a Match block.** It classified as an ordinary option,
  so the scope never changed and a global edit could rewrite an option inside that block.

  Fixed by normalising the separator **before** deciding what a line is: `SshLine.parse_directive`
  splits every line into a clean key and its remaining tokens, and the factory routes on that key.
  `Match=User bob` is then recognised by comparing the key to `match`, with no prefix matching and
  no special case - and a directive that merely begins with "match" routes correctly by
  construction.

- **An edit no longer restyles the line it touches.** The value is replaced in place, preserving
  the original separator and spacing: `Port=22` becomes `Port=2222`, `Port = 22` keeps its spaces,
  and tabs and indentation survive. Regenerating the line - as it did before - would have silently
  reformatted files the caller asked only to change a value in, which this module exists not to do.
  A key written on its own line with no value still gains a separator rather than being
  concatenated into `Port2222`.

- **A non-string `value` made the module report `changed` on every run.** `type: raw` hands the
  module whatever YAML resolved, so `value: 22` arrives as an int and `value: no` as a bool, while
  everything read back out of a file is a string. `"22" == 22` is false, so the module rewrote the
  file and reported a change every single time it ran.

  A mixed list - `value: [22, "2222"]` - additionally raised `TypeError` inside `sorted()`. That is
  the louder failure and the rarer one; the silent non-idempotence is the common case.

  Values are now rendered through one helper used by both the module boundary and the line class,
  so the fix holds whichever layer a caller enters at. Booleans are spelled the way sshd spells
  them: `value: no` unquoted is `False` in YAML, and `False` is not something sshd understands.

- **A write could crash on a config containing non-ASCII.** The read side forced UTF-8; the write
  side used the locale default, which is ASCII on a host under the C locale with PEP 538 coercion
  disabled - measured, `ANSI_X3.4-1968`. A config with a non-ASCII comment therefore read fine and
  raised `UnicodeEncodeError` on write. The suite now also runs under that locale in CI, because
  the test for it is silent under UTF-8.

- **A `config_path` containing a space broke validation.** The candidate file is created beside it,
  and the path was interpolated into the validate command unquoted, so it split into two arguments
  and sshd was handed a directory - refusing a change that was in fact valid. Now quoted; callers
  must not quote C(%s) themselves, which the option documents.

- **Appending a Match block to a file whose last line had no newline.** Terminating the file and
  separating the block are two concerns, and the single `lines[-1].strip()` check conflated them:
  on a whitespace-only unterminated last line it added no separator and glued the header on. The
  file is now terminated first, then separated.

- **The EXAMPLES named a collection that does not exist.** Both examples in
  `plugins/modules/config.py` called `aursu.sshd_setup.config`; the collection is
  `aursu.ssh_setup`. Anyone copying an example got a module-not-found.

- **`build_ignore` never matched the compose file.** It listed `docker-compose.yaml`
  while the file is `docker-compose.yml`, so the compose file shipped in every artifact.
  `.github/`, `.pytest_cache` and `dist` are excluded too.

- **`license_file: ''` alongside `license: [MIT]`.** The two keys are mutually exclusive and
  the empty string is not a path.

- **`requires_ansible: '>=2.9.10'`** named a version no longer supported by the tooling.
  Now `>=2.15.0`.

### Added

- **CI.** `test` renders every module with `ansible-doc`, compiles the plugins, and runs a
  blocking `ansible-lint`. `release` publishes to Galaxy on a `v*` tag, gated by
  `.github/scripts/release_preflight.py`, which refuses a tag that disagrees with `galaxy.yml`
  and a version Galaxy already holds - both of which are unrecoverable once uploaded.

- **`.gitignore`**, which `galaxy.yml` had referenced in `build_ignore` since the beginning
  without one ever existing.
