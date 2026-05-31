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
from typing import Dict, Optional

import jinja2

ADK_ROOT = Path(__file__).resolve().parents[2]
RULE_PARAMS_JSON = ADK_ROOT / "config" / "rule_params.json"
ADAPTER_DIR = ADK_ROOT / "pdk_adapters" / "interposer"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "assembly_rules.dru.jinja"

# Adapter override pattern: drc_rules['NAME'] = NUMBER
# Whitespace-tolerant; single or double quotes; integer or decimal value.
# Non-numeric Ruby expressions (e.g. referencing other variables) are ignored.
_OVERRIDE_RE = re.compile(
    r"^\s*drc_rules\[\s*['\"](?P<name>[A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]\s*=\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)"
)


def load_defaults(path: Path = RULE_PARAMS_JSON) -> Dict[str, float]:
    """Load ADK rule defaults from the JSON registry."""
    return {name: float(value)
            for name, value in json.loads(path.read_text())["rules"].items()}


def parse_adapter_overrides(adapter_path: Path) -> Dict[str, float]:
    """Extract numeric drc_rules[] overrides from a .drc adapter.

    Only literal numeric overrides are recognised. Anything more complex
    (Ruby expressions referencing other variables, computed values) is
    silently ignored. Comment lines starting with '#' are skipped.
    """
    overrides: Dict[str, float] = {}
    for line in adapter_path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _OVERRIDE_RE.match(line)
        if m:
            overrides[m.group("name")] = float(m.group("value"))
    return overrides


def resolve_adapter_path(name_or_path: str) -> Path:
    """Resolve a shortname (e.g. 'ihp_sg13g2_interposer') or a .drc path."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".drc" and candidate.is_file():
        return candidate.resolve()
    shortname = name_or_path[:-4] if name_or_path.endswith(".drc") else name_or_path
    candidate = ADAPTER_DIR / f"{shortname}.drc"
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(
        f"Interposer adapter not found: '{name_or_path}'. "
        f"Looked for the literal path and for '{ADAPTER_DIR / (shortname + '.drc')}'."
    )


def render_assembly_rules(rules: Dict[str, float],
                          adapter_name: Optional[str] = None,
                          template_dir: Optional[Path] = None) -> str:
    """Render the assembly DRU section.

    Returns a string suitable for writing to a .kicad_dru file or appending
    to a larger generator's output.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(rules=rules, adapter_name=adapter_name)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate the ADK KiCad assembly DRU section.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                              # defaults, stdout
  %(prog)s --interposer-adapter ihp_sg13g2_interposer    # adapter overrides
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

    rule_params_path = (Path(args.rule_params).resolve()
                        if args.rule_params else RULE_PARAMS_JSON)
    rules = load_defaults(rule_params_path)

    adapter_name = None
    if args.interposer_adapter:
        adapter_path = resolve_adapter_path(args.interposer_adapter)
        rules.update(parse_adapter_overrides(adapter_path))
        adapter_name = adapter_path.stem

    rendered = render_assembly_rules(rules, adapter_name=adapter_name)

    if args.out:
        Path(args.out).write_text(rendered)
    else:
        sys.stdout.write(rendered)

    return 0


if __name__ == "__main__":
    exit(main())
