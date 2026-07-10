# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""chiplet2dbx -- export a finalized .chiplet assembly to 3Dblox (.3dbv/.3dbx).

Produces the geometric, black-box view of a .chiplet assembly in the 3Dblox
dialect accepted by OpenROAD's ``read_3dbx`` (yaml-cpp based parser), so the
assembly can be linted by ``check_3dblox``. The export is lossy by
declaration: mask artwork, metallurgy, DRC parameters, io_pads and boundary
manifests have no 3Dblox target and are dropped. The mapping follows the
non-normative appendix ``docs/3dblox_interop.md`` of the chiplet-spec
repository; where that appendix and this tool disagree, the appendix governs.

Dies are emitted as black-box ChipletDefs (design_area + thickness + full
outline front/back bond regions, no cell LEF/DEF/verilog), the interposer as
a ``substrate`` def, and each die's connection stack as a Connection whose
thickness is the stack's exact total height. The exporter verifies the
z-consistency rule (die z == mount surface + stack height) and fails loudly
on any mismatch, non-90-degree rotation, ``face_down`` orientation, or
unresolvable connection stack -- it never rounds or guesses silently.

One minimal technology LEF (units only, no layers) is generated per distinct
`.chiplet` technology and referenced as each def's ``APR_tech_file``: the
target ODB requires every chip to carry a technology (``dbBlock::create``
reads the tech's DBU without a null check), and the tech-LEF filename is the
technology identity the loader dedupes on.

Importable entry points:

    load_chiplet(path) -> dict            (vendored chiplet_format_io)
    render_3dbv(assembly) -> str
    render_3dbx(assembly, dbv_filename) -> str
    render_tech_lef(precision) -> str
    export_3dblox(assembly, out_dir, name=None)
        -> (dbv_path, dbx_path, [tech_lef_paths])

New options must be appended as trailing keyword arguments with defaults so
importers stay source-compatible (see docs/integration.md).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ADK_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ADK_ROOT / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

import chiplet_format_io as cfio  # noqa: E402

#: Only micrometer assemblies are supported; 3Dblox coordinates are microns.
SUPPORTED_UNITS = "um"

#: Fallback DBU-per-micron when no technology declares a dbu.
DEFAULT_PRECISION = 1000

#: Tolerance (um) for the z-consistency check. Well below one DBU at any
#: realistic precision, so float noise passes and real mismatches fail.
Z_TOLERANCE = 1e-6

_ROTATION_TOKENS = {0: "R0", 90: "R90", 180: "R180", 270: "R270"}


class ExportError(ValueError):
    """Raised when the assembly cannot be exported faithfully."""


def load_chiplet(path) -> Dict[str, Any]:
    """Load and validate a finalized .chiplet file."""
    return cfio.load(path)


def _fnum(value: float) -> str:
    """Format a micron quantity: up to 6 decimals, no trailing zeros."""
    v = round(float(value), 6)
    if v == 0:
        v = 0.0
    return ("%.6f" % v).rstrip("0").rstrip(".")


def _require_dimension(comp: Dict[str, Any], key: str) -> float:
    dims = comp.get("dimensions") or {}
    value = dims.get(key)
    if value is None:
        raise ExportError(
            "component %r has no dimensions.%s; cannot export"
            % (comp.get("id"), key)
        )
    value = float(value)
    if value <= 0:
        raise ExportError(
            "component %r has non-positive dimensions.%s (%s); for dies the "
            "physical thickness comes from the board's DIE_THICKNESS_UM field"
            % (comp.get("id"), key, value)
        )
    return value


def _position(comp: Dict[str, Any]) -> Tuple[float, float, float]:
    pos = comp.get("position") or {}
    try:
        return float(pos["x"]), float(pos["y"]), float(pos.get("z", 0.0))
    except KeyError as exc:
        raise ExportError(
            "component %r has no position.%s" % (comp.get("id"), exc.args[0])
        ) from None


def _precision(assembly: Dict[str, Any]) -> int:
    """DBU per micron for the Header, derived from the technologies' dbu."""
    dbus = set()
    for name, tech in (assembly.get("technologies") or {}).items():
        if isinstance(tech, dict) and tech.get("dbu"):
            dbus.add(float(tech["dbu"]))
    if not dbus:
        return DEFAULT_PRECISION
    if len(dbus) > 1:
        raise ExportError(
            "technologies declare inconsistent dbu values %s; a single 3Dblox "
            "precision cannot represent them" % sorted(dbus)
        )
    per_um = 1.0 / dbus.pop()
    if abs(per_um - round(per_um)) > 1e-6:
        raise ExportError("technology dbu is not an integer fraction of 1 um")
    return int(round(per_um))


def _rotation_quarter(comp: Dict[str, Any]) -> int:
    """rotation.z normalized to one of 0/90/180/270; loud on anything else."""
    rotation = float((comp.get("rotation") or {}).get("z", 0.0)) % 360.0
    nearest = round(rotation / 90.0) * 90.0
    if abs(rotation - nearest) > 1e-6:
        raise ExportError(
            "component %r has rotation.z %s; 3Dblox only expresses multiples "
            "of 90 degrees" % (comp.get("id"), rotation)
        )
    return int(nearest) % 360


def _orient_token(comp: Dict[str, Any], quarter: int) -> str:
    """Compose .chiplet orientation + rotation quarter into one 3Dblox orient."""
    orientation = comp.get("orientation", "face_up")
    if orientation == "face_up":
        return _ROTATION_TOKENS[quarter]
    if orientation == "flip_chip":
        return "MZ" if quarter == 0 else "MZ_%s" % _ROTATION_TOKENS[quarter]
    if orientation == "face_down":
        raise ExportError(
            "component %r uses orientation face_down, which the interop "
            "mapping deliberately leaves unmapped" % comp.get("id")
        )
    raise ExportError(
        "component %r has unknown orientation %r" % (comp.get("id"), orientation)
    )


def _attach_region(orient_token: str) -> str:
    """Bond region of a die that faces the substrate for a given orient."""
    return "front" if orient_token.startswith("MZ") else "back"


def _stack_total_height(assembly: Dict[str, Any], stack_id: str,
                        component_id: str) -> float:
    stacks = assembly.get("connection_stacks") or {}
    stack = stacks.get(stack_id)
    if not isinstance(stack, dict) or not stack.get("layers"):
        raise ExportError(
            "component %r: connection %r does not resolve to a connection "
            "stack with layers; there is no thickness source"
            % (component_id, stack_id)
        )
    total = 0.0
    for layer in stack["layers"]:
        height = layer.get("height")
        if height is None:
            raise ExportError(
                "connection stack %r has a layer without height" % stack_id
            )
        total += float(height)
    return total


def _technology_of(comp: Dict[str, Any]) -> str:
    technology = comp.get("technology")
    if not technology:
        raise ExportError(
            "component %r has no technology; the export names each "
            "generated technology LEF after it" % comp.get("id")
        )
    return str(technology)


def _split_components(assembly: Dict[str, Any]):
    interposers = []
    dies = []
    for comp in assembly.get("components") or []:
        ctype = comp.get("type")
        if ctype == "interposer":
            interposers.append(comp)
        elif ctype == "die":
            dies.append(comp)
        else:
            raise ExportError(
                "component %r has type %r; the geometric export supports "
                "die and interposer components only" % (comp.get("id"), ctype)
            )
    if len(interposers) != 1:
        raise ExportError(
            "expected exactly one interposer component, found %d"
            % len(interposers)
        )
    return interposers[0], sorted(dies, key=lambda c: str(c.get("id")))


def _build_model(assembly: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the assembly into the def/instance/connection model."""
    units = (assembly.get("assembly") or {}).get("units")
    if units != SUPPORTED_UNITS:
        raise ExportError(
            "assembly.units is %r; only %r is supported" % (units, SUPPORTED_UNITS)
        )

    interposer, dies = _split_components(assembly)

    ipos_w = _require_dimension(interposer, "width")
    ipos_h = _require_dimension(interposer, "height")
    ipos_t = _require_dimension(interposer, "thickness")
    ipos_x, ipos_y, ipos_z = _position(interposer)
    mount_z = ipos_z + ipos_t

    defs: Dict[str, Dict[str, Any]] = {
        str(interposer.get("top_cell") or interposer["id"]): {
            "type": "substrate",
            "width": ipos_w,
            "height": ipos_h,
            "thickness": ipos_t,
            "technology": _technology_of(interposer),
            "key": ("__interposer__",),
        }
    }
    interposer_def = next(iter(defs))

    instances: List[Dict[str, Any]] = [{
        "name": str(interposer["id"]),
        "reference": interposer_def,
        # .chiplet positions are geometric centers; 3Dblox places by corner.
        "loc": (ipos_x - ipos_w / 2.0, ipos_y - ipos_h / 2.0),
        "z": ipos_z,
        "orient": "R0",
    }]
    connections: List[Dict[str, Any]] = []

    for die in dies:
        die_id = str(die["id"])
        width = _require_dimension(die, "width")
        height = _require_dimension(die, "height")
        thickness = _require_dimension(die, "thickness")
        x, y, z = _position(die)
        quarter = _rotation_quarter(die)
        orient = _orient_token(die, quarter)

        key = (
            str(die.get("layout")), str(die.get("top_cell")),
            str(die.get("technology")), width, height, thickness,
        )
        def_name = None
        for name, entry in defs.items():
            if entry["key"] == key:
                def_name = name
                break
        if def_name is None:
            base = str(die.get("top_cell") or die_id)
            def_name = base
            suffix = 2
            while def_name in defs:
                def_name = "%s_%d" % (base, suffix)
                suffix += 1
            defs[def_name] = {
                "type": "die",
                "width": width,
                "height": height,
                "thickness": thickness,
                "technology": _technology_of(die),
                "key": key,
            }

        # A quarter turn swaps the placed footprint's x/y spans; the center
        # stays the .chiplet position, so the corner uses the swapped spans.
        span_x, span_y = (height, width) if quarter in (90, 270) else (width, height)
        instances.append({
            "name": die_id,
            "reference": def_name,
            "loc": (x - span_x / 2.0, y - span_y / 2.0),
            "z": z,
            "orient": orient,
        })

        stack_id = die.get("connection")
        if not stack_id:
            raise ExportError(
                "die %r has no connection stack; the geometric export needs "
                "one to derive the die-to-substrate gap" % die_id
            )
        total = _stack_total_height(assembly, str(stack_id), die_id)
        gap = z - mount_z
        if abs(gap - total) > Z_TOLERANCE:
            raise ExportError(
                "die %r: z gap to the mount surface is %s um but connection "
                "stack %r totals %s um; a .chiplet with explicit position.z "
                "must satisfy z == mount_surface + stack height exactly"
                % (die_id, _fnum(gap), stack_id, _fnum(total))
            )
        connections.append({
            "name": "%s_attach" % die_id,
            "top": "%s.regions.%s" % (die_id, _attach_region(orient)),
            "bot": "%s.regions.front" % instances[0]["name"],
            "thickness": total,
        })

    # Ground the substrate so checkFloatingChips has a reference chip.
    connections.append({
        "name": "%s_mount" % instances[0]["name"],
        "top": "%s.regions.back" % instances[0]["name"],
        "bot": None,
        "thickness": 0.0,
    })

    return {
        "name": str((assembly.get("assembly") or {}).get("name")),
        "precision": _precision(assembly),
        "defs": defs,
        "instances": instances,
        "connections": connections,
    }


def _render_regions(lines: List[str], width: float, height: float) -> None:
    for side in ("front", "back"):
        lines.append("      %s:" % side)
        lines.append("        side: %s" % side)
        lines.append("        coords:")
        for cx, cy in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)):
            lines.append("          - [%s, %s]" % (_fnum(cx), _fnum(cy)))


def _render_header(lines: List[str], precision: int,
                   include: Optional[str] = None) -> None:
    lines.append("Header:")
    lines.append("  version: 3")
    lines.append("  unit: micron")
    lines.append("  precision: %d" % precision)
    if include:
        lines.append("  include:")
        lines.append("    - %s" % include)


def render_3dbv(assembly: Dict[str, Any]) -> str:
    """Render the ChipletDef (.3dbv) view of a finalized assembly dict."""
    model = _build_model(assembly)
    lines: List[str] = [
        "# Derived from %s.chiplet by chiplet2dbx (ADK); regenerate, do not edit."
        % model["name"],
    ]
    _render_header(lines, model["precision"])
    lines.append("")
    lines.append("ChipletDef:")
    for name in sorted(model["defs"]):
        entry = model["defs"][name]
        lines.append("  %s:" % name)
        lines.append("    type: %s" % entry["type"])
        lines.append("    design_area: [%s, %s]"
                     % (_fnum(entry["width"]), _fnum(entry["height"])))
        lines.append("    thickness: %s" % _fnum(entry["thickness"]))
        lines.append("    external:")
        lines.append("      APR_tech_file: [%s.lef]" % entry["technology"])
        lines.append("    regions:")
        _render_regions(lines, entry["width"], entry["height"])
    return "\n".join(lines) + "\n"


def render_tech_lef(precision: int) -> str:
    """Minimal technology LEF: units only, enough for a black-box chip."""
    return (
        "# Derived by chiplet2dbx (ADK); minimal units-only technology LEF.\n"
        "VERSION 5.8 ;\n"
        "BUSBITCHARS \"[]\" ;\n"
        "DIVIDERCHAR \"/\" ;\n"
        "UNITS\n"
        "  DATABASE MICRONS %d ;\n"
        "END UNITS\n"
        "MANUFACTURINGGRID %s ;\n"
        "END LIBRARY\n" % (precision, _fnum(1.0 / precision))
    )


def render_3dbx(assembly: Dict[str, Any], dbv_filename: str) -> str:
    """Render the assembly (.3dbx) view; includes ``dbv_filename``."""
    model = _build_model(assembly)
    lines: List[str] = [
        "# Derived from %s.chiplet by chiplet2dbx (ADK); regenerate, do not edit."
        % model["name"],
    ]
    _render_header(lines, model["precision"], include=dbv_filename)
    lines.append("")
    lines.append("Design:")
    lines.append("  name: \"%s\"" % model["name"])
    lines.append("")
    lines.append("ChipletInst:")
    for inst in model["instances"]:
        lines.append("  %s:" % inst["name"])
        lines.append("    reference: %s" % inst["reference"])
    lines.append("")
    lines.append("Stack:")
    for inst in model["instances"]:
        lines.append("  %s:" % inst["name"])
        lines.append("    loc: [%s, %s]"
                     % (_fnum(inst["loc"][0]), _fnum(inst["loc"][1])))
        lines.append("    z: %s" % _fnum(inst["z"]))
        lines.append("    orient: %s" % inst["orient"])
    lines.append("")
    lines.append("Connection:")
    for conn in model["connections"]:
        lines.append("  %s:" % conn["name"])
        lines.append("    top: %s" % conn["top"])
        lines.append("    bot: %s" % (conn["bot"] if conn["bot"] else "~"))
        lines.append("    thickness: %s" % _fnum(conn["thickness"]))
    return "\n".join(lines) + "\n"


def export_3dblox(assembly: Dict[str, Any], out_dir,
                  name: Optional[str] = None) -> Tuple[Path, Path, List[Path]]:
    """Write ``<name>.3dbv``/``.3dbx`` plus one tech LEF per technology."""
    model = _build_model(assembly)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = name or model["name"]
    dbv_path = out_dir / ("%s.3dbv" % base)
    dbx_path = out_dir / ("%s.3dbx" % base)
    dbv_path.write_text(render_3dbv(assembly), encoding="utf-8")
    dbx_path.write_text(render_3dbx(assembly, dbv_path.name), encoding="utf-8")
    lef_paths = []
    technologies = sorted({d["technology"] for d in model["defs"].values()})
    for technology in technologies:
        lef_path = out_dir / ("%s.lef" % technology)
        lef_path.write_text(render_tech_lef(model["precision"]),
                            encoding="utf-8")
        lef_paths.append(lef_path)
    return dbv_path, dbx_path, lef_paths


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a finalized .chiplet assembly to 3Dblox "
                    "(.3dbv + .3dbx) for OpenROAD read_3dbx / check_3dblox.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python openroad/chiplet2dbx.py --chiplet demo.chiplet\n"
               "  python openroad/chiplet2dbx.py --chiplet demo.chiplet "
               "--out-dir build/3dblox\n",
    )
    parser.add_argument("--chiplet", required=True,
                        help="finalized .chiplet file to export")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: alongside the input)")
    parser.add_argument("--name", default=None,
                        help="basename for the outputs (default: assembly.name)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    chiplet_path = Path(args.chiplet)
    try:
        assembly = load_chiplet(chiplet_path)
        out_dir = Path(args.out_dir) if args.out_dir else chiplet_path.parent
        dbv_path, dbx_path, lef_paths = export_3dblox(
            assembly, out_dir, name=args.name)
    except (cfio.ChipletFormatError, ExportError, OSError) as exc:
        print("chiplet2dbx: error: %s" % exc, file=sys.stderr)
        return 1
    for path in [dbv_path, dbx_path] + lef_paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
