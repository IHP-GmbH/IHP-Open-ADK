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
reads the tech's DBU without a null check), and the loader dedupes technology
and LEF libraries by file stem, silently skipping a second file with a known
stem.

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

Optionally, per-die eda-views records (``--views INST=RECORD``, each with the
die contract it is bound to, ``--contract INST=DIR``) export a die with its
recorded views instead of as a black box: its ChipletDef names the record's
technology LEF, cell LEFs, Liberty files at one corner and DEF, by paths
relative to the .3dbv, and its bump map lists the DEF pad instances with their
DEF masters at their centres in the DEF frame, with the die's own port and net
(``- -`` for supply pads). The record is read with the standard library: every
row used is re-hashed, the record must be bound to the named contract (layout,
build record, top cell and pin list), and each pad centre, the DEF units and
the die area are checked against the DEF; a pad must be placed ``N`` on a
whole DBU and its master must have one pin, and a MACRO name must have one
body across every record LEF. Such a die declares ``anchor: bbox_center``, since
the DEF frame is the die outline, and a flipped one is placed ``MZ_MY``: the
target's ``MZ`` mirrors z only, so the flip's x mirror goes into the
orientation and the bump map stays in the DEF frame. A ``--top-verilog``
system top is named in the .3dbx Design block after its module, instances and
ports are checked against the records; this tool never writes Verilog. With
``--views`` the CLI prints, after the written paths, one line per die:
``<ref>: eda views: <kinds>; unqualified: <files>`` or
``<ref>: eda views: absent``.

Importable entry points:

    load_chiplet(path) -> dict            (vendored chiplet_format_io)
    load_pinlist(path) -> [{"name", "x_um", "y_um"}, ...]
    load_views(record, contract_dir, corner=None) -> dict
    render_3dbv(assembly, *, die_pins=None, die_views=None,
                base_dir=None) -> str
    render_3dbx(assembly, dbv_filename, *, die_pins=None, die_views=None,
                top_verilog=None) -> str
    render_tech_lef(precision, route_layer=None) -> str
    export_3dblox(assembly, out_dir, name=None, die_pins=None,
                  die_views=None, corner=None, top_verilog=None)
        -> (dbv_path, dbx_path, [extra_paths])

