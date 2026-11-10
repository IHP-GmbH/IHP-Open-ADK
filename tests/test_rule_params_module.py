# SPDX-License-Identifier: Apache-2.0
"""adk_rule_params and adk_registry load by file from an ADK root.

A consumer that pins an ADK tree loads these two modules with
importlib.util.spec_from_file_location and never puts the ADK on sys.path, so
neither may import anything beyond the standard library. The import check is
static (every import statement, at any depth), and a probe in an isolated
interpreter (-I, cwd outside the tree) calls every public function.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
if str(ADK_ROOT) not in sys.path:
    sys.path.insert(0, str(ADK_ROOT))

import adk_rule_params as rp  # noqa: E402

INTERCONNECT_IDS = tuple(sorted(
    p.stem for p in (ADK_ROOT / "pdk_adapters" / "interconnect").glob("*.drc")))

_PROBE = r"""
import importlib.util, json, sys
from pathlib import Path

root = Path(sys.argv[1])

def load(name):
    spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

rp = load("adk_rule_params")
reg = load("adk_registry")
reg.validate_adapter_id("intm4tm2", "interposer adapter")
adapter = reg.resolve_adapter("intm4tm2").value
print(json.dumps({
    "rules": rp.load_rule_params(root),
    "table": rp.read_rule_table(root / rp.RULE_PARAMS_RELPATH),
    "adapter": str(adapter),
    "overrides": rp.parse_adapter_overrides(adapter),
    "ixn": {i: rp.parse_interconnect_overrides(reg.resolve_adapter(i).value)
            for i in sys.argv[2:]},
}))
"""


def _imported_roots(path: Path) -> set:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: relative import"
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("name", ["adk_rule_params", "adk_registry"])
def test_module_imports_only_the_standard_library(name):
    roots = _imported_roots(ADK_ROOT / f"{name}.py")
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots


def test_modules_load_by_file_without_the_adk_on_sys_path(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(ADK_ROOT), *INTERCONNECT_IDS],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    raw = json.loads((ADK_ROOT / "config" / "rule_params.json").read_text())["rules"]
    assert out["rules"] == out["table"] == {k: float(v) for k, v in raw.items()}
    assert Path(out["adapter"]) == ADK_ROOT / "pdk_adapters" / "interposer" / "intm4tm2.drc"
    assert isinstance(out["overrides"], dict)
    assert INTERCONNECT_IDS
    for adapter_id in INTERCONNECT_IDS:
        assert set(out["ixn"][adapter_id]) == {"IXN_spacing", "IXN_pitch", "IXN_pad_size"}


def test_read_rule_table_reads_an_alternate_table(tmp_path):
    alt = tmp_path / "rule_params.json"
    alt.write_text(json.dumps({"rules": {"ASM_b": 250}}))
    assert rp.read_rule_table(alt) == {"ASM_b": 250.0}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_read_rule_table_refuses_a_value_that_is_not_finite(tmp_path, value):
    table = tmp_path / "rule_params.json"
    table.write_text('{"rules": {"ASM_b": %s, "ASM_g": 200}}' % value)
    with pytest.raises(ValueError, match="not finite: ASM_b"):
        rp.read_rule_table(table)


def test_read_rule_table_refuses_a_table_without_rules(tmp_path):
    bad = tmp_path / "rule_params.json"
    bad.write_text("{}")
    with pytest.raises(ValueError, match="missing required 'rules' key"):
        rp.read_rule_table(bad)


@pytest.mark.parametrize("line", [
    "drc_rules['ASM_b'] = 2.5e2",
    "drc_rules['ASM_b'] = 1_000",
    "drc_rules['ASM_b'] = 250;",
    "drc_rules['ASM_b'] = +250",
    "drc_rules['ASM_b'] = 0250",
    "x = 1; drc_rules['ASM_b'] = 250",
    "drc_rules.store('ASM_b', 250)",
    "drc_rules.merge!('ASM_g' => 250)",
    "drc_rules['ASM_b'] = 200.0 + 50",
    "x = 1  # see drc_rules",
])
def test_an_override_the_parser_cannot_read_is_refused(tmp_path, line):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text(f"# a comment is ignored\n{line}\n")
    with pytest.raises(rp.UnreadOverrideError) as exc:
        rp.parse_adapter_overrides(adapter)
    assert exc.value.lines == [(2, line)]
    assert "line 2 (" in str(exc.value) and line in str(exc.value)


def test_a_continuation_after_an_override_is_refused(tmp_path):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text("drc_rules['ASM_b'] = 250\n\n  .+(50)\n")
    with pytest.raises(rp.UnreadOverrideError) as exc:
        rp.parse_adapter_overrides(adapter)
    assert exc.value.lines == [(3, ".+(50)")]


def test_a_continuation_after_a_comment_is_refused(tmp_path):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text("drc_rules['ASM_b'] = 250\n# why\n  .+(50)\n")
    with pytest.raises(rp.UnreadOverrideError) as exc:
        rp.parse_adapter_overrides(adapter)
    assert exc.value.lines == [(3, ".+(50)")]


def test_a_leading_dot_chain_elsewhere_is_read(tmp_path):
    # KLayout layer expressions are often chained on new lines; only a line
    # continuing an override can change its value.
    adapter = tmp_path / "adapter.drc"
    adapter.write_text("drc_rules['ASM_b'] = 250\n"
                       "pads = polygons(9, 0)\n  .and(polygons(41, 0))\n")
    assert rp.parse_adapter_overrides(adapter) == {"ASM_b": 250.0}


ADAPTER = rp.parse_adapter_overrides
INTERCONNECT = rp.parse_interconnect_overrides


@pytest.mark.parametrize("parse, text, bad", [
    # Ruby reads a form feed or a lone CR as a space: one line, value 300.
    (ADAPTER, "drc_rules['ASM_b'] = 250\f+ 50\n", [1]),
    (ADAPTER, "drc_rules['ASM_b'] = 250\r+ 50\n", [1]),
    # Ruby keeps the last assignment; any repeat is refused.
    (ADAPTER, "drc_rules['ASM_b'] = 250\ndrc_rules['ASM_b'] = 300\n", [2]),
    # Either table named from the other adapter kind.
    (INTERCONNECT, "interconnect_rules['IXN_pitch'] = 75.0\ndrc_rules['ASM_b'] = 300\n", [2]),
    (ADAPTER, "drc_rules['ASM_b'] = 250\ninterconnect_rules['IXN_pitch'] = 50\n", [2]),
    # Non-ASCII digits and whitespace.
    (ADAPTER, "drc_rules['ASM_b'] = 2\u0665\u0660\n", [1]),
    (ADAPTER, "drc_rules['ASM_b'] =\u00a0250\n", [1]),
    # Mismatched quotes leave a string open in Ruby.
    (ADAPTER, "drc_rules['ASM_b\"] = 250\n#'] = 1; drc_rules['ASM_b'] = 300\n", [1, 2]),
    # A comment-led line inside a string can run code, so any comment naming
    # a table is refused, a commented-out override too.
    (ADAPTER, "# drc_rules['ASM_b'] = 300\n", [1]),
    (ADAPTER, "s = %(\n#{drc_rules['ASM_b'] = 300}\n)\n", [2]),
])
def test_lines_naming_a_table_outside_the_literal_form_are_refused(tmp_path, parse, text, bad):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text(text)
    with pytest.raises(rp.UnreadOverrideError) as exc:
        parse(adapter)
    assert [n for n, _ in exc.value.lines] == bad


@pytest.mark.parametrize("text, bad", [
    # The override is the last target of a multiple assignment, which Ruby
    # sets to nil, also across a comment block or a heredoc.
    ("a,\ndrc_rules['ASM_b'] = 200\n", [2]),
    ("a,\n=begin\n=end\ndrc_rules['ASM_b'] = 200\n", [4]),
    ("x[<<~E],\nbody\nE\ndrc_rules['ASM_b'] = 200\n", [4]),
    ("a, *\ndrc_rules['ASM_b'] = 200\n", [2]),
    # Code Ruby may not run, or reads as text.
    ("s = \"\ndrc_rules['ASM_b'] = 300\n\"\n", [2]),
    ("if false\n  drc_rules['ASM_b'] = 300\nend\n", [2]),
    ("__END__\ndrc_rules['ASM_b'] = 300\n", [2]),
    # Any code at all, once, and every override after it.
    ("drc_rules['ASM_b'] = 200\nx = 1\ndrc_rules['ASM_g'] = 250\n", [3]),
    ("x = 1\ndrc_rules['ASM_b'] = 200\ndrc_rules['ASM_g'] = 250\n", [2, 3]),
])
def test_an_override_below_other_code_is_refused(tmp_path, text, bad):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text(text)
    with pytest.raises(rp.UnreadOverrideError) as exc:
        rp.parse_adapter_overrides(adapter)
    assert [n for n, _ in exc.value.lines] == bad
    assert exc.value.reasons == ["below other code"] * len(bad)


def test_overrides_above_code_are_read(tmp_path):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text("# header\n\ndrc_rules['ASM_b'] = 200 # why,\n"
                       "drc_rules['ASM_g'] = 250\n# more\nx = 1,\n  2\n")
    assert rp.parse_adapter_overrides(adapter) == {"ASM_b": 200.0, "ASM_g": 250.0}


SHIPPED_ADAPTERS = sorted((ADK_ROOT / "pdk_adapters").glob("*/*.drc"))
# Per adapter kind: its parser and the names a shipped adapter overrides.
SHIPPED_KINDS = {
    "interposer": (rp.parse_adapter_overrides, set()),
    "interconnect": (rp.parse_interconnect_overrides,
                     {"IXN_spacing", "IXN_pitch", "IXN_pad_size"}),
}


def test_adapters_of_every_kind_are_shipped():
    assert {p.parent.name for p in SHIPPED_ADAPTERS} == set(SHIPPED_KINDS)


@pytest.mark.parametrize("adapter", SHIPPED_ADAPTERS, ids=lambda p: p.stem)
def test_the_shipped_adapters_are_read(adapter):
    parse, names = SHIPPED_KINDS[adapter.parent.name]
    assert set(parse(adapter)) == names


def test_a_table_in_a_trailing_comment_is_named_as_the_reason(tmp_path):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text("drc_rules['ASM_b'] = 300 # see interconnect_rules\n")
    with pytest.raises(rp.UnreadOverrideError, match=r"line 1 \(comment names a rule table\)"):
        rp.parse_adapter_overrides(adapter)


def test_a_comment_naming_no_table_is_a_comment(tmp_path):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text("# ASM_b was 250 here\ndrc_rules['ASM_b'] = 250 # it's the rule\n")
    assert rp.parse_adapter_overrides(adapter) == {"ASM_b": 250.0}


def test_an_interconnect_override_the_parser_cannot_read_is_refused(tmp_path):
    adapter = tmp_path / "ixn.drc"
    adapter.write_text("interconnect_rules['IXN_pitch'] = 7.5e1\n")
    with pytest.raises(rp.UnreadOverrideError) as exc:
        rp.parse_interconnect_overrides(adapter)
    assert exc.value.table == "interconnect_rules"


@pytest.mark.parametrize("line, value", [
    ("drc_rules['ASM_b'] = 250.0  # the technology's rule", 250.0),
    ("drc_rules['ASM_b'] = 0", 0.0),
    ("drc_rules['ASM_b'] = 0.5", 0.5),
    ("drc_rules['ASM_b'] = -0", 0.0),
    ("drc_rules['ASM_b'] = 250\r", 250.0),
])
def test_a_literal_override_is_read(tmp_path, line, value):
    adapter = tmp_path / "adapter.drc"
    adapter.write_text(line + "\n")
    out = rp.parse_adapter_overrides(adapter)
    assert out == {"ASM_b": value}
    # str() tells 0.0 from -0.0, which == does not.
    assert str(out["ASM_b"]) == str(value)
