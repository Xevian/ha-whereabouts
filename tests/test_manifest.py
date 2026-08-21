"""Manifest consistency checks.

    python tests/test_manifest.py

The version is stored twice — in manifest.json, which HACS reads to decide
whether an update is available, and in const.py, which builds the User-Agent
sent to Nominatim and Overpass. Nothing keeps them in step but this test, and
a silent drift means either HACS never offers the release or the integration
misidentifies itself to two APIs that ask for identification.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_hass import PACKAGE_DIR, load  # noqa: E402

const = load("const")

with open(os.path.join(PACKAGE_DIR, "manifest.json"), encoding="utf-8") as handle:
    MANIFEST = json.load(handle)


def test_version_matches_between_manifest_and_const():
    assert MANIFEST["version"] == const.INTEGRATION_VERSION, (
        f"manifest.json says {MANIFEST['version']} but const.py says "
        f"{const.INTEGRATION_VERSION}"
    )


def test_version_is_semver():
    """HACS refuses to serve a release whose version it cannot parse."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", MANIFEST["version"]), MANIFEST["version"]


def test_user_agent_carries_the_version():
    assert const.USER_AGENT.endswith(const.INTEGRATION_VERSION)


def test_zone_dependency_is_declared():
    """zones.py reads zone.* states, so the dependency belongs in the manifest."""
    assert "zone" in MANIFEST["after_dependencies"]


def test_http_dependency_is_declared():
    """__init__ registers static paths via hass.http, which hassfest enforces.

    http is a hard dependency rather than an after_dependency: the bundled
    map-pin icons cannot be served unless it is set up first.
    """
    assert "http" in MANIFEST["dependencies"]


def test_manifest_keys_are_sorted():
    """hassfest requires domain, name, then alphabetical — and fails the build."""
    keys = list(MANIFEST)
    expected = ["domain", "name"] + sorted(
        k for k in keys if k not in ("domain", "name")
    )
    assert keys == expected, f"expected order {expected}, got {keys}"


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = []
    for name, test in tests:
        try:
            test()
        except AssertionError as err:
            failures.append(name)
            print(f"FAIL  {name}\n      {err}")
        else:
            print(f"pass  {name}")

    print()
    if failures:
        print(f"{len(failures)} of {len(tests)} failed")
        return 1
    print(f"all {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
