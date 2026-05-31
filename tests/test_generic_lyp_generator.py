"""ADK generic chiplet LYP generator tests.

Byte-diff against a golden .lyp; checks that rendered layer numbers match
config/chiplet_pads.json and that the vocabulary obeys the layer-registry
schema constraints.
"""

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ADK_ROOT = Path(__file__).resolve().parents[1]
LYP_DIR = ADK_ROOT / "klayout" / "lyp"
GENERATOR = LYP_DIR / "generate_generic_lyp.py"
GOLDEN = Path(__file__).resolve().parent / "golden" / "generic_layers.lyp.golden"
CHIPLET_PADS_JSON = ADK_ROOT / "config" / "chiplet_pads.json"

sys.path.insert(0, str(LYP_DIR))
from generate_generic_lyp import (  # noqa: E402
    load_layers,
    render_generic_lyp,
)

# Display name in the .lyp -> abstract key in chiplet_pads.json.
_NAME_TO_KEY = {
    "outline.drawing": "outline",
    "pad.drawing": "pad_drawing",
    "pad.text": "pad_text",
}


def _golden_bytes() -> bytes:
    return GOLDEN.read_bytes()


def test_render_matches_golden():
    rendered = render_generic_lyp(load_layers())
    assert rendered.encode() == _golden_bytes(), (
        "Generator output diverged from golden. If intentional, regenerate:\n"
        f"  python {GENERATOR} --out {GOLDEN}\n\nActual output:\n{rendered}"
    )


def test_cli_stdout_matches_golden():
    proc = subprocess.run(
        [sys.executable, str(GENERATOR)],
        capture_output=True, check=True,
    )
    assert proc.stdout == _golden_bytes()


def test_cli_writes_file(tmp_path):
    out = tmp_path / "generic.lyp"
    subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(out)],
        check=True,
    )
    assert out.read_bytes() == _golden_bytes()


def test_lyp_is_well_formed_xml():
    ET.fromstring(_golden_bytes())


def test_lyp_numbers_match_config():
    """Every rendered <source> must equal the layer/datatype in the JSON
    vocabulary -- the .lyp is a pure function of chiplet_pads.json."""
    vocab = json.loads(CHIPLET_PADS_JSON.read_text())["layers"]
    root = ET.fromstring(_golden_bytes())
    rendered = {p.find("name").text: p.find("source").text
                for p in root.findall(".//properties")}
    assert set(rendered) == set(_NAME_TO_KEY)
    for name, source in rendered.items():
        entry = vocab[_NAME_TO_KEY[name]]
        assert source == f"{entry['gds_layer']}/{entry['gds_datatype']}"


def test_chiplet_pads_obeys_registry_schema():
    """Lightweight guard mirroring config/schema/layers.schema.json so the
    vocabulary stays valid without requiring the jsonschema package."""
    data = json.loads(CHIPLET_PADS_JSON.read_text())
    assert "version" in data and "layers" in data
    name_re = re.compile(r"^[a-z][a-z0-9_]*$")
    for name, entry in data["layers"].items():
        assert name_re.match(name), name
        assert set(entry) <= {"gds_layer", "gds_datatype", "purpose"}
        assert 0 <= entry["gds_layer"] <= 255
        assert 0 <= entry["gds_datatype"] <= 255
