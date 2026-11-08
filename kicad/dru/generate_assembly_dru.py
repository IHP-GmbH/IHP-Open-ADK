# SPDX-License-Identifier: Apache-2.0
"""
ADK KiCad assembly DRU generator.

Reads adk/config/rule_params.json for defaults, optionally merges per-interposer
numeric overrides parsed from an adapter .drc file, and renders the assembly
DRU section to stdout or to a file.

Scope: the only KLayout rule with a clean KiCad DRU mapping is ASM.b
(courtyard_clearance between die footprints). ASM.a is covered by KiCad's
native courtyard collision check; ASM.e/ASM.f/ASM.g are post-layout only (see
template header).

The render_assembly_rules() function is the entry point for downstream
generators (e.g. an interposer PDK's KiCad DRU generator that wants to append
ADK rules to its own metal/via output).
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, Optional

import jinja2

ADK_ROOT = Path(__file__).resolve().parents[2]

# The ADK root holds adk_registry, the single anchored id->location resolver,
# and adk_rule_params, the rule table and override parsers. Bootstrap it onto
# sys.path so this generator works when invoked by absolute path (the normal
# case) as well as when imported by a downstream generator.
if str(ADK_ROOT) not in sys.path:
    sys.path.insert(0, str(ADK_ROOT))

import adk_registry  # noqa: E402
from adk_rule_params import (  # noqa: E402
    RULE_PARAMS_RELPATH,
    parse_adapter_overrides,
    parse_interconnect_overrides,
    read_rule_table,
)

RULE_PARAMS_JSON = ADK_ROOT / RULE_PARAMS_RELPATH
INTERCONNECT_JSON = ADK_ROOT / "config" / "interconnect.json"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "assembly_rules.dru.jinja"


def _format_number(value: float) -> str:
    """Render a number as a fixed-point decimal, to 6 places, with no
    scientific notation and no trailing zeros: 50.0 -> '50', 30.5 -> '30.5',
    1.5e6 -> '1500000'. KiCad's DRU length-literal parser cannot read
    scientific notation."""
    s = format(float(value), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _format_mm(value_um: float) -> str:
    """Render a length given in um as a KiCad DRU literal in mm: 200 ->
    '0.2mm'. KiCad (9.99 in the ADK image) applies no clearance written in um
    or nm and reports no error, so every length goes out in mm, to 1 nm. A
    nonzero length that rounds to 0 is refused rather than written as no
    clearance."""
    if not math.isfinite(value_um):
        raise ValueError(f"{value_um} um is not a length")
    mm = _format_number(value_um / 1000.0)
    if value_um and mm in ("0", "-0"):
        raise ValueError(f"{value_um} um rounds to 0 at the 1 nm DRU resolution")
    return mm + "mm"


def load_defaults(path: Path = RULE_PARAMS_JSON) -> Dict[str, float]:
    """Load ADK rule defaults from the JSON registry (see adk_rule_params)."""
    return read_rule_table(path)


def load_interconnect_defaults(path: Path = INTERCONNECT_JSON) -> Dict[str, float]:
    """Load ADK interconnect-axis defaults. Returns {} if the registry is absent."""
    if not path.is_file():
        return {}
    return {name: float(value)
            for name, value in json.loads(path.read_text()).get("rules", {}).items()}


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


# Adapter selection is code selection (the same file the KLayout deck EVALs),
# so the argument is a registry id and nothing else: no directory join, no
# CWD-relative path, no ".drc" branch. Neither resolver builds a Path from its
# argument, so the returned path is by construction one of the vetted registry
# values. Do not reintroduce a search directory here.


def resolve_adapter_path(adapter_id: str) -> Path:
    """Resolve an interposer adapter registry id to its vetted .drc path."""
    adk_registry.validate_adapter_id(adapter_id, "interposer adapter")
    return adk_registry.resolve_adapter(adapter_id).value


def resolve_interconnect_adapter_path(adapter_id: str) -> Path:
    """Resolve an interconnect adapter registry id to its vetted .drc path."""
    adk_registry.validate_adapter_id(adapter_id, "interconnect adapter")
    return adk_registry.resolve_adapter(adapter_id).value


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
    env.filters["mm"] = _format_mm
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
  %(prog)s                                   # defaults, stdout
  %(prog)s --interposer-adapter intm4tm2     # adapter overrides
  %(prog)s --out adk_section.txt             # write the section to a file

The output is a section for a project's .kicad_dru, which starts with a
(version 1) line; KiCad ignores a rules file without one, silently.
""",
    )
    parser.add_argument(
        "--interposer-adapter", default=None,
        help="Interposer adapter: a registry id (see "
             "docs/adapter_contract.md); resolved through the ADK registry "
             "only. A path is refused. Numeric drc_rules[] overrides in the "
             "adapter are merged on top of config/rule_params.json defaults.",
    )
    parser.add_argument(
        "--interconnect-adapter", default=None,
        help="Optional interconnect adapter: a registry id (see "
             "docs/adapter_contract.md); resolved through the ADK registry "
             "only. A path is refused. Adds a provenance comment for the bump "
             "pitch/spacing axis (IXN rules, post-layout only). Omit for "
             "interposer-only output (identical to before this axis existed).",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output path for the section. If omitted, write to stdout.",
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
    # The registry failures come first and keep their own exit codes: an
    # unknown/malformed adapter id (or a path where an id belongs) is 1, an
    # unusable registry is 5, an unreadable registry major is 6. Without this
    # they would escape the tuple below as an uncaught traceback.
    except adk_registry.RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return adk_registry.exit_code_for(e)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError,
            jinja2.TemplateError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
