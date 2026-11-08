# SPDX-License-Identifier: Apache-2.0
"""Validate the ADK chiplet pad vocabulary registry.

config/chiplet_pads.json is the single source of truth for the canonical
black-box chiplet layers (pad_drawing / pad_text / outline). The KLayout .lyp
that mirrors it is hand-maintained with its consumer
(gds_to_kicad/pdks/generic.lyp); a drift test there pins the two together.
"""

import json
import re
from pathlib import Path

ADK_ROOT = Path(__file__).resolve().parents[1]
CHIPLET_PADS_JSON = ADK_ROOT / "config" / "chiplet_pads.json"


def test_chiplet_pads_obeys_registry_schema():
    """Same constraints as config/schema/layers.schema.json (as vendored with
    reader 1.21.0: names anchored with (?![\\s\\S]) so a trailing line break is
    refused, gds numbers capped at the 16-bit stream field), checked without
    requiring the jsonschema package."""
    data = json.loads(CHIPLET_PADS_JSON.read_text())
    assert "version" in data and "layers" in data
    name_re = re.compile(r"^[a-z][a-z0-9_]*(?![\s\S])")
    for name, entry in data["layers"].items():
        assert name_re.match(name), name
        assert set(entry) <= {"gds_layer", "gds_datatype", "purpose"}
        assert 0 <= entry["gds_layer"] <= 65535
        assert 0 <= entry["gds_datatype"] <= 65535


def test_canonical_layers_present():
    layers = json.loads(CHIPLET_PADS_JSON.read_text())["layers"]
    assert {"pad_drawing", "pad_text", "outline"} <= set(layers)
