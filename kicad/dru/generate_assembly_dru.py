# SPDX-License-Identifier: Apache-2.0
"""
ADK KiCad assembly DRU generator.

Reads adk/config/rule_params.json for defaults, optionally merges per-interposer
numeric overrides parsed from an adapter .drc file, and renders the assembly
DRU section to stdout or to a file.

Scope: the only KLayout rule with a clean KiCad DRU mapping is ASM.b
(courtyard_clearance). ASM.a is covered by KiCad's native courtyard collision
check; ASM.e/ASM.f are post-layout only (see template header).

The render_assembly_rules() function is the entry point for downstream
generators (e.g. an interposer PDK's KiCad DRU generator that wants to append
ADK rules to its own metal/via output).
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Pattern

import jinja2

ADK_ROOT = Path(__file__).resolve().parents[2]
RULE_PARAMS_JSON = ADK_ROOT / "config" / "rule_params.json"
INTERCONNECT_JSON = ADK_ROOT / "config" / "interconnect.json"
ADAPTER_DIR = ADK_ROOT / "pdk_adapters" / "interposer"
INTERCONNECT_ADAPTER_DIR = ADK_ROOT / "pdk_adapters" / "interconnect"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "assembly_rules.dru.jinja"

# Adapter override pattern: drc_rules['NAME'] = NUMBER
# The rule NAME matches the config schema (^[A-Z][A-Za-z0-9_]*$). The value
# must be a COMPLETE numeric literal: the trailing `\s*(?:#.*)?$` rejects a
# partial match on a non-literal RHS (scientific notation 30.0e3, Ruby
# underscore grouping 1_000, unit suffixes 30um, or computed expressions
# `30.0 + foo`). Such lines do not match and are silently ignored per the
# documented "only literal numeric overrides" scope -- never truncated to a
# misleading partial number. An optional trailing `# comment` is allowed.
_OVERRIDE_RE = re.compile(
    r"^\s*drc_rules\[\s*['\"](?P<name>[A-Z][A-Za-z0-9_]*)['\"]\s*\]\s*=\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?:#.*)?$"
)

# Same shape as _OVERRIDE_RE but for interconnect adapters' interconnect_rules[].
_INTERCONNECT_OVERRIDE_RE = re.compile(
    r"^\s*interconnect_rules\[\s*['\"](?P<name>[A-Z][A-Za-z0-9_]*)['\"]\s*\]\s*=\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?:#.*)?$"
)


def _format_number(value: float) -> str:
    """Render a rule value as a fixed-point decimal with no scientific notation
    and no trailing zeros: 50.0 -> '50', 30.5 -> '30.5'. Python's '%g' switches
    to scientific notation for magnitudes >= 1e6 (e.g. '1.5e+06'), which KiCad's
    DRU length-literal parser cannot read; this keeps the output decimal for any
    magnitude while leaving small integers byte-identical to the old '%g'."""
    s = format(float(value), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def load_defaults(path: Path = RULE_PARAMS_JSON) -> Dict[str, float]:
    """Load ADK rule defaults from the JSON registry."""
    data = json.loads(path.read_text())
    if "rules" not in data:
        raise ValueError(f"{path}: missing required 'rules' key.")
    return {name: float(value) for name, value in data["rules"].items()}


def load_interconnect_defaults(path: Path = INTERCONNECT_JSON) -> Dict[str, float]:
    """Load ADK interconnect-axis defaults. Returns {} if the registry is absent."""
    if not path.is_file():
        return {}
    return {name: float(value)
            for name, value in json.loads(path.read_text()).get("rules", {}).items()}


def _parse_overrides(adapter_path: Path, pattern: Pattern) -> Dict[str, float]:
    """Extract numeric ``...rules['NAME'] = NUMBER`` overrides from a .drc adapter.

    Only complete numeric literals are recognised (see the regex). Anything more
    complex (Ruby expressions, computed values, non-numeric RHS) does not match
    and is silently ignored. Comment lines starting with '#' are skipped.
    """
    overrides: Dict[str, float] = {}
    for line in adapter_path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = pattern.match(line)
        if m:
            overrides[m.group("name")] = float(m.group("value"))
    return overrides


def parse_adapter_overrides(adapter_path: Path) -> Dict[str, float]:
    """Extract numeric drc_rules[] overrides from a .drc interposer adapter."""
    return _parse_overrides(adapter_path, _OVERRIDE_RE)


def parse_interconnect_overrides(adapter_path: Path) -> Dict[str, float]:
    """Extract numeric interconnect_rules[] overrides from an interconnect adapter."""
    return _parse_overrides(adapter_path, _INTERCONNECT_OVERRIDE_RE)


def _apply_overrides(base: Dict[str, float], overrides: Dict[str, float],
                     source: Path, kind: str) -> None:
    """Merge ``overrides`` into ``base`` after validating them.

    An override key not in ``base`` is a typo (e.g. lowercased 'asm_b'): the
    template would silently emit the default, so fail loudly instead. Negative
    values are rejected (a spacing/area constraint cannot be negative).
    """
    unknown = sorted(set(overrides) - set(base))
    if unknown:
        raise ValueError(
            f"{kind} adapter {source} overrides unknown rule key(s) {unknown}; "
            f"known keys: {sorted(base)}."
        )
    for name, value in overrides.items():
        if value < 0:
            raise ValueError(
                f"{kind} adapter {source} sets {name}={value}; rule values must "
                f"be non-negative."
            )
    base.update(overrides)


def _resolve_adapter_path(name_or_path: str, search_dir: Path, kind: str) -> Path:
    """Resolve a shortname (e.g. 'intm4tm2') or a .drc path against ``search_dir``."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".drc" and candidate.is_file():
        return candidate.resolve()
    shortname = name_or_path[:-4] if name_or_path.endswith(".drc") else name_or_path
    candidate = search_dir / f"{shortname}.drc"
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(
        f"{kind} adapter not found: '{name_or_path}'. "
        f"Looked for the literal path and for '{search_dir / (shortname + '.drc')}'."
    )


