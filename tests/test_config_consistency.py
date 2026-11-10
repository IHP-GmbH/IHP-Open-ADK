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


# ---------------------------------------------------------------------------
# Semantic controls. Four schemas under config/schema/ (boundary_manifest,
# interconnect, layers, rule_params) are vendored from chiplet-spec and equal
# its schemas at the commit vendor/README.md names; they are unchanged since
# reader 1.5.0. Two semantics changed on the way
# from the previous copies and this file proves both are live HERE, not only in
# the reference: every key/name pattern ends in (?![\s\S]) instead of $, so a
# value or key with a trailing line break is refused (with $, Python's regex
# engine matches before a trailing newline and the smuggled key passed), and
# layers' gds_layer / gds_datatype cap is the 16-bit stream field, 65535, not
# 255 (the intm4tm2 interposer maps datatypes 300 and 301). Each control also
# checks its own sensitivity by weakening the schema in memory.
# ---------------------------------------------------------------------------

_ANCHOR = r"(?![\s\S])"


def _schema(name):
    return json.loads((SCHEMA / name).read_text())


def _valid(schema, instance):
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(schema).is_valid(instance)


def _weakened(node):
    """The same schema with every end anchor put back to `$`: what the
    previous copies said. Walks the loaded document so patternProperties keys
    and `pattern` values are both rewritten."""
    if isinstance(node, dict):
        return {k.replace(_ANCHOR, "$"): _weakened(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_weakened(x) for x in node]
    if isinstance(node, str):
        return node.replace(_ANCHOR, "$")
    return node


@pytest.mark.parametrize("schema_name, good, smuggled", [
    ("boundary_manifest.schema.json",
     {"schema": "adk-boundary-manifest", "version": "1.0",
      "boundaries": [{"polygon_dbu": [[0, 0], [1, 0], [1, 1]]}]},
     {"schema": "adk-boundary-manifest", "version": "1.0\n",
      "boundaries": [{"polygon_dbu": [[0, 0], [1, 0], [1, 1]]}]}),
    ("interconnect.schema.json",
     {"version": "1.0.0", "rules": {"IXN_spacing": 40.0}},
     {"version": "1.0.0", "rules": {"IXN_spacing\n": 40.0}}),
    ("rule_params.schema.json",
     {"version": "1.0.0", "rules": {"ASM_b": 10.0}},
     {"version": "1.0.0", "rules": {"ASM_b\n": 10.0}}),
    ("layers.schema.json",
     {"version": "1.0.0", "layers": {"metal1": {"gds_layer": 8, "gds_datatype": 0}}},
     {"version": "1.0.0", "layers": {"metal1\n": {"gds_layer": 8, "gds_datatype": 0}}}),
])
def test_schema_anchors_refuse_a_trailing_line_break(schema_name, good, smuggled):
    schema = _schema(schema_name)
    assert _weakened(schema) != schema, f"{schema_name} carries no (?![\\s\\S]) anchor"
    assert _valid(schema, good)
    assert not _valid(schema, smuggled), (
        f"{schema_name} accepted a trailing line break; the vendored copy is "
        f"not chiplet-spec's or the anchor is not live")
    # Sensitivity: with `$` back in place the same instance passes, so the
    # refusal above is the anchor's doing and not some other constraint.
    assert _valid(_weakened(schema), smuggled)


def test_layers_schema_caps_gds_numbers_at_the_16_bit_field():
    schema = _schema("layers.schema.json")

    def layers(layer, datatype):
        return {"version": "1.0.0",
                "layers": {"x": {"gds_layer": layer, "gds_datatype": datatype}}}

    assert _valid(schema, layers(255, 255))       # the old cap still passes
    assert _valid(schema, layers(134, 300))       # intm4tm2's datatypes 300/301
    assert _valid(schema, layers(65535, 65535))   # the field width
    assert not _valid(schema, layers(65536, 0))
    assert not _valid(schema, layers(0, 65536))
    assert not _valid(schema, layers(-1, 0))
    # Sensitivity: the previous 255 cap would refuse the interposer datatype.
    old = json.loads(json.dumps(schema).replace("65535", "255"))
    assert not _valid(old, layers(134, 300))


def test_shipped_configs_validate_against_the_revendored_schemas():
    """The data files did not change with the schemas; every one of them must
    still validate (layers/rule_params/interconnect are covered above by
    test_config_obeys_its_schema, chiplet_pads.json is added here)."""
    schema = _schema("layers.schema.json")
    assert _valid(schema, json.loads((CONFIG / "chiplet_pads.json").read_text()))
