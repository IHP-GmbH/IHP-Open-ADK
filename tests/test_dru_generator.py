# SPDX-License-Identifier: Apache-2.0
"""ADK KiCad DRU generator tests.

Byte-diff against a golden DRU; coverage of CLI, adapter override parsing,
and adapter-applied rendering.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
DRU_DIR = ADK_ROOT / "kicad" / "dru"
GENERATOR = DRU_DIR / "generate_assembly_dru.py"
GOLDEN = Path(__file__).resolve().parent / "golden" / "assembly_rules.dru.golden"

sys.path.insert(0, str(DRU_DIR))
from generate_assembly_dru import (  # noqa: E402
    _format_number,
    load_defaults,
    load_interconnect_defaults,
    parse_adapter_overrides,
    parse_interconnect_overrides,
    render_assembly_rules,
    resolve_adapter_path,
    resolve_interconnect_adapter_path,
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
    adapter = resolve_adapter_path("intm4tm2")
    assert parse_adapter_overrides(adapter) == {}


def test_stale_adapter_ids_do_not_resolve():
    """'intm4tm2' is the only id for the IHP interposer adapter. The
    pre-rename spellings must NOT resolve: no alias map, no fallback."""
    assert resolve_adapter_path("intm4tm2").name == "intm4tm2.drc"
    for stale in ("ihp_intm4tm2", "ihp_sg13g2_interposer"):
        with pytest.raises(FileNotFoundError):
            resolve_adapter_path(stale)


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


# ---------------------------------------------------------------------------
# Interconnect axis (additive; the default render must stay byte-identical)
# ---------------------------------------------------------------------------

def test_default_render_unaffected_by_interconnect_kwargs():
    """Passing interconnect_adapter_name=None keeps the golden byte-identical."""
    rules = load_defaults()
    rendered = render_assembly_rules(rules, interconnect_adapter_name=None)
    assert rendered.encode() == _golden_bytes()


def test_interconnect_adapter_adds_provenance_comment():
    """With an interconnect adapter the ASM.b rule survives and a provenance
    comment for the IXN axis is appended (no new constraint)."""
    rules = load_defaults()
    rendered = render_assembly_rules(
        rules, interconnect_adapter_name="ihp_cupillar")
    assert "courtyard_clearance (min 50um)" in rendered
    assert "Interconnect axis: ihp_cupillar" in rendered
    # IXN rules are post-layout only -> no courtyard constraint emitted for them.
    assert rendered.count("courtyard_clearance") == 1


def test_ihp_cupillar_interconnect_overrides_parsed():
    # Table 6.1 Option 1 -- must mirror the interposer stage's defaults
    # (interposer_tech_default.json Padc_a/Padc_b/Padc_e = 35/40/75).
    adapter = resolve_interconnect_adapter_path("ihp_cupillar")
    overrides = parse_interconnect_overrides(adapter)
    assert overrides == {"IXN_spacing": 40.0, "IXN_pitch": 75.0, "IXN_pad_size": 35.0}


def test_interconnect_defaults_load():
    defaults = load_interconnect_defaults()
    assert defaults["IXN_spacing"] == 40.0
    assert defaults["IXN_pitch"] == 75.0
    assert defaults["IXN_pad_size"] == 35.0


# ---------------------------------------------------------------------------
# Override-parser robustness and the fixed-point number filter
# ---------------------------------------------------------------------------

def test_format_number_avoids_scientific_notation():
    # %g would emit '1.5e+06' (unparseable by KiCad); the filter stays decimal
    # and trims trailing zeros, leaving small integers golden-identical.
    assert _format_number(50.0) == "50"
    assert _format_number(30.5) == "30.5"
    assert _format_number(1500000.0) == "1500000"


def test_override_ignores_non_literal_rhs(tmp_path):
    # Per the documented 'only literal numeric overrides' scope, a non-literal
    # RHS must be IGNORED, never truncated to a misleading partial number.
    adapter = tmp_path / "weird.drc"
    adapter.write_text(
        "drc_rules['ASM_b'] = 30.0e3\n"      # scientific notation
        "drc_rules['ASM_e'] = 1_000\n"       # ruby underscore grouping
        "drc_rules['ASM_b'] = 30.0 + foo\n"  # computed expression
        "drc_rules['asm_b'] = 25.0\n"        # lowercase: schema-invalid key
    )
    assert parse_adapter_overrides(adapter) == {}


def test_override_literal_with_comment_parsed(tmp_path):
    adapter = tmp_path / "ok.drc"
    adapter.write_text("drc_rules['ASM_b'] = 40.0   # tightened\n")
    assert parse_adapter_overrides(adapter) == {"ASM_b": 40.0}


def test_main_rejects_unknown_override_key(tmp_path):
    adapter = tmp_path / "bad_key.drc"
    adapter.write_text("drc_rules['ASM_z'] = 10.0\n")
    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--interposer-adapter", str(adapter)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "unknown rule key" in proc.stderr and "ASM_z" in proc.stderr


def test_main_rejects_negative_override(tmp_path):
    adapter = tmp_path / "neg.drc"
    adapter.write_text("drc_rules['ASM_b'] = -50\n")
    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--interposer-adapter", str(adapter)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "non-negative" in proc.stderr


def test_main_missing_rule_params_clean_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--rule-params",
         str(tmp_path / "nope.json")],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert proc.stderr.startswith("error:")


def test_ihp_sbump_overrides_parsed():
    # The third shipped interconnect adapter (solder bump) previously had no
    # coverage; a typo'd key/value would have shipped undetected.
    adapter = resolve_interconnect_adapter_path("ihp_sbump")
    assert parse_interconnect_overrides(adapter) == {
        "IXN_spacing": 70.0, "IXN_pitch": 130.0, "IXN_pad_size": 60.0}