def resolve_adapter_path(name_or_path: str) -> Path:
    """Resolve an interposer adapter shortname (e.g. 'intm4tm2') or a .drc path."""
    return _resolve_adapter_path(name_or_path, ADAPTER_DIR, "Interposer")


def resolve_interconnect_adapter_path(name_or_path: str) -> Path:
    """Resolve an interconnect adapter shortname or .drc path."""
    return _resolve_adapter_path(name_or_path, INTERCONNECT_ADAPTER_DIR, "Interconnect")


def render_assembly_rules(rules: Dict[str, float],
                          adapter_name: Optional[str] = None,
                          template_dir: Optional[Path] = None,
                          interconnect_rules: Optional[Dict[str, float]] = None,
                          interconnect_adapter_name: Optional[str] = None) -> str:
    """Render the assembly DRU section.

    Returns a string suitable for writing to a .kicad_dru file or appending
    to a larger generator's output.

    The interconnect_* arguments are appended (kwargs with defaults) so existing
    importers are unaffected. With no interconnect adapter the rendered
    output is byte-identical to before this axis existed: the interconnect block
    is template-guarded on ``interconnect_adapter_name``.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["num"] = _format_number
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        rules=rules,
        adapter_name=adapter_name,
        interconnect_rules=interconnect_rules or {},
        interconnect_adapter_name=interconnect_adapter_name,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate the ADK KiCad assembly DRU section.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                              # defaults, stdout
  %(prog)s --interposer-adapter intm4tm2    # adapter overrides
  %(prog)s --out assembly.kicad_dru                      # write to file
""",
    )
    parser.add_argument(
        "--interposer-adapter", default=None,
        help="Interposer adapter shortname or .drc path. Numeric "
             "drc_rules[] overrides in the adapter are merged on top of "
             "config/rule_params.json defaults.",
    )
    parser.add_argument(
        "--interconnect-adapter", default=None,
        help="Optional interconnect adapter shortname or .drc path. Adds a "
             "provenance comment for the bump pitch/spacing axis (IXN rules, "
             "post-layout only). Omit for interposer-only output (identical to "
             "before this axis existed).",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output path. If omitted, write to stdout.",
    )
    parser.add_argument(
        "--rule-params", default=None,
        help="Alternate rule_params.json path "
             "(default: adk/config/rule_params.json).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        rule_params_path = (Path(args.rule_params).resolve()
                            if args.rule_params else RULE_PARAMS_JSON)
        rules = load_defaults(rule_params_path)

        adapter_name = None
        if args.interposer_adapter:
            adapter_path = resolve_adapter_path(args.interposer_adapter)
            _apply_overrides(rules, parse_adapter_overrides(adapter_path),
                             adapter_path, "Interposer")
            adapter_name = adapter_path.stem

        # The interconnect numbers are loaded and forwarded for the documented
        # append-only render signature, but the current KiCad template emits no
        # IXN constraint (the IXN axis is post-layout/KLayout-only); only the
        # adapter NAME appears, as a provenance comment.
        interconnect_rules = load_interconnect_defaults()
        interconnect_adapter_name = None
        if args.interconnect_adapter:
            ixn_adapter_path = resolve_interconnect_adapter_path(args.interconnect_adapter)
            _apply_overrides(interconnect_rules,
                             parse_interconnect_overrides(ixn_adapter_path),
                             ixn_adapter_path, "Interconnect")
            interconnect_adapter_name = ixn_adapter_path.stem

        rendered = render_assembly_rules(
            rules, adapter_name=adapter_name,
            interconnect_rules=interconnect_rules,
            interconnect_adapter_name=interconnect_adapter_name,
        )

        if args.out:
            Path(args.out).write_text(rendered)
        else:
            sys.stdout.write(rendered)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError,
            jinja2.TemplateError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
