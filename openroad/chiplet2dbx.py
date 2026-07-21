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

Optionally, per-die pin lists (the ``*.pins.json`` artifact produced by
gds_to_kicad's footprint_to_pinlist) turn the export bump-aware: each such
die's def gains a ``.bmap`` (bump centers in the def-local frame) plus a
per-method bump macro LEF rendered by the interconnect PDK's generator,
discovered via INTERCONNECT_PDK_ROOT / sibling walk and required loudly when
pins are supplied. Pin coordinates are die-local GDS micrometers (y-up,
origin = GDS origin, the ``gds_origin`` anchor); the def-local position is
``pin + dimensions/2``, mirrored in x for ``flip_chip`` dies because this
ecosystem realizes the flip as a layout mirror while the target's flipped
orientation leaves bump x/y unchanged. Because a bump map is a property of
the ChipletDef (not of the instance), dies sharing artwork but differing in
connection method or net binding split into separate defs. Bump port/net
columns are emitted only when the `.chiplet` ``netlist:`` block binds the
pad name to a net; otherwise ``-`` (the target warns on port-without-net).

Importable entry points:

    load_chiplet(path) -> dict            (vendored chiplet_format_io)
    load_pinlist(path) -> [{"name", "x_um", "y_um"}, ...]
    render_3dbv(assembly, die_pins=None) -> str
    render_3dbx(assembly, dbv_filename, die_pins=None) -> str
    render_tech_lef(precision, route_layer=None) -> str
    export_3dblox(assembly, out_dir, name=None, die_pins=None)
        -> (dbv_path, dbx_path, [extra_paths])

New options must be appended as trailing keyword arguments with defaults so
importers stay source-compatible (see docs/integration.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

#: Routing-layer name used in the generated (synthetic) technology LEFs and
#: passed to the bump LEF generator, so bump pad PORTs resolve in every
#: generated die technology.
ROUTE_LAYER_NAME = "BUMP_ATTACH"

_ROTATION_TOKENS = {0: "R0", 90: "R90", 180: "R180", 270: "R270"}

_NAME_SANITIZER = re.compile(r"[^A-Za-z0-9_]")


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


def _mount_reference(interposer: Dict[str, Any]) -> float:
    """The interposer surface dies mount on, in the assembly z-frame.

    Prefers the component-level ``attachment_surface_z`` (the BEOL-top
    die-attachment plane, decoupled from the physical interposer body). Falls
    back to ``dimensions.thickness`` for legacy .chiplet files, where thickness
    encoded that surface. The 3Dblox substrate is a thin mount plane, so this
    reference is both the substrate def thickness and the die mount z; the
    physical interposer body is intentionally not modelled here.
    """
    asz = interposer.get("attachment_surface_z")
    if asz is None:
        return _require_dimension(interposer, "thickness")
    value = float(asz)
    if value <= 0:
        raise ExportError(
            "interposer %r has non-positive attachment_surface_z (%s)"
            % (interposer.get("id"), value)
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
    rot = comp.get("rotation") or {}
    try:
        rotation = float(rot.get("z", 0.0)) % 360.0
    except (AttributeError, TypeError, ValueError):
        # Malformed rotation (non-dict container, or non-numeric z) surfaces
        # as a clean ExportError ("chiplet2dbx: error: ...", exit 1) instead
        # of a raw traceback. This exporter has no exit-2 tier by design.
        raise ExportError(
            "component %r has a non-numeric/malformed rotation.z"
            % comp.get("id")) from None
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
            "component %r uses orientation face_down, which is not a canonical "
            "orientation token; use flip_chip" % comp.get("id")
        )
    raise ExportError(
        "component %r has unknown orientation %r (expected face_up or "
        "flip_chip)" % (comp.get("id"), orientation)
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


def load_pinlist(path) -> List[Dict[str, Any]]:
    """Load a gds_to_kicad ``*.pins.json`` into [{name, x_um, y_um}, ...].

    Pin coordinates in the artifact are die-local GDS DBU (y-up); they are
    converted to micrometers with the file's own ``dbu_um``.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError("cannot read pin list %s: %s" % (path, exc)) from None
    dbu_um = data.get("dbu_um")
    pins = data.get("pins")
    if not dbu_um or not isinstance(pins, list) or not pins:
        raise ExportError(
            "pin list %s lacks dbu_um or a non-empty pins array" % path)
    dbu_um = float(dbu_um)
    result = []
    for i, pin in enumerate(pins):
        try:
            result.append({
                "name": str(pin["name"]),
                "x_um": float(pin["center_x_dbu"]) * dbu_um,
                "y_um": float(pin["center_y_dbu"]) * dbu_um,
            })
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportError(
                "pin list %s: pin[%d] is malformed (%s)" % (path, i, exc)
            ) from None
    return result


def _die_pin_nets(assembly: Dict[str, Any], die_id: str) -> Dict[str, str]:
    """pin name -> net name for one die, from the optional netlist block."""
    nets = (assembly.get("netlist") or {}).get("nets") or []
    binding: Dict[str, str] = {}
    for net in nets:
        net_name = str(net.get("name") or "")
        if not net_name:
            continue
        for conn in net.get("connections") or []:
            if str(conn.get("component")) == die_id and conn.get("pin"):
                binding[str(conn["pin"])] = net_name
    return binding


def _sanitize(name: str) -> str:
    return _NAME_SANITIZER.sub("_", name) or "_"


def _bump_lef_generator():
    """Import the interconnect PDK's bump LEF generator (env -> walk -> loud)."""
    candidates = []
    env = os.environ.get("INTERCONNECT_PDK_ROOT")
    if env:
        candidates.append(Path(env))
    for base in [ADK_ROOT.parent] + list(ADK_ROOT.parents):
        for repo_name in ("interconnect_pdk", "IHP-Interconnect-IntM4TM2"):
            candidates.append(base / repo_name)
    for root in candidates:
        module_dir = root / "libs.tech" / "openroad" / "python"
        if (module_dir / "bump_lef_generator.py").is_file():
            if str(module_dir) not in sys.path:
                sys.path.insert(0, str(module_dir))
            import bump_lef_generator
            return bump_lef_generator
    raise ExportError(
        "bump-aware export needs the interconnect PDK's bump LEF generator; "
        "set INTERCONNECT_PDK_ROOT or place the interconnect_pdk checkout "
        "next to the adk checkout"
    )


def _die_bump_rows(die: Dict[str, Any], pins: List[Dict[str, Any]],
                   width: float, height: float, flipped: bool,
                   pin_nets: Dict[str, str]) -> List[Dict[str, Any]]:
    """Transform die-local GDS pin centers into def-local bump rows.

    Def-local frame is the outline rectangle (0,0)..(width,height) with the
    die's geometric center at (width/2, height/2). ``flip_chip`` dies are
    pre-mirrored in x: the ecosystem realizes the flip as a layout mirror,
    while the target's flipped orientation keeps bump x/y unchanged.
    """
    rows = []
    used_names: Dict[str, int] = {}
    outside = []
    for pin in pins:
        x = width / 2.0 + (-pin["x_um"] if flipped else pin["x_um"])
        y = height / 2.0 + pin["y_um"]
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            outside.append("%s at (%s, %s)" % (pin["name"], _fnum(x), _fnum(y)))
            continue
        base = _sanitize(pin["name"])
        count = used_names.get(base, 0) + 1
        used_names[base] = count
        name = base if count == 1 else "%s_%d" % (base, count)
        net = pin_nets.get(pin["name"], "-")
        rows.append({
            "name": name,
            "x": x,
            "y": y,
            "port": pin["name"] if net != "-" else "-",
            "net": net,
        })
    if outside:
        raise ExportError(
            "die %r: %d pin(s) fall outside the dimensions outline "
            "(%s x %s): %s -- the pin list and dimensions disagree"
            % (die.get("id"), len(outside), _fnum(width), _fnum(height),
               "; ".join(outside[:5])))
    return rows


def _technology_of(comp: Dict[str, Any]) -> str:
    technology = comp.get("technology")
    if not technology:
        raise ExportError(
            "component %r has no technology; the export names each "
            "generated technology LEF after it" % comp.get("id")
        )
    return str(technology)


def _check_die_anchor(comp: Dict[str, Any]) -> None:
    """Die bumps are exported die-local plus the die ``position`` (the
    ``gds_origin`` anchor; see the module docstring). A die declaring
    ``bbox_center`` would need its bumps re-centered on the die GDS bbox first,
    which this exporter does not do, so it is a hard error rather than a silent
    bbox-corner misplacement. An absent anchor is also a hard error: the frame
    contract (coord_frame_contract.md 2.2) defaults it to ``bbox_center``
    (unsupported here), so the die must declare ``anchor: gds_origin``
    explicitly (which every gds_to_kicad die and the plugin writer emit)."""
    anchor = comp.get("anchor")
    if anchor == "gds_origin":
        return
    if anchor is None:
        raise ExportError(
            "die %r has no 'anchor' field. The frame contract defaults an "
            "absent anchor to bbox_center, which this exporter does not "
            "support; declare anchor: gds_origin explicitly." % comp.get("id"))
    if anchor == "bbox_center":
        raise ExportError(
            "die %r declares anchor bbox_center, which this exporter does not "
            "support: it emits die-local bumps at the gds_origin anchor. "
            "Re-anchor the die to gds_origin." % comp.get("id"))
    raise ExportError(
        "die %r has unknown anchor %r (expected gds_origin or bbox_center)"
        % (comp.get("id"), anchor))


def _split_components(assembly: Dict[str, Any]):
    interposers = []
    dies = []
    for comp in assembly.get("components") or []:
        ctype = comp.get("type")
        if ctype == "interposer":
            interposers.append(comp)
        elif ctype == "die":
            _check_die_anchor(comp)
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


def _bump_macro_name(method_id: str) -> str:
    """Bump macro name convention; must match the interconnect PDK
    generator's bump_macro_name (drift-guarded by a test against the
    discoverable sibling, and by ODB-531 at load time)."""
    return "BUMP_%s" % method_id.upper()


def _build_model(assembly: Dict[str, Any],
                 die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None
                 ) -> Dict[str, Any]:
    """Resolve the assembly into the def/instance/connection model."""
    die_pins = die_pins or {}
    units = (assembly.get("assembly") or {}).get("units")
    if units != SUPPORTED_UNITS:
        raise ExportError(
            "assembly.units is %r; only %r is supported" % (units, SUPPORTED_UNITS)
        )

    interposer, dies = _split_components(assembly)

    ipos_w = _require_dimension(interposer, "width")
    ipos_h = _require_dimension(interposer, "height")
    # The 3Dblox substrate is a THIN mount plane, not the physical interposer
    # body: its def thickness and the die mount surface are both the
    # interposer's die-attachment surface z (attachment_surface_z when the
    # .chiplet declares it, else the legacy dimensions.thickness).
    mount_ref = _mount_reference(interposer)
    ipos_x, ipos_y, ipos_z = _position(interposer)
    mount_z = ipos_z + mount_ref

    defs: Dict[str, Dict[str, Any]] = {
        str(interposer.get("top_cell") or interposer["id"]): {
            "type": "substrate",
            "width": ipos_w,
            "height": ipos_h,
            "thickness": mount_ref,
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

    unknown_pins = set(die_pins) - {str(d.get("id")) for d in dies}
    if unknown_pins:
        raise ExportError(
            "pin lists were supplied for unknown die refs: %s"
            % ", ".join(sorted(unknown_pins)))

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
        bump = None
        if die_id in die_pins:
            # A bump map is a property of the ChipletDef, so dies that share
            # artwork but differ in method or net binding get separate defs.
            method = str(die.get("connection") or "")
            if not method:
                raise ExportError(
                    "die %r has a pin list but no connection method; the "
                    "bump map is keyed by method" % die_id)
            pin_nets = _die_pin_nets(assembly, die_id)
            rows = _die_bump_rows(die, die_pins[die_id], width, height,
                                  orient.startswith("MZ"), pin_nets)
            key = key + (method, tuple(sorted(pin_nets.items())))
            bump = {
                "method": method,
                "cell": _bump_macro_name(method),
                "rows": rows,
            }
        def_name = None
        for name, entry in defs.items():
            if entry["key"] == key:
                if bump and (entry.get("bump") or {}).get("rows") != bump["rows"]:
                    raise ExportError(
                        "dies sharing def %r were given different pin lists"
                        % name)
                def_name = name
                break
        if def_name is None:
            base = str(die.get("top_cell") or die_id)
            if bump:
                base = _sanitize("%s__%s" % (base, bump["method"]))
            def_name = base
            suffix = 2
            while def_name in defs:
                def_name = "%s_%d" % (base, suffix)
                suffix += 1
            technology = _technology_of(die)
            if bump:
                bump["lef"] = _sanitize(
                    "%s__%s" % (bump["method"], technology)) + ".lef"
                bump["bmap"] = "%s.bmap" % def_name
            defs[def_name] = {
                "type": "die",
                "width": width,
                "height": height,
                "thickness": thickness,
                "technology": technology,
                "key": key,
                "bump": bump,
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


def _render_regions(lines: List[str], width: float, height: float,
                    front_bmap: Optional[str] = None) -> None:
    for side in ("front", "back"):
        lines.append("      %s:" % side)
        lines.append("        side: %s" % side)
        if side == "front" and front_bmap:
            lines.append("        bmap: %s" % front_bmap)
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


def render_3dbv(assembly: Dict[str, Any], *,
                die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None
                ) -> str:
    """Render the ChipletDef (.3dbv) view of a finalized assembly dict."""
    model = _build_model(assembly, die_pins)
    lines: List[str] = [
        "# Derived from %s.chiplet by chiplet2dbx (ADK); regenerate, do not edit."
        % model["name"],
    ]
    _render_header(lines, model["precision"])
    lines.append("")
    lines.append("ChipletDef:")
    for name in sorted(model["defs"]):
        entry = model["defs"][name]
        bump = entry.get("bump")
        lines.append("  %s:" % name)
        lines.append("    type: %s" % entry["type"])
        lines.append("    design_area: [%s, %s]"
                     % (_fnum(entry["width"]), _fnum(entry["height"])))
        lines.append("    thickness: %s" % _fnum(entry["thickness"]))
        lines.append("    external:")
        lines.append("      APR_tech_file: [%s.lef]" % entry["technology"])
        if bump:
            lines.append("      LEF_file: [%s]" % bump["lef"])
        lines.append("    regions:")
        _render_regions(lines, entry["width"], entry["height"],
                        front_bmap=bump["bmap"] if bump else None)
    return "\n".join(lines) + "\n"


def render_bmap(assembly: Dict[str, Any], def_name: str, *,
                die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None
                ) -> str:
    """Render the bump map of one bump-aware ChipletDef."""
    model = _build_model(assembly, die_pins)
    entry = model["defs"].get(def_name)
    if entry is None or not entry.get("bump"):
        raise ExportError("def %r has no bump map" % def_name)
    bump = entry["bump"]
    lines = [
        "# Bump map for ChipletDef %s (method %s); derived from %s.chiplet "
        "by chiplet2dbx (ADK)." % (def_name, bump["method"], model["name"]),
        "# bumpInstName bumpCellType x y portName netName  (x/y = bump "
        "center, def-local um)",
    ]
    for row in bump["rows"]:
        lines.append("%s %s %s %s %s %s" % (
            row["name"], bump["cell"], _fnum(row["x"]), _fnum(row["y"]),
            row["port"], row["net"]))
    return "\n".join(lines) + "\n"


def render_tech_lef(precision: int, route_layer: Optional[str] = None) -> str:
    """Minimal technology LEF: units only, enough for a black-box chip.

    With ``route_layer`` a single minimal routing layer is added so bump
    macro PORTs referencing it resolve in this technology.
    """
    layer = ""
    if route_layer:
        layer = (
            "LAYER %s\n"
            "  TYPE ROUTING ;\n"
            "  DIRECTION HORIZONTAL ;\n"
            "  PITCH 1 ;\n"
            "  WIDTH 1 ;\n"
            "END %s\n" % (route_layer, route_layer)
        )
    return (
        "# Derived by chiplet2dbx (ADK); minimal units-only technology LEF.\n"
        "VERSION 5.8 ;\n"
        "BUSBITCHARS \"[]\" ;\n"
        "DIVIDERCHAR \"/\" ;\n"
        "UNITS\n"
        "  DATABASE MICRONS %d ;\n"
        "END UNITS\n"
        "MANUFACTURINGGRID %s ;\n"
        "%s"
        "END LIBRARY\n" % (precision, _fnum(1.0 / precision), layer)
    )


def render_3dbx(assembly: Dict[str, Any], dbv_filename: str, *,
                die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None
                ) -> str:
    """Render the assembly (.3dbx) view; includes ``dbv_filename``."""
    model = _build_model(assembly, die_pins)
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
                  name: Optional[str] = None,
                  die_pins: Optional[Dict[str, Any]] = None
                  ) -> Tuple[Path, Path, List[Path]]:
    """Write ``<name>.3dbv``/``.3dbx`` plus one tech LEF per technology.

    ``die_pins`` maps die refs to either a ``*.pins.json`` path or an
    already-parsed pin list; when given, the affected defs also get a
    ``.bmap`` and a per-(method, technology) bump macro LEF rendered by the
    interconnect PDK's generator.
    """
    parsed_pins: Dict[str, List[Dict[str, Any]]] = {}
    for ref, value in (die_pins or {}).items():
        parsed_pins[str(ref)] = (value if isinstance(value, list)
                                 else load_pinlist(value))
    model = _build_model(assembly, parsed_pins)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = name or model["name"]
    dbv_path = out_dir / ("%s.3dbv" % base)
    dbx_path = out_dir / ("%s.3dbx" % base)
    dbv_path.write_text(render_3dbv(assembly, die_pins=parsed_pins),
                        encoding="utf-8")
    dbx_path.write_text(render_3dbx(assembly, dbv_path.name,
                                    die_pins=parsed_pins), encoding="utf-8")
    extra_paths = []

    bump_defs = {n: d for n, d in model["defs"].items() if d.get("bump")}
    generator = _bump_lef_generator() if bump_defs else None
    for def_name in sorted(bump_defs):
        entry = bump_defs[def_name]
        bmap_path = out_dir / entry["bump"]["bmap"]
        bmap_path.write_text(
            render_bmap(assembly, def_name, die_pins=parsed_pins),
            encoding="utf-8")
        extra_paths.append(bmap_path)
    for lef_name, method in sorted({
            (d["bump"]["lef"], d["bump"]["method"])
            for d in bump_defs.values()}):
        lef_path = out_dir / lef_name
        try:
            lef_text = generator.render_bump_lef(method, ROUTE_LAYER_NAME)
        except KeyError as exc:
            raise ExportError(
                "connection method %r is not in the interconnect PDK "
                "manifest, so no bump macro can be generated: %s"
                % (method, exc)) from None
        lef_path.write_text(lef_text, encoding="utf-8")
        extra_paths.append(lef_path)

    route_layer = ROUTE_LAYER_NAME if bump_defs else None
    technologies = sorted({d["technology"] for d in model["defs"].values()})
    for technology in technologies:
        lef_path = out_dir / ("%s.lef" % technology)
        lef_path.write_text(render_tech_lef(model["precision"], route_layer),
                            encoding="utf-8")
        extra_paths.append(lef_path)
    return dbv_path, dbx_path, extra_paths


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a finalized .chiplet assembly to 3Dblox "
                    "(.3dbv + .3dbx) for OpenROAD read_3dbx / check_3dblox.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python openroad/chiplet2dbx.py --chiplet demo.chiplet\n"
               "  python openroad/chiplet2dbx.py --chiplet demo.chiplet "
               "--out-dir build/3dblox \\\n"
               "      --pins U1=chiplets/die_a.pins.json "
               "--pins U2=chiplets/die_b.pins.json\n",
    )
    parser.add_argument("--chiplet", required=True,
                        help="finalized .chiplet file to export")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: alongside the input)")
    parser.add_argument("--name", default=None,
                        help="basename for the outputs (default: assembly.name)")
    parser.add_argument("--pins", action="append", default=[],
                        metavar="REF=PINS_JSON",
                        help="per-die pin list (gds_to_kicad *.pins.json); "
                             "repeatable; enables the bump map for that die")
    return parser.parse_args(argv)


def _parse_pins_args(specs: List[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for spec in specs:
        ref, sep, path = spec.partition("=")
        if not sep or not ref or not path:
            raise ExportError("--pins expects REF=PATH, got %r" % spec)
        if ref in result:
            raise ExportError("--pins given twice for ref %r" % ref)
        result[ref] = path
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    chiplet_path = Path(args.chiplet)
    try:
        assembly = load_chiplet(chiplet_path)
        out_dir = Path(args.out_dir) if args.out_dir else chiplet_path.parent
        dbv_path, dbx_path, extra_paths = export_3dblox(
            assembly, out_dir, name=args.name,
            die_pins=_parse_pins_args(args.pins))
    except (cfio.ChipletFormatError, ExportError, OSError) as exc:
        print("chiplet2dbx: error: %s" % exc, file=sys.stderr)
        return 1
    for path in [dbv_path, dbx_path] + extra_paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
