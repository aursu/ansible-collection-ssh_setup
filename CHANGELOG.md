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

- **`aursu.general` floor raised to `>=1.7.0`** from `>=1.5.0`. The module now imports
  `CUMULATIVE_DIRECTIVES` from the parser and reads the list-typed `value` that 1.7.0 introduced.
  A floor lower than what the code needs is the same class of defect as declaring no dependency.

- **`state: absent` documented as removing every occurrence**, in every file the directive appears
  in. That was already the behaviour; for a cumulative directive it means the whole set goes.

- **The module still does not reload sshd, and now says so.** Writing a file and restarting a
  daemon are different decisions, and only the caller knows whether this is the last of several
  options being set. Notify a handler - `changed` is reported so that works.

### Fixed

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
