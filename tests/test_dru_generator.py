"""ADK KiCad DRU generator tests.

Byte-diff against a golden DRU; coverage of CLI, adapter override parsing,
and adapter-applied rendering.
"""

import subprocess
import sys
from pathlib import Path

ADK_ROOT = Path(__file__).resolve().parents[1]
DRU_DIR = ADK_ROOT / "kicad" / "dru"
GENERATOR = DRU_DIR / "generate_assembly_dru.py"
GOLDEN = Path(__file__).resolve().parent / "golden" / "assembly_rules.dru.golden"

sys.path.insert(0, str(DRU_DIR))
from generate_assembly_dru import (  # noqa: E402
    load_defaults,
    parse_adapter_overrides,
    render_assembly_rules,
    resolve_adapter_path,
)


def _golden_bytes() -> bytes:
    return GOLDEN.read_bytes()


def test_render_defaults_matches_golden():
    rules = load_defaults()
    rendered = render_assembly_rules(rules)
    assert rendered.encode() == _golden_bytes(), (
        "Generator output diverged from golden. If intentional, regenerate:\n"
        f"  python {GENERATOR} > {GOLDEN}\n\nActual output:\n{rendered}"
    )


def test_cli_stdout_matches_golden():
    proc = subprocess.run(
        [sys.executable, str(GENERATOR)],
        capture_output=True, check=True,
    )
    assert proc.stdout == _golden_bytes()


def test_cli_writes_file(tmp_path):
    out = tmp_path / "out.kicad_dru"
    subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(out)],
        check=True,
    )
    assert out.read_bytes() == _golden_bytes()


def test_ihp_adapter_has_no_numeric_overrides():
    """The current IHP adapter declares only layer mappings; the parser
    must return {} without crashing on the AND-intersection line."""
    adapter = resolve_adapter_path("ihp_sg13g2_interposer")
    assert parse_adapter_overrides(adapter) == {}


def test_adapter_override_is_applied(tmp_path):
    """An adapter that overrides ASM_b must shift the rendered constraint
    and the header must mention the adapter name."""
    custom = tmp_path / "custom_interposer.drc"
    custom.write_text(
        "# Test adapter with an explicit ASM_b override.\n"
        "chiplet_attachment_input = polygons(999, 0)\n"
        "drc_rules['ASM_b'] = 30.0\n"
    )
    overrides = parse_adapter_overrides(custom)
    assert overrides == {"ASM_b": 30.0}

    rules = load_defaults()
    rules.update(overrides)
    rendered = render_assembly_rules(rules, adapter_name="custom_interposer")
    assert "courtyard_clearance (min 30um)" in rendered
    assert "courtyard_clearance (min 50um)" not in rendered
    assert "adapter overrides from custom_interposer" in rendered


def test_adapter_parser_ignores_commented_overrides(tmp_path):
    custom = tmp_path / "commented.drc"
    custom.write_text(
        "# drc_rules['ASM_b'] = 99.0\n"
        "drc_rules['ASM_e'] = 5000\n"
    )
    overrides = parse_adapter_overrides(custom)
    assert overrides == {"ASM_e": 5000.0}
