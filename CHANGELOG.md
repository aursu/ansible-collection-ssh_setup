# Changelog

All notable changes to `aursu.ssh_setup` are documented here.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
