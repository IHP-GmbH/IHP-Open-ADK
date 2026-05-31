"""
ADK generic chiplet LYP generator.

Reads adk/config/chiplet_pads.json (the canonical pads-only chiplet layer
vocabulary) and renders a minimal KLayout .lyp that shows only metal pads, pad
names, and the die outline. This is the "default LYP" used to view or convert
chiplets from commercial / closed PDK nodes that ship a GDS with no .lyp.

Mirrors adk/kicad/dru/generate_assembly_dru.py: config -> Jinja -> concrete
artifact, byte-stable for golden tests. render_generic_lyp() is the entry point
for downstream consumers.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import jinja2

ADK_ROOT = Path(__file__).resolve().parents[2]
CHIPLET_PADS_JSON = ADK_ROOT / "config" / "chiplet_pads.json"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "generic_layers.lyp.jinja"

# Presentation (display name + KLayout style) per abstract layer. Layer NUMBERS
# come from config/chiplet_pads.json; only how they look lives here. Dither and
# line styles are KLayout built-ins (I*/C0) so the minimal .lyp needs no custom
# pattern definitions. Render order: outline first (bottom), names last (top).
_STYLE = {
    "outline":     {"name": "outline.drawing", "color": "#ff3030", "dither": "I1", "width": 2},
    "pad_drawing": {"name": "pad.drawing",     "color": "#d9a521", "dither": "I0", "width": 1},
    "pad_text":    {"name": "pad.text",        "color": "#ffffff", "dither": "I1", "width": 1},
}
_ORDER = ["outline", "pad_drawing", "pad_text"]


def load_layers(path: Path = CHIPLET_PADS_JSON) -> List[Dict]:
    """Build the styled layer list for the template from the JSON vocabulary.

    Layer numbers come from chiplet_pads.json; presentation from _STYLE.
    """
    vocab = json.loads(path.read_text())["layers"]
    layers: List[Dict] = []
    for key in _ORDER:
        entry = vocab.get(key)
        style = _STYLE.get(key)
        if entry is None or style is None:
            continue
        layers.append({
            "name": style["name"],
            "source": f"{entry['gds_layer']}/{entry['gds_datatype']}",
            "frame_color": style["color"],
            "fill_color": style["color"],
            "dither": style["dither"],
            "width": style["width"],
        })
    return layers


def render_generic_lyp(layers: List[Dict],
                       template_dir: Optional[Path] = None) -> str:
    """Render the generic chiplet .lyp from a styled layer list."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(layers=layers)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate the ADK generic chiplet KLayout .lyp.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                   # render to stdout
  %(prog)s --out klayout/lyp/generic.lyp     # write the canonical artifact
""",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output path. If omitted, write to stdout.",
    )
    parser.add_argument(
        "--config", default=None,
        help="Alternate chiplet_pads.json path "
             "(default: adk/config/chiplet_pads.json).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config_path = Path(args.config).resolve() if args.config else CHIPLET_PADS_JSON
    rendered = render_generic_lyp(load_layers(config_path))
    if args.out:
        Path(args.out).write_text(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
