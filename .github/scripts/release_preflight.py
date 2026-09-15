#!/usr/bin/env python3
"""Pre-flight guards for publishing an Ansible collection to Galaxy.

Two properties of Galaxy make a bad publish permanent rather than merely annoying:
it takes the version from ``galaxy.yml`` and ignores the git tag entirely, and it
refuses a version it already holds. So a tag that does not match ``galaxy.yml``
publishes the wrong version under the right name, and the tag cannot be re-pointed
at a corrected artifact afterwards - only abandoned.

Both failures are cheap to detect before the build and impossible to repair after
the upload, which is the whole argument for checking here.

Run it in CI, and by hand before tagging::

    python .github/scripts/release_preflight.py --tag v1.7.0

Without ``--tag`` it falls back to ``GITHUB_REF_NAME``; with neither, the tag
check is skipped and only the version itself is checked.

Exit codes: ``0`` clean, ``1`` a guard refused the release, ``2`` the check could
not run (bad arguments, unreadable or malformed ``galaxy.yml``).
"""

import argparse
import os
import sys
import urllib.error
import urllib.request

VERSION_URL = (
    "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published"
    "/collections/index/{namespace}/{name}/versions/{version}/"
)

ON_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CANNOT_RUN = 2


def _emit(level, message):
    """Write a message as a workflow annotation on Actions, plain text elsewhere.

    The script is meant to be run by hand before tagging as well as in CI, and
    ``::error::`` lines are noise in a terminal.
    """
    stream = sys.stdout if level == "notice" else sys.stderr
    if ON_ACTIONS and level in ("error", "warning"):
        stream.write("::%s::%s\n" % (level, message))
    else:
        prefix = {"error": "ERROR: ", "warning": "WARNING: "}.get(level, "")
        stream.write("%s%s\n" % (prefix, message))
    stream.flush()


def error(message):
    _emit("error", message)


def warn(message):
    _emit("warning", message)


def info(message):
    _emit("notice", message)


def load_galaxy_metadata(path):
    """Return (namespace, name, version) from galaxy.yml.

    Parsed as YAML rather than matched with a regex: ``version: "1.7.0"`` is valid
    and quite common, and a pattern that reads the quotes as part of the value
    compares unequal to every tag and fails a release that was correct.
    """
    try:
        import yaml
    except ImportError:
        error("PyYAML is required: pip install ansible-core")
        raise SystemExit(EXIT_CANNOT_RUN)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (IOError, OSError) as exc:
        error("Could not read %s: %s" % (path, exc))
        raise SystemExit(EXIT_CANNOT_RUN)
    except yaml.YAMLError as exc:
        error("%s is not valid YAML: %s" % (path, exc))
        raise SystemExit(EXIT_CANNOT_RUN)

    if not isinstance(data, dict):
        error("%s does not contain a mapping." % path)
        raise SystemExit(EXIT_CANNOT_RUN)

    missing = [key for key in ("namespace", "name", "version") if not data.get(key)]
    if missing:
        error("%s is missing required key(s): %s" % (path, ", ".join(missing)))
        raise SystemExit(EXIT_CANNOT_RUN)

    # A YAML scalar like 1.7 loads as a float and 1 as an int. Both are invalid
    # versions, and str() on them would quietly produce a plausible string.
    version = data["version"]
    if not isinstance(version, str):
        error(
            "version in %s is %s (%r), not a string. Quote it: version: \"%s\"."
            % (path, type(version).__name__, version, version)
        )
        raise SystemExit(EXIT_CANNOT_RUN)

    return str(data["namespace"]), str(data["name"]), version