New options must be appended as trailing keyword arguments with defaults so
importers stay source-compatible (see docs/integration.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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

#: The per-die record read with --views, the die contract it must be bound
#: to, and the pin-list version and frame of the record's pins view. A reader
#: names the format it reads; each of these is checked, never assumed.
VIEWS_SCHEMA = "chiplet-flow-eda-views/v1"
VIEWS_PINS_MAJOR = "1"
VIEWS_PINS_FRAME = "die-local DEF frame, Y up; the flip-chip footprint mirrors X"
CONTRACT_FILE = "die-contract.json"
CONTRACT_SCHEMA = "die-output-contract"
CONTRACT_MAJOR = "1"
#: Record kinds this exporter reads; the first three appear exactly once at
#: the corner, the other two at least once. Every other kind is left alone.
VIEWS_KINDS = ("def", "tech_lef", "pins", "lef", "liberty")
_VIEWS_ONCE = ("def", "tech_lef", "pins")
_VIEWS_ROLES = ("SIGNAL", "POWER", "GROUND")
_VIEWS_STATUSES = ("recorded", "unqualified")

_ROTATION_TOKENS = {0: "R0", 90: "R90", 180: "R180", 270: "R270"}

_NAME_SANITIZER = re.compile(r"[^A-Za-z0-9_]")
#: A path written into the .3dbv as a plain YAML scalar.
_PLAIN_PATH = re.compile(r"[A-Za-z0-9_./+-]+")
#: One item of a generated system top, comments removed: the module header,
#: a wire, a die instance with named connections only, or endmodule.
_TOP_ITEM = re.compile(
    r"\s*(?:(?P<module>module\s+(?P<name>\w+)\s*(?:\([^;]*\))?\s*;)"
    r"|(?P<wire>wire\s+\w+\s*;)"
    r"|(?P<inst>(?P<cell>\w+)\s+(?P<inst_name>\w+)\s*\("
    r"(?P<body>\s*(?:\.\w+\s*\(\s*\w+\s*\)\s*"
    r"(?:,\s*\.\w+\s*\(\s*\w+\s*\)\s*)*)?)\)\s*;)"
    r"|(?P<end>endmodule\b))\s*")


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


def _substrate_thickness(interposer: Dict[str, Any], seats_dies: bool) -> float:
    """The 3Dblox substrate def thickness: the height above the interposer's
    position.z that dies mount at.

    The 3Dblox substrate is a thin mount plane, not the physical interposer
    body. When the interposer seats dies it is the interposer component's
    ``attachment_surface_z`` (the die-attachment plane), and a missing one is
    refused: ``dimensions.thickness`` is the body and does not stand in for
    it. An interposer that seats nothing needs no plane; its def then takes
    the body thickness, unless it declares a plane anyway.
    """
    asz = interposer.get("attachment_surface_z")
    if asz is None:
        if not seats_dies:
            return _require_dimension(interposer, "thickness")
        raise ExportError(
            "interposer %r seats dies but declares no attachment_surface_z; "
            "declare the plane it mounts them on. dimensions.thickness is "
            "the body, not the plane, and is not read in its place"
            % interposer.get("id"))
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: Any, what: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ExportError("%s is not a JSON object" % what)
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExportError("%s is not a non-empty string" % what)
    return value


def _json_file(path: Path, what: str) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError("cannot read %s %s: %s" % (what, path, exc)) from None
    return _mapping(data, "%s %s" % (what, path))


def _file_inside(root: Path, relative: Any, what: str) -> Path:
    """``relative`` as a file under the resolved ``root``: a relative POSIX
    path with no ``..`` that, symlinks resolved, is still a file inside it."""
    if (not isinstance(relative, str) or not relative
            or relative.startswith("/") or "\\" in relative
            or ".." in relative.split("/")):
        raise ExportError("%s names %r, not a relative path inside %s"
                          % (what, relative, root))
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ExportError("%s names %r, which is not a file inside %s"
                          % (what, relative, root))
    return path


def _bound_contract(doc: Dict[str, Any], record: Path, contract_dir,
                    pins_sha: str) -> List[Any]:
    """Refuse unless the record was written for the die contract in
    ``contract_dir``: its layout and build record are the ones the record is
    bound to, its top cell is the record's design and its pin list is the
    bytes the record's pins row binds. Returns the contract's die bbox."""
    cdir = Path(contract_dir).resolve()
    contract = _json_file(cdir / CONTRACT_FILE, "die contract")
    version = contract.get("version")
    if (contract.get("schema") != CONTRACT_SCHEMA or not isinstance(version, str)
            or version.split(".")[0] != CONTRACT_MAJOR):
        raise ExportError("%s is not a %s version %s.x file"
                          % (cdir / CONTRACT_FILE, CONTRACT_SCHEMA, CONTRACT_MAJOR))
    die = _mapping(contract.get("die"), "the die of the contract in %s" % cdir)
    build = _mapping(contract.get("build_record"),
                     "the build record of the contract in %s" % cdir)
    inventory = _mapping(contract.get("inventory"),
                         "the inventory of the contract in %s" % cdir)
    if inventory.get("kind") != "pins_json":
        raise ExportError("die contract %s lists no pins.json" % cdir)
    for entry, what in ((build, "build record"), (inventory, "pin list")):
        path = _file_inside(cdir, entry.get("path"), "the contract's %s" % what)
        if _sha256(path) != entry.get("sha256"):
            raise ExportError("%s is not the bytes the contract in %s binds"
                              % (path.name, cdir))
    bound = _mapping(doc.get("bound_to"), "the bound_to of %s" % record)
    # An absent value on both sides would compare equal, so each is required.
    if (_text(bound.get("layout_sha256"), "the layout digest of %s" % record)
            != _text(die.get("layout_sha256"),
                     "the layout digest of the contract in %s" % cdir)
            or _text(bound.get("build_record_sha256"),
                     "the build record digest of %s" % record)
            != build.get("sha256")):
        raise ExportError("%s is not bound to the layout and build record of %s"
                          % (record, cdir))
    design = _text(doc.get("design"), "the design of %s" % record)
    if design != _text(die.get("top_cell"),
                       "the top cell of the contract in %s" % cdir):
        raise ExportError("%s describes %s, and the contract in %s is %s"
                          % (record, design, cdir, die.get("top_cell")))
    if pins_sha != inventory.get("sha256"):
        raise ExportError("the pin list of %s is not the one %s binds"
                          % (record, cdir))
    return die.get("bbox_um")


def _def_statements(text: str, section: str) -> List[str]:
    """The ``- ...`` statements of one DEF section, whitespace collapsed."""
    match = re.search(r"^\s*%s\s+\d+\s*;(.*?)^\s*END\s+%s\b" % (section, section),
                      text, re.M | re.S)
    if match is None:
        return []
    return [" ".join(part.split()) for part in match.group(1).split(";")
            if part.strip().startswith("-")]


def _def_facts(path: Path) -> Dict[str, Any]:
    """Units, die area, placed components, pin nets and net members of a DEF."""
    text = path.read_text(encoding="utf-8", errors="replace")
    units = re.search(r"^\s*UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text, re.M)
    area = re.search(r"^\s*DIEAREA\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*"
                     r"\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*;", text, re.M)
    if units is None or area is None:
        raise ExportError("DEF %s states no UNITS or no rectangular DIEAREA"
                          % path)
    components = {}
    for stmt in _def_statements(text, "COMPONENTS"):
        m = re.match(r"- (\S+) (\S+) .*?\+ (?:FIXED|PLACED|COVER) "
                     r"\( (-?\d+) (-?\d+) \) (\S+)", stmt)
        if m:
            components[m.group(1)] = (m.group(2), int(m.group(3)),
                                      int(m.group(4)), m.group(5))
    pins = {}
    for stmt in _def_statements(text, "PINS"):
        m = re.match(r"- (\S+) \+ NET (\S+)", stmt)
        if m:
            pins[m.group(1)] = m.group(2)
    members: Dict[str, set] = {}
    for section in ("NETS", "SPECIALNETS"):
        for stmt in _def_statements(text, section):
            head = stmt.split(" + ")[0].split()
            members.setdefault(head[1], set()).update(
                re.findall(r"\( (\S+) \S+ \)", " ".join(head[2:])))
    return {"units": int(units.group(1)),
            "diearea": tuple(int(v) for v in area.groups()),
            "components": components, "pins": pins, "members": members}


def _lef_macros(path: Path) -> Dict[str, Tuple[float, float, int, str]]:
    """MACRO name -> (width, height in microns, number of PINs, digest of the
    macro's tokens line by line). A token starting with ``#`` comments out
    the rest of its line, as the LEF reader takes it, so blank lines,
    comments and spacing do not change the digest. A PIN closes on its own
    END first, so a pin named like its macro does not end it."""
    macros: Dict[str, Tuple[float, float, int, str]] = {}
    macro, pin, size, pins, body = None, None, None, 0, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        words = line.split()
        tok = next((words[:i] for i, word in enumerate(words)
                    if word.startswith("#")), words)
        if len(tok) > 1 and tok[0] == "MACRO":
            macro, pin, size, pins, body = tok[1], None, None, 0, []
        if macro and tok:
            body.append(" ".join(tok))
        if macro and len(tok) >= 4 and tok[0] == "SIZE" and tok[2] == "BY":
            size = (float(tok[1]), float(tok[3]))
        elif macro and len(tok) > 1 and tok[0] == "PIN":
            pin, pins = tok[1], pins + 1
        elif pin and len(tok) > 1 and tok[0] == "END" and tok[1] == pin:
            pin = None
        elif len(tok) > 1 and tok[0] == "END" and tok[1] == macro:
            if size is not None:
                digest = hashlib.sha256("\n".join(body).encode()).hexdigest()
                macros[macro] = size + (pins, digest)
            macro = None
    return macros


def _lef_units(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*DATABASE\s+MICRONS\s+(\d+)\s*;", text, re.M)
    if m is None:
        raise ExportError("technology LEF %s states no DATABASE MICRONS" % path)
    return int(m.group(1))


def load_views(record, contract_dir, corner: Optional[str] = None
               ) -> Dict[str, Any]:
    """Read one die's eda-views record at ``corner`` (default: the record's).

    Every row used is re-hashed and the record must be bound to the die
    contract in ``contract_dir``. Returns the files to name, each as
    ``(path, sha256)``, and the bump rows: one per pad in the pins view, at
    the pad centre in the DEF frame (um), with the pad's DEF instance and
    master, checked against the DEF placement, and the die's own port and net
    (``-`` for supply pads). ``unqualified`` lists the files of the rows used
    that the record states unqualified, and ``macros`` each LEF MACRO's body
    digest and file.
    """
    record = Path(record).resolve()
    doc = _json_file(record, "eda-views record")
    if doc.get("schema") != VIEWS_SCHEMA:
        raise ExportError("%s is not a %s record" % (record, VIEWS_SCHEMA))
    corners = doc.get("corners")
    corner = doc.get("default_corner") if corner is None else corner
    if (not isinstance(corners, list) or not isinstance(corner, str)
            or corner not in corners):
        raise ExportError("%s has no corner %r; it states %s"
                          % (record, corner, corners))
    rows = doc.get("views")
    if not isinstance(rows, list):
        raise ExportError("%s lists no views" % record)
    found: Dict[str, List[Tuple[Path, str]]] = {k: [] for k in VIEWS_KINDS}
    unqualified: List[str] = []
    for row in rows:
        kind = row.get("kind") if isinstance(row, dict) else None
        if kind not in VIEWS_KINDS:
            continue
        applies = row.get("corners", [corner])
        if not isinstance(applies, list):
            raise ExportError("%s: a %s row states corners that are not a list"
                              % (record, kind))
        if corner not in applies:
            continue
        path = _file_inside(record.parent, row.get("file"),
                            "%s: the %s row" % (record, kind))
        if _sha256(path) != row.get("sha256"):
            raise ExportError("%s: %s is not the bytes its row binds"
                              % (record, row.get("file")))
        if row.get("status") not in _VIEWS_STATUSES:
            raise ExportError("%s: the %s row has status %r"
                              % (record, kind, row.get("status")))
        if row["status"] != "recorded" and row["file"] not in unqualified:
            unqualified.append(row["file"])
        if (path, row["sha256"]) not in found[kind]:
            found[kind].append((path, row["sha256"]))
    for kind in VIEWS_KINDS:
        count = len(found[kind])
        if count == 0 or (kind in _VIEWS_ONCE and count != 1):
            raise ExportError("%s has %d %s rows at corner %s; %s expected"
                              % (record, count, kind, corner,
                                 "one is" if kind in _VIEWS_ONCE
                                 else "at least one is"))
    bbox_um = _bound_contract(doc, record, contract_dir, found["pins"][0][1])

    pins = _json_file(found["pins"][0][0], "pins view")
    if (str(pins.get("version", "")).split(".")[0] != VIEWS_PINS_MAJOR
            or pins.get("coordinate_frame") != VIEWS_PINS_FRAME):
        raise ExportError(
            "the pins view of %s is not version %s.x in the frame %r"
            % (record, VIEWS_PINS_MAJOR, VIEWS_PINS_FRAME))
    facts = _def_facts(found["def"][0][0])
    units = facts["units"]
    dbu_um = pins.get("dbu_um")
    if (not isinstance(dbu_um, (int, float)) or isinstance(dbu_um, bool)
            or abs(dbu_um * units - 1.0) > 1e-9):
        raise ExportError("the pins view of %s is not in the DEF's units (%d "
                          "per um)" % (record, units))
    macros: Dict[str, Tuple[float, float, int, str]] = {}
    sources: Dict[str, str] = {}
    for path, _ in found["lef"]:
        for macro, found_macro in _lef_macros(path).items():
            # The target binds a master to the first library that defines it.
            if macros.setdefault(macro, found_macro)[3] != found_macro[3]:
                raise ExportError("%s: MACRO %s has one body in %s and another "
                                  "in %s" % (record, macro, sources[macro],
                                             path.name))
            sources.setdefault(macro, path.name)

    rows = []
    signal_ports = set()
    for pin in pins.get("pins") or []:
        try:
            name, port, net, role = (str(pin[k]) for k in
                                     ("name", "port", "net", "role"))
            cx, cy = float(pin["center_x_dbu"]), float(pin["center_y_dbu"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportError("the pins view of %s has a malformed pin (%s)"
                              % (record, exc)) from None
        where = "%s: pad %s" % (record, name)
        if not (math.isfinite(cx) and math.isfinite(cy)):
            raise ExportError("the pins view of %s has a malformed pin (%s is "
                              "centred at (%s, %s))" % (record, name, cx, cy))
        if role not in _VIEWS_ROLES:
            raise ExportError("%s has role %r" % (where, role))
        if name not in facts["components"]:
            raise ExportError("%s is not a DEF component" % where)
        cell, px, py, orient = facts["components"][name]
        if cell not in macros:
            raise ExportError("%s is a %s, which no LEF row sizes"
                              % (where, cell))
        w, h, npins, _ = macros[cell]
        # The target places a bump by its unrotated master centre and ties it
        # to the master's first pin, so a pad is read only placed N with one
        # pin.
        if orient != "N":
            raise ExportError("%s is placed %s in the DEF; a pad must be "
                              "placed N" % (where, orient))
        if npins != 1:
            raise ExportError("%s is a %s with %d pins; a pad master must "
                              "have one" % (where, cell, npins))
        if cx != round(cx) or cy != round(cy):
            raise ExportError("%s is centred at (%s, %s) DBU, between two DBU; "
                              "the target rounds a bump to a whole DBU"
                              % (where, _fnum(cx), _fnum(cy)))
        if (abs(px + w * units / 2.0 - cx) > 1e-6
                or abs(py + h * units / 2.0 - cy) > 1e-6):
            raise ExportError("%s is centred at (%s, %s) DBU, and the DEF "
                              "places its %s at (%d, %d) %s"
                              % (where, _fnum(cx), _fnum(cy), cell, px, py,
                                 orient))
        if name not in facts["members"].get(net, ()):
            raise ExportError("%s is not on net %s in the DEF" % (where, net))
        if role == "SIGNAL":
            if facts["pins"].get(port) != net:
                raise ExportError("%s: port %s is not a DEF pin on net %s"
                                  % (where, port, net))
            if port in signal_ports:
                raise ExportError("%s: port %s already has a pad; the target "
                                  "binds a port to one bump" % (where, port))
            signal_ports.add(port)
        else:
            port = net = "-"
        rows.append({"name": name, "cell": cell, "x": cx / units,
                     "y": cy / units, "port": port, "net": net})
    if not rows or len({r["name"] for r in rows}) != len(rows):
        raise ExportError("the pins view of %s lists no pads, or a pad twice"
                          % record)
    return {
        "record": record,
        "record_sha256": _sha256(record),
        "design": doc["design"],
        "corner": corner,
        "bbox_um": bbox_um,
        "def": found["def"][0],
        "tech_lef": found["tech_lef"][0],
        "lef": found["lef"],
        "liberty": found["liberty"],
        "units": units,
        "tech_units": _lef_units(found["tech_lef"][0][0]),
        "diearea": facts["diearea"],
        "rows": rows,
        "ports": signal_ports,
        "unqualified": unqualified,
        "macros": {m: (macros[m][3], sources[m]) for m in macros},
    }


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


def _check_die_anchor(comp: Dict[str, Any], views: bool = False) -> None:
    """Die bumps are exported die-local plus the die ``position`` (the
    ``gds_origin`` anchor; see the module docstring). A die declaring
    ``bbox_center`` would need its bumps re-centered on the die GDS bbox first,
    which this exporter does not do, so it is a hard error rather than a silent
    bbox-corner misplacement. An absent anchor is also a hard error: the frame
    contract (coord_frame_contract.md 2.2) defaults it to ``bbox_center``
    (unsupported here), so the die must declare ``anchor: gds_origin``
    explicitly (which every gds_to_kicad die and the plugin writer emit).

    A die exported with an eda-views record is the reverse: its bumps are in
    the DEF frame, whose origin is the die outline's corner, so it must declare
    ``bbox_center`` and ``gds_origin`` is the hard error."""
    anchor = comp.get("anchor")
    if views:
        if anchor != "bbox_center":
            raise ExportError(
                "die %r is exported with an eda-views record, whose bumps are "
                "in the DEF frame of the die outline; it must declare anchor: "
                "bbox_center, not %r" % (comp.get("id"), anchor))
        return
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


class _Parts(NamedTuple):
    interposer: Dict[str, Any]
    dies: List[Dict[str, Any]]  # sorted by id
    mount_ref: float  # _substrate_thickness


def _split_components(assembly: Dict[str, Any], view_refs=()) -> _Parts:
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
    # Read before any die is judged, so a file that seats dies with no plane
    # is refused for that first; every caller, the view listing included.
    substrate = _substrate_thickness(interposers[0], bool(dies))
    for die in dies:
        _check_die_anchor(die, str(die.get("id")) in view_refs)
    return _Parts(interposers[0], sorted(dies, key=lambda c: str(c.get("id"))),
                  substrate)


def _bump_macro_name(method_id: str) -> str:
    """Bump macro name convention; must match the interconnect PDK
    generator's bump_macro_name (drift-guarded by a test against the
    discoverable sibling, and by ODB-531 at load time)."""
    return "BUMP_%s" % method_id.upper()


def _check_views_die(die: Dict[str, Any], views: Dict[str, Any],
                     width: float, height: float, precision: int) -> None:
    """A record die's DEF, contract and dimensions describe one outline in one
    unit: the precision, the DEF units and the tech LEF units are equal."""
    die_id = die.get("id")
    if views["design"] != str(die.get("top_cell")):
        raise ExportError("die %r has top_cell %r, and its eda-views record "
                          "describes %r" % (die_id, die.get("top_cell"),
                                            views["design"]))
    if not views["units"] == views["tech_units"] == precision:
        raise ExportError(
            "die %r: DEF units %d, technology LEF units %d and export "
            "precision %d must be equal" % (die_id, views["units"],
                                            views["tech_units"], precision))
    outline = (0, 0, round(width * precision), round(height * precision))
    bbox = views["bbox_um"]
    try:
        same_bbox = (isinstance(bbox, list) and len(bbox) == 4
                     and all(abs(float(a) - b) <= 1e-9
                             for a, b in zip(bbox, (0.0, 0.0, width, height))))
    except (TypeError, ValueError):
        same_bbox = False
    if views["diearea"] != outline or not same_bbox:
        raise ExportError(
            "die %r is %s x %s um, and its DEF DIEAREA is %s DBU and its "
            "contract bbox %s um; they must be the same outline from (0, 0)"
            % (die_id, _fnum(width), _fnum(height), views["diearea"], bbox))


def _check_stems(model: Dict[str, Any], generated: List[str]) -> None:
    """Refuse two different technology or LEF files with one stem: the target
    loads one library per file stem and silently skips a second file with a
    known stem. ``generated`` are the files the export writes itself."""
    stems: Dict[str, str] = {}
    named = [("generated:%s" % f, Path(f).stem) for f in generated]
    for def_name in sorted(model["defs"]):
        views = model["defs"][def_name].get("views")
        if views:
            named += [(sha, path.stem)
                      for path, sha in [views["tech_lef"]] + views["lef"]]
    for identity, stem in named:
        if stems.setdefault(stem, identity) != identity:
            raise ExportError(
                "two different technology or LEF files share the stem %r; the "
                "target would load only the first" % stem)


def _check_macros(model: Dict[str, Any]) -> None:
    """Refuse a MACRO name two record dies define with different bodies: the
    target binds a master to the first library that defines it."""
    bodies: Dict[str, Tuple[str, str]] = {}
    for def_name in sorted(model["defs"]):
        views = model["defs"][def_name].get("views") or {"macros": {}}
        for macro, (digest, lef) in sorted(views["macros"].items()):
            where = "%s of %s" % (lef, def_name)
            first = bodies.setdefault(macro, (digest, where))
            if first[0] != digest:
                raise ExportError("MACRO %s has one body in %s and another in "
                                  "%s" % (macro, first[1], where))


def _list_liberty(model: Dict[str, Any]) -> None:
    """Set each record def's ``liberty`` list. The target does not
    deduplicate Liberty, so each file is listed once, on the first def in name
    order, and two different files with one basename are refused."""
    names: Dict[str, str] = {}
    listed = set()
    for def_name in sorted(model["defs"]):
        views = model["defs"][def_name].get("views")
        if not views:
            continue
        own = []
        for path, sha in views["liberty"]:
            if names.setdefault(path.name, sha) != sha:
                raise ExportError("two different Liberty files are named %s"
                                  % path.name)
            if sha not in listed:
                listed.add(sha)
                own.append(path)
        model["defs"][def_name]["liberty"] = own


def _build_model(assembly: Dict[str, Any],
                 die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 die_views: Optional[Dict[str, Dict[str, Any]]] = None
                 ) -> Dict[str, Any]:
    """Resolve the assembly into the def/instance/connection model."""
    die_pins = die_pins or {}
    die_views = die_views or {}
    units = (assembly.get("assembly") or {}).get("units")
    if units != SUPPORTED_UNITS:
        raise ExportError(
            "assembly.units is %r; only %r is supported" % (units, SUPPORTED_UNITS)
        )

    interposer, dies, mount_ref = _split_components(assembly, set(die_views))
    precision = _precision(assembly)

    ipos_w = _require_dimension(interposer, "width")
    ipos_h = _require_dimension(interposer, "height")
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
    unknown_views = set(die_views) - {str(d.get("id")) for d in dies}
    if unknown_views:
        raise ExportError(
            "eda-views records were supplied for unknown die refs: %s"
            % ", ".join(sorted(unknown_views)))
    both = set(die_pins) & set(die_views)
    if both:
        raise ExportError(
            "dies %s were given both a pin list and an eda-views record"
            % ", ".join(sorted(both)))

    for die in dies:
        die_id = str(die["id"])
        width = _require_dimension(die, "width")
        height = _require_dimension(die, "height")
        thickness = _require_dimension(die, "thickness")
        x, y, z = _position(die)
        quarter = _rotation_quarter(die)
        orient = _orient_token(die, quarter)
        views = die_views.get(die_id)

        key = (
            str(die.get("layout")), str(die.get("top_cell")),
            str(die.get("technology")), width, height, thickness,
        )
        bump = None
        if views:
            # The target's MZ mirrors z only; the flip's x mirror goes into
            # the orientation so the bump map stays in the DEF frame.
            if orient.startswith("MZ"):
                if quarter:
                    raise ExportError(
                        "die %r: a flipped die with an eda-views record and a "
                        "quarter turn is not supported" % die_id)
                orient = "MZ_MY"
            _check_views_die(die, views, width, height, precision)
            key = ("__views__", views["record_sha256"], thickness)
        elif die_id in die_pins:
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
            if views:
                base = views["design"]
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
            defs[def_name] = {
                "type": "die",
                "width": width,
                "height": height,
                "thickness": thickness,
                "technology": technology,
                "key": key,
                "bump": bump,
            }
            if bump or views:
                defs[def_name]["bmap"] = "%s.bmap" % def_name
            if views:
                defs[def_name]["views"] = views

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

    model = {
        "name": str((assembly.get("assembly") or {}).get("name")),
        "precision": precision,
        "defs": defs,
        "instances": instances,
        "connections": connections,
    }
    # The files this model makes the export write next to the .3dbv.
    generated = ["%s.lef" % t for t in _generated_technologies(model)]
    generated += sorted({d["bump"]["lef"] for d in defs.values() if d.get("bump")})
    _check_stems(model, generated)
    _check_macros(model)
    _list_liberty(model)
    return model


def _generated_technologies(model: Dict[str, Any]) -> List[str]:
    """Technologies that get a generated LEF: every def without a record."""
    return sorted({d["technology"] for d in model["defs"].values()
                   if not d.get("views")})


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


def _view_path(path: Path, base_dir) -> str:
    """A record file as the .3dbv names it: relative to ``base_dir`` (the
    .3dbv's directory, where the target resolves it) or absolute without."""
    text = str(path) if base_dir is None else os.path.relpath(
        path, Path(base_dir).resolve())
    text = text.replace(os.sep, "/")
    if not _PLAIN_PATH.fullmatch(text):
        raise ExportError("cannot name %r in the .3dbv: only letters, digits "
                          "and _ . / + - are written unquoted" % text)
    return text


def render_3dbv(assembly: Dict[str, Any], *,
                die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                die_views: Optional[Dict[str, Dict[str, Any]]] = None,
                base_dir=None) -> str:
    """Render the ChipletDef (.3dbv) view of a finalized assembly dict.

    ``die_views`` maps die refs to :func:`load_views` results; their files are
    named relative to ``base_dir``, the directory the .3dbv is written to.
    """
    model = _build_model(assembly, die_pins, die_views)
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
        views = entry.get("views")
        if views:
            lines.append("      APR_tech_file: [%s]"
                         % _view_path(views["tech_lef"][0], base_dir))
            lines.append("      LEF_file: [%s]" % ", ".join(
                _view_path(path, base_dir) for path, _ in views["lef"]))
            if entry["liberty"]:
                lines.append("      liberty_file: [%s]" % ", ".join(
                    _view_path(path, base_dir) for path in entry["liberty"]))
            lines.append("      DEF_file: %s"
                         % _view_path(views["def"][0], base_dir))
        else:
            lines.append("      APR_tech_file: [%s.lef]" % entry["technology"])
        if bump:
            lines.append("      LEF_file: [%s]" % bump["lef"])
        lines.append("    regions:")
        _render_regions(lines, entry["width"], entry["height"],
                        front_bmap=entry.get("bmap"))
    return "\n".join(lines) + "\n"


def render_bmap(assembly: Dict[str, Any], def_name: str, *,
                die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                die_views: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> str:
    """Render the bump map of one bump-aware or record ChipletDef."""
    model = _build_model(assembly, die_pins, die_views)
    entry = model["defs"].get(def_name)
    if entry is not None and entry.get("views"):
        views = entry["views"]
        lines = [
            "# Bump map for ChipletDef %s from its eda-views record at corner "
            "%s; derived from %s.chiplet by chiplet2dbx (ADK)."
            % (def_name, views["corner"], model["name"]),
            "# bumpInstName bumpCellType x y portName netName  (x/y = pad "
            "center, DEF frame um)",
        ]
        for row in views["rows"]:
            lines.append("%s %s %s %s %s %s" % (
                row["name"], row["cell"], _fnum(row["x"]), _fnum(row["y"]),
                row["port"], row["net"]))
        return "\n".join(lines) + "\n"
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
                die_pins: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                die_views: Optional[Dict[str, Dict[str, Any]]] = None,
                top_verilog: Optional[str] = None) -> str:
    """Render the assembly (.3dbx) view; includes ``dbv_filename``.

    ``top_verilog`` is written as the Design's verilog_file as given; the
    export checks the file against the records before naming it.
    """
    model = _build_model(assembly, die_pins, die_views)
    lines: List[str] = [
        "# Derived from %s.chiplet by chiplet2dbx (ADK); regenerate, do not edit."
        % model["name"],
    ]
    _render_header(lines, model["precision"], include=dbv_filename)
    lines.append("")
    lines.append("Design:")
    lines.append("  name: \"%s\"" % model["name"])
    if top_verilog:
        lines.append("  external:")
        lines.append("    verilog_file: %s" % top_verilog)
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


def _check_top_verilog(path: Path, model: Dict[str, Any],
                       die_views: Dict[str, Dict[str, Any]]) -> None:
    """Refuse a system top the target would bind silently wrong: it must be
    one module named after the Design, instantiating record dies by their
    instance name and top cell, on signal ports of their pins views only. The
    target skips a missing file and drops unknown instances and ports without
    a message, so this is checked before the file is named."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise ExportError("cannot read the system top %s: %s" % (path, exc)) from None
    text = re.sub(r"//[^\n]*", "", text)
    items, pos, end = [], 0, len(text.rstrip())
    while pos < end:
        item = _TOP_ITEM.match(text, pos)
        if item is None:
            raise ExportError(
                "the system top %s is not a generated system top (one module "
                "of wires and dies with named connections) at %r"
                % (path, text[pos:pos + 40].strip()))
        items.append(item)
        pos = item.end()
    modules = [item.group("name") for item in items if item.group("module")]
    if modules != [model["name"]]:
        raise ExportError("the system top %s defines modules %s; exactly one, "
                          "%s (the Design name), is expected"
                          % (path, modules, model["name"]))
    if (not items[0].group("module") or not items[-1].group("end")
            or sum(1 for item in items if item.group("end")) != 1):
        raise ExportError("the system top %s does not open with its module "
                          "and close with one endmodule" % path)
    seen = set()
    for item in items:
        if not item.group("inst"):
            continue
        cell, inst = item.group("cell"), item.group("inst_name")
        views = die_views.get(inst)
        if views is None or inst in seen:
            raise ExportError("the system top %s instantiates %s, which is not "
                              "a die with an eda-views record, or twice"
                              % (path, inst))
        seen.add(inst)
        if cell != views["design"]:
            raise ExportError("the system top %s makes %s a %s; its record "
                              "describes %s" % (path, inst, cell, views["design"]))
        for port in re.findall(r"\.(\w+)", item.group("body")):
            if port not in views["ports"]:
                raise ExportError("the system top %s connects %s.%s, which is "
                                  "not a signal port of its pins view"
                                  % (path, inst, port))
    if not seen:
        raise ExportError("the system top %s instantiates no die" % path)


def _load_die_views(die_views, corner: Optional[str], top_verilog=None
                    ) -> Dict[str, Dict[str, Any]]:
    if not die_views and (corner is not None or top_verilog is not None):
        raise ExportError("a corner or a system top needs at least one "
                          "eda-views record (--views)")
    loaded = {}
    for ref, value in (die_views or {}).items():
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ExportError("die %r: an eda-views record is given as a "
                              "(record, contract_dir) pair" % ref)
        record, contract_dir = value
        loaded[str(ref)] = load_views(record, contract_dir, corner)
    corners = sorted({views["corner"] for views in loaded.values()})
    if len(corners) > 1:
        raise ExportError("the eda-views records default to different corners "
                          "(%s); name one with --corner" % ", ".join(corners))
    return loaded


def export_3dblox(assembly: Dict[str, Any], out_dir,
                  name: Optional[str] = None,
                  die_pins: Optional[Dict[str, Any]] = None,
                  die_views: Optional[Dict[str, Tuple[Any, Any]]] = None,
                  corner: Optional[str] = None,
                  top_verilog=None) -> Tuple[Path, Path, List[Path]]:
    """Write ``<name>.3dbv``/``.3dbx`` plus one tech LEF per technology.

    ``die_pins`` maps die refs to either a ``*.pins.json`` path or an
    already-parsed pin list; when given, the affected defs also get a
    ``.bmap`` and a per-(method, technology) bump macro LEF rendered by the
    interconnect PDK's generator.

    ``die_views`` maps die refs to ``(record, contract_dir)``: an eda-views
    record and the die contract it must be bound to, read at ``corner`` (by
    default the records' shared default corner). Those defs name the record's
    files in place and get a ``.bmap`` from its pins view. ``top_verilog`` is
    a system top named in the .3dbx once checked against the records.
    """
    parsed_pins = _parse_pins(die_pins)
    loaded_views = _load_die_views(die_views, corner, top_verilog)
    return _write_3dblox(assembly, out_dir, name, parsed_pins, loaded_views,
                         top_verilog)


def _parse_pins(die_pins) -> Dict[str, List[Dict[str, Any]]]:
    return {str(ref): value if isinstance(value, list) else load_pinlist(value)
            for ref, value in (die_pins or {}).items()}


def _write_3dblox(assembly, out_dir, name, parsed_pins, loaded_views,
                  top_verilog) -> Tuple[Path, Path, List[Path]]:
    model = _build_model(assembly, parsed_pins, loaded_views)
    out_dir = Path(out_dir)
    top_name = None
    if top_verilog is not None:
        top_path = Path(top_verilog).resolve()
        _check_top_verilog(top_path, model, loaded_views)
        top_name = _view_path(top_path, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = name or model["name"]
    dbv_path = out_dir / ("%s.3dbv" % base)
    dbx_path = out_dir / ("%s.3dbx" % base)
    dbv_path.write_text(render_3dbv(assembly, die_pins=parsed_pins,
                                    die_views=loaded_views, base_dir=out_dir),
                        encoding="utf-8")
    dbx_path.write_text(render_3dbx(assembly, dbv_path.name,
                                    die_pins=parsed_pins,
                                    die_views=loaded_views,
                                    top_verilog=top_name), encoding="utf-8")
    extra_paths = []

    bump_defs = {n: d for n, d in model["defs"].items() if d.get("bump")}
    generator = _bump_lef_generator() if bump_defs else None
    for def_name in sorted(n for n, d in model["defs"].items()
                           if d.get("bmap")):
        bmap_path = out_dir / model["defs"][def_name]["bmap"]
        bmap_path.write_text(
            render_bmap(assembly, def_name, die_pins=parsed_pins,
                        die_views=loaded_views),
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
    for technology in _generated_technologies(model):
        lef_path = out_dir / ("%s.lef" % technology)
        lef_path.write_text(render_tech_lef(model["precision"], route_layer),
                            encoding="utf-8")
        extra_paths.append(lef_path)
    return dbv_path, dbx_path, extra_paths


def _views_lines(assembly: Dict[str, Any], loaded_views: Dict[str, Any]
                 ) -> List[str]:
    """One line per die, in id order: the kinds its eda-views record gave the
    export and the files of those rows the record states unqualified, or
    ``absent`` for a die exported without a record."""
    lines = []
    for die in _split_components(assembly, set(loaded_views)).dies:
        ref = str(die.get("id"))
        views = loaded_views.get(ref)
        if views is None:
            lines.append("%s: eda views: absent" % ref)
        else:
            lines.append("%s: eda views: %s; unqualified: %s"
                         % (ref, ", ".join(VIEWS_KINDS),
                            ", ".join(views["unqualified"]) or "none"))
    return lines


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
               "--pins U2=chiplets/die_b.pins.json\n"
               "  python openroad/chiplet2dbx.py --chiplet demo.chiplet "
               "--out-dir build/3dblox \\\n"
               "      --views U1=views/u1/eda-views.json "
               "--contract U1=contracts/die_a \\\n"
               "      --top-verilog build/top.v\n",
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
    parser.add_argument("--views", action="append", default=[],
                        metavar="REF=RECORD",
                        help="per-die eda-views record (eda-views.json); "
                             "repeatable; exports that die with its recorded "
                             "views and needs --contract for the same REF")
    parser.add_argument("--contract", action="append", default=[],
                        metavar="REF=DIR",
                        help="the die contract directory the --views record "
                             "of REF must be bound to")
    parser.add_argument("--corner", default=None,
                        help="corner to read the records at (default: their "
                             "shared default corner)")
    parser.add_argument("--top-verilog", default=None, metavar="FILE",
                        help="system top Verilog named in the .3dbx Design, "
                             "after it is checked against the records")
    return parser.parse_args(argv)


def _parse_ref_args(specs: List[str], option: str = "--pins"
                    ) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for spec in specs:
        ref, sep, path = spec.partition("=")
        if not sep or not ref or not path:
            raise ExportError("%s expects REF=PATH, got %r" % (option, spec))
        if ref in result:
            raise ExportError("%s given twice for ref %r" % (option, ref))
        result[ref] = path
    return result


def _parse_views_args(views: List[str], contracts: List[str]
                      ) -> Dict[str, Tuple[str, str]]:
    records = _parse_ref_args(views, "--views")
    dirs = _parse_ref_args(contracts, "--contract")
    if set(records) != set(dirs):
        raise ExportError(
            "each --views needs a --contract for the same ref, and each "
            "--contract a --views; unpaired: %s"
            % ", ".join(sorted(set(records) ^ set(dirs))))
    return {ref: (records[ref], dirs[ref]) for ref in records}


def main(argv=None) -> int:
    args = parse_args(argv)
    chiplet_path = Path(args.chiplet)
    try:
        assembly = load_chiplet(chiplet_path)
        out_dir = Path(args.out_dir) if args.out_dir else chiplet_path.parent
        die_pins = _parse_pins(_parse_ref_args(args.pins))
        loaded_views = _load_die_views(
            _parse_views_args(args.views, args.contract), args.corner,
            args.top_verilog)
        dbv_path, dbx_path, extra_paths = _write_3dblox(
            assembly, out_dir, args.name, die_pins, loaded_views,
            args.top_verilog)
    except (cfio.ChipletFormatError, ExportError, OSError) as exc:
        print("chiplet2dbx: error: %s" % exc, file=sys.stderr)
        return 1
    for path in [dbv_path, dbx_path] + extra_paths:
        print(path)
    if args.views:
        for line in _views_lines(assembly, loaded_views):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
