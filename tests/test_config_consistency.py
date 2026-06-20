# SPDX-License-Identifier: Apache-2.0
"""Config registry consistency: version sync + JSON-Schema conformance.

Pure-Python (no klayout CLI), so these run on every CI. They pin (1) the
CHANGELOG's stated invariant that VERSION and the config version keys track the
latest released entry -- the check that would have caught the 0.1.0/0.2.0 skew
this audit fixed -- and (2) each config against its committed schema, so the
shipped schema files (which the umbrella Dockerfile bakes) cannot drift from the
data they describe.
"""
import json
import re
from pathlib import Path

import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ADK_ROOT / "config"
SCHEMA = CONFIG / "schema"


def _latest_released_changelog_version() -> str:
    text = (ADK_ROOT / "CHANGELOG.md").read_text()
    # The first '## X.Y.Z' heading is the latest release (Unreleased has none).
    m = re.search(r"^##\s+(\d+\.\d+\.\d+)\b", text, re.MULTILINE)
    assert m, "No released '## X.Y.Z' heading found in CHANGELOG.md"
    return m.group(1)


def test_version_file_matches_changelog():
    assert (ADK_ROOT / "VERSION").read_text().strip() == \
        _latest_released_changelog_version()


@pytest.mark.parametrize("name", [
    "layers.json", "rule_params.json", "interconnect.json", "chiplet_pads.json",
])
def test_config_version_matches_changelog(name):
    expected = _latest_released_changelog_version()
    data = json.loads((CONFIG / name).read_text())
    assert data["version"] == expected, (
        f"{name} version {data['version']!r} != latest released {expected!r} "
        f"(CHANGELOG.md states VERSION and config version keys must match it)"
    )


@pytest.mark.parametrize("config,schema", [
    ("layers.json", "layers.schema.json"),
    ("rule_params.json", "rule_params.schema.json"),
    ("interconnect.json", "interconnect.schema.json"),
])
def test_config_obeys_its_schema(config, schema):
    jsonschema = pytest.importorskip("jsonschema")
    data = json.loads((CONFIG / config).read_text())
    sch = json.loads((SCHEMA / schema).read_text())
    jsonschema.validate(data, sch)