def check_semver(version):
    """Refuse a version Galaxy's importer would reject after the upload.

    ``ansible-galaxy collection build`` does not check this - it will happily
    produce ``aursu-probe-not-a-version.tar.gz`` - so the rejection lands at
    import, which is after the upload and therefore after the tag is spent.

    Validated with ansible-core's own ``SemanticVersion`` rather than a local
    pattern: it is the class Galaxy's dependency resolver uses, so it is by
    definition the same notion of "valid" that will judge the upload.
    """
    try:
        from ansible.utils.version import SemanticVersion
    except ImportError:
        error("ansible-core is required: pip install ansible-core")
        raise SystemExit(EXIT_CANNOT_RUN)

    # SemanticVersion("") constructs an empty object instead of raising, so the
    # empty case is checked here rather than relying on the exception.
    if version:
        try:
            SemanticVersion(version)
            info("Version %s is valid semver." % version)
            return True
        except ValueError:
            pass

    error(
        "Version %r in galaxy.yml is not valid semantic versioning. "
        "Galaxy rejects it at import, after the tag is already spent." % version
    )
    return False


def check_tag_matches(tag, version):
    """Refuse a tag that names a different version than galaxy.yml declares.

    A leading ``v`` is stripped, so both ``v1.7.0`` and ``1.7.0`` are accepted.
    """
    if tag is None:
        info("No tag given and GITHUB_REF_NAME is unset; skipping the tag check.")
        return True

    stripped = tag[1:] if tag.startswith("v") else tag
    info("tag=%s (version %s) galaxy.yml=%s" % (tag, stripped, version))
    if stripped == version:
        return True

    error("Tag %s does not match the galaxy.yml version %s." % (tag, version))
    error("Bump galaxy.yml and re-tag, or tag v%s." % version)
    return False


def galaxy_status(namespace, name, version, timeout):
    """Return Galaxy's HTTP status for this exact version, or None if unreachable."""
    url = VERSION_URL.format(namespace=namespace, name=name, version=version)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        # 404 is the answer we want and arrives as an exception, not a result.
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        warn("Could not reach Galaxy (%s)." % exc)
        return None


def check_not_published(namespace, name, version, timeout):
    """Refuse a version Galaxy already holds.

    This is not hypothetical. 1.5.0 was published on 2026-02-04 from a working
    tree whose galaxy.yml bump was never committed, so the repository claimed
    1.4.0 for seven months while Galaxy served 1.5.0. The next release attempt
    collided, and the only remedy was to renumber.
    """
    code = galaxy_status(namespace, name, version, timeout)

    if code == 404:
        info("Galaxy does not hold %s.%s %s yet." % (namespace, name, version))
        return True

    if code == 200:
        error("%s.%s %s is already published on Galaxy." % (namespace, name, version))
        error("Galaxy will not accept it again. Bump galaxy.yml and re-tag.")
        return False

    # Anything else is Galaxy being unreachable or unhappy, which is not evidence
    # that the version is taken. Refusing here would block releases on someone
    # else's outage; the publish step fails loudly enough if the version is gone.
    warn(
        "Could not confirm whether %s.%s %s is published (HTTP %s); continuing."
        % (namespace, name, version, code)
    )
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Refuse a collection release that Galaxy cannot undo."
    )
    parser.add_argument(
        "--galaxy-yml",
        default="galaxy.yml",
        help="Path to galaxy.yml (default: %(default)s).",
    )
    parser.add_argument(
        "--tag",
        default=os.environ.get("GITHUB_REF_NAME"),
        help="Release tag to check against galaxy.yml. Defaults to GITHUB_REF_NAME.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for Galaxy (default: %(default)s).",
    )
    parser.add_argument(
        "--skip-galaxy",
        action="store_true",
        help="Skip the already-published check; do not reach the network.",
    )
    args = parser.parse_args(argv)

    namespace, name, version = load_galaxy_metadata(args.galaxy_yml)
    info("Collection %s.%s version %s" % (namespace, name, version))

    # Run every check rather than stopping at the first, so one run reports all
    # the reasons the release is refused instead of one per attempt.
    passed = [check_semver(version), check_tag_matches(args.tag, version)]
    if args.skip_galaxy:
        info("Skipping the Galaxy already-published check as requested.")
    else:
        passed.append(check_not_published(namespace, name, version, args.timeout))

    if all(passed):
        info("Pre-flight passed; %s.%s %s is safe to publish." % (namespace, name, version))
        return EXIT_OK
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
