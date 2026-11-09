#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""ADK-side entry for assembly-LVS resolved v2 documents.

Reads one resolved v2 document (``assembly-lvs.resolved/v2``) together with an
ADK attachment sidecar (``adk-resolved-attachment`` 1.0.0) and writes the ADK
view of the same assembly: an assembled GDS whose carrier cell is a
geometry-preserved copy of the resolved interposer layout with the ADK die
layouts instanced on it, the boundary manifest, the per-method IXN sidecar, the
pillar manifest, a ``.chiplet`` and an entry manifest that records every input
and output hash, the frame constants, the body measurements and the exhaustive
terminal correspondence tables.

The entry is a nominal identity, frame and correspondence adapter. It runs no
DRC and no LVS, it invents no z, it resolves no collision, and it does not
certify the process: the shape and contact predicates it applies to the
attachment artwork are corpus-binding controls of profile
``nominal-identity-carrier/1.0.0`` (a failed predicate means "the artwork
differs from what this profile binds", never "a process rule is violated").

Arithmetic is exact: every number of the resolved document is read as a Decimal
from its scalar text and converted to integer database units on
``technology.grid_um``; every layout in play (the resolved carrier and proxies,
the ADK die layouts, the assembled output) must have a database unit equal to
``grid_um``, and there is no tolerance argument anywhere.

Frames (docs/resolved_v2_entry.md): L(die) die-local, A resolved assembly,
G(int) the interposer's own GDS frame, C the ADK canonical frame (G rebased to
the lower-left corner of the interposer top-cell bbox). The chain from a
die-local point to C is

    c(p) = pos_d + R(theta_d) * M_y^m_d * (p - anchor_d) - pos_i + anchor_i - corner

with the mirror applied before the quarter turn (assembly-lvs ``_orient_xy``),
and ``g(p) = c(p) + corner`` is the same point in G(int). Because both the LVS
and the ADK conventions mirror before rotating and ``R(t) M_y = R(t+180) M_x``,
a resolved die ``(theta, mirror_x true)`` is the ADK die ``(rotation.z = theta +
180 mod 360, flip_chip)``.

Exit codes: 0 success, 1 usage, 2 refusal (nothing left behind in the output
directory), registry codes 1/5/6 for adapter id problems (``adk_registry``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, Iterable, List, NoReturn, Optional, Tuple

ADK_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = ADK_ROOT / "vendor"
for _p in (str(VENDOR_DIR), str(ADK_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import chiplet_format_io as cfio  # noqa: E402
import adk_registry  # noqa: E402
import yaml  # noqa: E402
from yaml.constructor import ConstructorError  # noqa: E402

try:
    import klayout.db as db
except ImportError:  # pragma: no cover - reported at run time
    db = None

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
ENTRY_SCHEMA = "adk-resolved-v2-entry"
ENTRY_VERSION = "1.0.0"
SIDECAR_SCHEMA = "adk-resolved-attachment"
SIDECAR_VERSION = "1.0.0"
RESOLVED_SCHEMA_VERSION = "assembly-lvs.resolved/v2"
PROFILE = "nominal-identity-carrier/1.0.0"
GENERATOR = "resolved_v2_entry.py"
#: Reserved terminal-id namespace of the frozen v2 contract (assembly-lvs model.py).
IO_TERMINAL_NAMESPACE = "io_pad:"
#: Exact-safe coordinate range in grid units (assembly-lvs geometry.py).
SAFE_GRID_COORD = (1 << 30) - 1
# Output file names derive from assembly.id only when it is one plain path component;
# anything else (separators, a leading dot, an empty or over-long string) is a refusal,
# never a path (an LVS-side finding: an unconstrained ID reached the output paths).
OUTPUT_STEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
# Completed output names (stem plus suffix) share the alphabet but not the id length rule:
# one plain component of at most 255 characters, so every id the stem rule admits (up to
# 64 characters) yields valid names for all outputs.
OUTPUT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
ATTACHMENT_ROLES = ("opening", "dfpad", "body", "body2", "mark")
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2

SCOPE_STATEMENT = (
    "This bridge establishes reproducible nominal cross-tool binding (identity, "
    "frames, correspondence) between one assembly-lvs resolved v2 document and the "
    "ADK view of the same assembly. It does not prove clean DRC or LVS, "
    "uncertain-placement preservation, contact completeness, manufacturing "
    "qualification, or any novelty from combining two checkers. Its attachment "
    "predicates are corpus-binding controls of profile " + PROFILE + ", not "
    "process rules."
)


class Refusal(Exception):
    """A refusal: the entry stops, nothing is written, exit 2."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


class Usage(Exception):
    """A usage error: bad arguments or an unreadable sidecar path, exit 1."""


def _fail(path: str, message: str) -> NoReturn:
    raise Refusal(path, message)


# --------------------------------------------------------------------------- #
# Strict syntax layer: JSON and YAML without duplicate keys, numbers as Decimal #
# --------------------------------------------------------------------------- #
_INT_TEXT = re.compile(r"\A[-+]?[0-9]+\Z")


def _decimal_from_text(text: str, where: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        _fail(where, f"number {text!r} is not a decimal")


def _json_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("document", f"duplicate key {key!r} in a JSON object")
        result[key] = value
    return result


def _json_constant(text: str) -> NoReturn:
    _fail("document", f"non-finite number {text!r}")


def load_strict_json(text: str) -> Any:
    """Parse JSON refusing repeated keys and NaN/Infinity; numbers as Decimal."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_json_pairs,
            parse_int=lambda s: _decimal_from_text(s, "document"),
            parse_float=lambda s: _decimal_from_text(s, "document"),
            parse_constant=_json_constant,
        )
    except json.JSONDecodeError as exc:
        _fail("document", f"invalid JSON: {exc}")


class _StrictLoader(yaml.SafeLoader):
    """YAML loader: duplicate keys refused, every number a Decimal from its text."""


def _construct_unique_mapping(loader: _StrictLoader, node: yaml.MappingNode,
                              deep: bool = False) -> Dict[Any, Any]:
    loader.flatten_mapping(node)
    result: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   "found an unhashable key", key_node.start_mark) from exc
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   f"found duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


#: YAML 1.1 special floats, carried as non-finite Decimals so that the value
#: layer refuses them with the same "finite decimal" message as JSON constants.
_YAML_SPECIAL = {".nan": "NaN", ".inf": "Infinity", "+.inf": "Infinity", "-.inf": "-Infinity"}


_LEADING_ZERO = re.compile(r"\A[-+]?0[0-9]")


def _refuse_leading_zero(text: str) -> None:
    # Profile policy: a numeric scalar with a leading zero is ambiguous between YAML 1.1
    # octal (PyYAML reads 012 as 10) and decimal, and JSON forbids it outright; the
    # entry refuses it instead of choosing a reading (LVS finding on the differential).
    if _LEADING_ZERO.match(text):
        _fail("document", f"numeric scalar {text!r} has a leading zero (YAML 1.1 octal versus "
              "decimal is ambiguous); profile policy: write the plain decimal")


def _construct_yaml_decimal(loader: _StrictLoader, node: yaml.ScalarNode) -> Decimal:
    text = loader.construct_scalar(node).replace("_", "")
    special = _YAML_SPECIAL.get(text.lower())
    if special is not None:
        return Decimal(special)
    _refuse_leading_zero(text)
    return _decimal_from_text(text, "document")


def _construct_yaml_int(loader: _StrictLoader, node: yaml.ScalarNode) -> Decimal:
    text = loader.construct_scalar(node).replace("_", "")
    if not _INT_TEXT.match(text):
        _fail("document", f"integer {text!r} is not in plain decimal notation")
    _refuse_leading_zero(text)
    return Decimal(text)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                              _construct_unique_mapping)
_StrictLoader.add_constructor("tag:yaml.org,2002:float", _construct_yaml_decimal)
_StrictLoader.add_constructor("tag:yaml.org,2002:int", _construct_yaml_int)


def load_strict_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        _fail("document", f"invalid YAML: {exc}")


def load_strict_document(path: Path) -> Tuple[Any, bytes]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail("document", f"not UTF-8: {exc}")
    if path.suffix.lower() == ".json":
        return load_strict_json(text), raw
    return load_strict_yaml(text), raw


# --------------------------------------------------------------------------- #
# Value helpers (mirroring assembly-lvs parser.py, exact)                      #
# --------------------------------------------------------------------------- #
def _mapping(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        _fail(path, "expected a mapping with string keys")
    return value


def _sequence(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        _fail(path, "expected a sequence")
    return value


def _keys(value: Dict[str, Any], path: str, required: Iterable[str],
          optional: Iterable[str] = ()) -> None:
    required = set(required)
    optional = set(optional)
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        _fail(path, f"missing required keys {missing}")
    if unknown:
        _fail(path, f"unknown keys {unknown}")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "expected a non-empty string")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "expected a boolean")
    return value


def _decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        _fail(path, "expected a finite decimal number")
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        _fail(path, "expected a finite decimal number")
    return result


def _integer(value: Any, path: str) -> int:
    result = _decimal(value, path)
    if result != result.to_integral_value():
        _fail(path, "expected an integer")
    return int(result)


def _grid_units(value: Decimal, grid: Decimal, path: str) -> int:
    units = value / grid
    if units != units.to_integral_value():
        _fail(path, f"{value} um is not aligned to technology.grid_um={grid}")
    result = int(units)
    if not -SAFE_GRID_COORD <= result <= SAFE_GRID_COORD:
        _fail(path, f"{value} um is outside the exact-safe range of "
                    f"[-{SAFE_GRID_COORD}, {SAFE_GRID_COORD}] grid units")
    return result


def _point_dbu(value: Any, path: str, grid: Decimal) -> Tuple[int, int]:
    values = _sequence(value, path)
    if len(values) != 2:
        _fail(path, "expected exactly two coordinates [x, y]")
    return (_grid_units(_decimal(values[0], f"{path}[0]"), grid, f"{path}[0]"),
            _grid_units(_decimal(values[1], f"{path}[1]"), grid, f"{path}[1]"))


def _size_dbu(value: Any, path: str, grid: Decimal) -> Tuple[int, int]:
    size = _point_dbu(value, path, grid)
    if size[0] <= 0 or size[1] <= 0:
        _fail(path, "both dimensions must be greater than zero")
    if size[0] % 2 or size[1] % 2:
        _fail(path, "window edges must be representable on the technology grid")
    return size


def _string_list(value: Any, path: str, *, nonempty: bool = True,
                 unique: bool = True) -> Tuple[str, ...]:
    items = tuple(_string(item, f"{path}[{i}]")
                  for i, item in enumerate(_sequence(value, path)))
    if nonempty and not items:
        _fail(path, "must not be empty")
    if unique and len(items) != len(set(items)):
        _fail(path, "contains duplicate values")
    return items


def _portable_relative_path(value: Any, path: str) -> str:
    configured = _string(value, path)
    if "\\" in configured:
        _fail(path, "must use portable '/' path separators")
    posix = PurePosixPath(configured)
    windows = PureWindowsPath(configured)
    if posix.is_absolute() or windows.is_absolute():
        _fail(path, "must be relative")
    if ".." in posix.parts or ".." in windows.parts:
        _fail(path, "must not escape its root directory")
    return configured


def _regular_file(path: Path, where: str) -> Path:
    if path.is_symlink():
        _fail(where, f"{path} is a symlink; a regular file is required")
    if not path.is_file():
        _fail(where, f"file does not exist: {path}")
    return path


def _no_symlink_below(root: Path, configured: str, where: str) -> None:
    """Refuse a configured path any component of which, below ``root``, is a symlink."""
    current = root
    for part in PurePosixPath(configured).parts:
        current = current / part
        if current.is_symlink():
            _fail(where, f"{configured} is reached through a symlink at {current}; "
                         "a regular file is required")


def _in_root_file(root: Path, configured: str, where: str) -> Path:
    _portable_relative_path(configured, where)
    _no_symlink_below(root, configured, where)
    resolved = (root / configured).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        _fail(where, f"{configured} escapes its root {root}")
    return _regular_file(resolved, where)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: Any, path: str) -> str:
    digest = _string(value, path)
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        _fail(path, "expected a lowercase SHA-256 hex digest")
    return digest


# --------------------------------------------------------------------------- #
# Resolved v2 model                                                            #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Technology:
    id: str
    grid_um: Decimal
    conductors: Tuple[str, ...]
    vias: Tuple[Tuple[str, Tuple[str, str]], ...]

    @property
    def all_layers(self) -> frozenset:
        return frozenset(self.conductors) | frozenset(v[0] for v in self.vias)


@dataclass(frozen=True)
class LayerMap:
    source: Tuple[int, int]
    technology_layer: str


@dataclass(frozen=True)
class IOPad:
    id: str
    pin: str
    position: Tuple[int, int]
    size: Tuple[int, int]
    technology_layer: str
    net: Optional[str]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.id, self.pin)


@dataclass(frozen=True)
class Placement:
    position: Tuple[int, int]
    rotation_deg: int
    mirror_x: bool
    anchor: Tuple[int, int]


@dataclass(frozen=True)
class Component:
    id: str
    role: str
    layout_path: str
    layout_resolved: Path
    top_cell: str
    placement: Placement
    layers: Tuple[LayerMap, ...]
    io_pads: Tuple[IOPad, ...]


@dataclass(frozen=True)
class Terminal:
    id: str
    component: str
    pin: str
    technology_layer: str
    position: Tuple[int, int]
    size: Tuple[int, int]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.component, self.pin)


@dataclass(frozen=True)
class Endpoint:
    component: str
    pin: str
    layer: Optional[str]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.component, self.pin)


@dataclass(frozen=True)
class LogicalNet:
    name: str
    connections: Tuple[Endpoint, ...]
    net_class: Optional[str]
    external: Optional[bool]


@dataclass(frozen=True)
class InterfaceContact:
    id: str
    pairs: Tuple[Tuple[str, str], Tuple[str, str]]  # ((role, layer), (role, layer))

    def layer_of(self, role: str) -> str:
        for r, layer in self.pairs:
            if r == role:
                return layer
        raise KeyError(role)


@dataclass
class ResolvedDocument:
    schema_version: str
    assembly_id: str
    units: str
    interface: Dict[str, str]
    components: Tuple[Component, ...]
    technology: Technology
    contacts: Tuple[InterfaceContact, ...]
    terminals: Tuple[Terminal, ...]
    nets: Tuple[LogicalNet, ...]
    uncertainty_model: str
    uncertainty: Dict[str, Decimal]
    provenance: Dict[str, Optional[str]]
    source_path: Path
    source_sha256: str
    #: die id -> (technology layer, GDS source pair) of its dedicated contact layer
    die_contact: Dict[str, Tuple[str, Tuple[int, int]]] = field(default_factory=dict)

    @property
    def interposer(self) -> Component:
        return next(c for c in self.components if c.role == "interposer")

    @property
    def dies(self) -> Tuple[Component, ...]:
        return tuple(c for c in self.components if c.role == "die")

    def component(self, component_id: str) -> Component:
        return next(c for c in self.components if c.id == component_id)

    def terminals_of(self, die_id: str) -> Tuple[Terminal, ...]:
        return tuple(t for t in self.terminals if t.component == die_id)


def _parse_technology(value: Any) -> Technology:
    data = _mapping(value, "technology")
    _keys(data, "technology", {"id", "grid_um", "conductors", "vias"})
    tech_id = _string(data["id"], "technology.id")
    grid = _decimal(data["grid_um"], "technology.grid_um")
    if grid <= 0:
        _fail("technology.grid_um", "must be greater than zero")
    conductors = _string_list(data["conductors"], "technology.conductors")
    vias: List[Tuple[str, Tuple[str, str]]] = []
    names: set = set()
    for i, item in enumerate(_sequence(data["vias"], "technology.vias")):
        path = f"technology.vias[{i}]"
        via = _mapping(item, path)
        _keys(via, path, {"layer", "connects"})
        layer = _string(via["layer"], f"{path}.layer")
        if layer in names or layer in conductors:
            _fail(f"{path}.layer", f"duplicate technology layer {layer!r}")
        connects = _string_list(via["connects"], f"{path}.connects")
        if len(connects) != 2:
            _fail(f"{path}.connects", "a via must connect exactly two conductors")
        unknown = sorted(set(connects) - set(conductors))
        if unknown:
            _fail(f"{path}.connects", f"unknown conductor layers {unknown}")
        names.add(layer)
        vias.append((layer, (connects[0], connects[1])))
    return Technology(tech_id, grid, conductors, tuple(vias))


def _parse_layer_map(value: Any, path: str, technology: Technology) -> LayerMap:
    data = _mapping(value, path)
    _keys(data, path, {"source", "technology_layer"})
    source = _sequence(data["source"], f"{path}.source")
    if len(source) != 2:
        _fail(f"{path}.source", "expected [GDS layer, datatype]")
    layer = _integer(source[0], f"{path}.source[0]")
    datatype = _integer(source[1], f"{path}.source[1]")
    if not 0 <= layer <= 65535 or not 0 <= datatype <= 65535:
        _fail(f"{path}.source", "GDS layer and datatype must be in [0, 65535]")
    tech_layer = _string(data["technology_layer"], f"{path}.technology_layer")
    if tech_layer not in technology.all_layers:
        _fail(f"{path}.technology_layer", f"unknown layer {tech_layer!r}")
    return LayerMap((layer, datatype), tech_layer)


def _parse_io_pad(value: Any, path: str, technology: Technology) -> IOPad:
    data = _mapping(value, path)
    _keys(data, path, {"id", "pin", "position_um", "size_um", "technology_layer"}, {"net"})
    layer = _string(data["technology_layer"], f"{path}.technology_layer")
    if layer not in technology.conductors:
        _fail(f"{path}.technology_layer", "an I/O pad must use a conductor layer")
    net = data.get("net")
    if net is not None:
        net = _string(net, f"{path}.net")
    return IOPad(
        id=_string(data["id"], f"{path}.id"),
        pin=_string(data["pin"], f"{path}.pin"),
        position=_point_dbu(data["position_um"], f"{path}.position_um", technology.grid_um),
        size=_size_dbu(data["size_um"], f"{path}.size_um", technology.grid_um),
        technology_layer=layer,
        net=net,
    )


def _parse_component(value: Any, index: int, source_dir: Path,
                     technology: Technology) -> Component:
    path = f"assembly.components[{index}]"
    data = _mapping(value, path)
    _keys(data, path, {"id", "role", "layout", "placement", "layers"}, {"io_pads"})
    component_id = _string(data["id"], f"{path}.id")
    role = _string(data["role"], f"{path}.role")
    if role not in {"die", "interposer"}:
        _fail(f"{path}.role", "v2 supports only 'die' and 'interposer'")
    layout = _mapping(data["layout"], f"{path}.layout")
    _keys(layout, f"{path}.layout", {"path", "top_cell"})
    configured = _portable_relative_path(layout["path"], f"{path}.layout.path")
    resolved = (source_dir / configured).resolve()
    if not resolved.is_file():
        _fail(f"{path}.layout.path", f"file does not exist: {configured}")
    _no_symlink_below(source_dir, configured, f"{path}.layout.path")
    top_cell = _string(layout["top_cell"], f"{path}.layout.top_cell")

    placement = _mapping(data["placement"], f"{path}.placement")
    _keys(placement, f"{path}.placement",
          {"position_um", "rotation_deg", "mirror_x", "anchor_um"})
    rotation = _integer(placement["rotation_deg"], f"{path}.placement.rotation_deg")
    if rotation not in {0, 90, 180, 270}:
        _fail(f"{path}.placement.rotation_deg", "must be one of 0, 90, 180, 270")
    place = Placement(
        position=_point_dbu(placement["position_um"], f"{path}.placement.position_um",
                            technology.grid_um),
        rotation_deg=rotation,
        mirror_x=_boolean(placement["mirror_x"], f"{path}.placement.mirror_x"),
        anchor=_point_dbu(placement["anchor_um"], f"{path}.placement.anchor_um",
                          technology.grid_um),
    )

    maps = tuple(_parse_layer_map(item, f"{path}.layers[{i}]", technology)
                 for i, item in enumerate(_sequence(data["layers"], f"{path}.layers")))
    if not maps:
        _fail(f"{path}.layers", "must not be empty")
    sources = [m.source for m in maps]
    if len(sources) != len(set(sources)):
        _fail(f"{path}.layers", "a source GDS layer may be mapped only once")
    targets = [m.technology_layer for m in maps]
    if len(targets) != len(set(targets)):
        _fail(f"{path}.layers", "a technology layer may be mapped from only one GDS source")
    if not any(m.technology_layer in technology.conductors for m in maps):
        _fail(f"{path}.layers", "component must map at least one conductor layer")

    pads = tuple(_parse_io_pad(item, f"{path}.io_pads[{i}]", technology)
                 for i, item in enumerate(_sequence(data.get("io_pads", []), f"{path}.io_pads")))
    if pads and role != "interposer":
        _fail(f"{path}.io_pads", "only the interposer may declare top-level I/O pads")
    ids = [p.id for p in pads]
    if len(ids) != len(set(ids)):
        _fail(f"{path}.io_pads", "I/O pad IDs must be unique")
    keys = [p.key for p in pads]
    if len(keys) != len(set(keys)):
        _fail(f"{path}.io_pads", "I/O pad (id, pin) endpoint tuples must be unique")
    mapped_conductors = {m.technology_layer for m in maps
                         if m.technology_layer in technology.conductors}
    for pad in pads:
        if pad.technology_layer not in mapped_conductors:
            _fail(f"{path}.io_pads",
                  f"I/O pad {pad.id!r} uses unmapped conductor {pad.technology_layer!r}")
    return Component(component_id, role, configured, resolved, top_cell, place, maps, pads)


def _parse_terminal(value: Any, path: str, components: Dict[str, Component],
                    technology: Technology) -> Terminal:
    data = _mapping(value, path)
    _keys(data, path, {"id", "component", "pin", "technology_layer", "position_um", "size_um"})
    component_id = _string(data["component"], f"{path}.component")
    if component_id not in components:
        _fail(f"{path}.component", f"unknown component {component_id!r}")
    if components[component_id].role != "die":
        _fail(f"{path}.component", "explicit terminal windows are die-side only")
    layer = _string(data["technology_layer"], f"{path}.technology_layer")
    if layer not in technology.conductors:
        _fail(f"{path}.technology_layer", "a terminal must use a conductor layer")
    if layer not in {m.technology_layer for m in components[component_id].layers}:
        _fail(f"{path}.technology_layer", f"component does not map layer {layer!r}")
    return Terminal(
        _string(data["id"], f"{path}.id"),
        component_id,
        _string(data["pin"], f"{path}.pin"),
        layer,
        _point_dbu(data["position_um"], f"{path}.position_um", technology.grid_um),
        _size_dbu(data["size_um"], f"{path}.size_um", technology.grid_um),
    )


def _parse_endpoint(value: Any, path: str) -> Endpoint:
    data = _mapping(value, path)
    _keys(data, path, {"component", "pin"}, {"layer"})
    layer = data.get("layer")
    if layer is not None:
        layer = _string(layer, f"{path}.layer")
    return Endpoint(_string(data["component"], f"{path}.component"),
                    _string(data["pin"], f"{path}.pin"), layer)


def decode_resolved_v2(path: Path, *, enforce_profile: bool = True) -> ResolvedDocument:
    """Decode and validate one resolved v2 document.

    With ``enforce_profile`` the entry's profile restrictions are applied on top
    of the nominal v2 semantics (identity carrier placement, empty internal
    equivalences, all uncertainty bounds zero, one terminal per (die, pin)).
    Without it the decoder answers the nominal v2 question only, which is what
    the frozen-parser differential compares against.
    """
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise Usage(f"resolved document is not a file: {path}")
    raw, raw_bytes = load_strict_document(source_path)
    data = _mapping(raw, "document")
    _keys(data, "document",
          {"schema_version", "assembly", "technology", "contracts", "uncertainty", "provenance"})
    version = _string(data["schema_version"], "schema_version")
    if version != RESOLVED_SCHEMA_VERSION:
        _fail("schema_version", f"expected {RESOLVED_SCHEMA_VERSION!r}, got {version!r}")
    technology = _parse_technology(data["technology"])

    assembly = _mapping(data["assembly"], "assembly")
    _keys(assembly, "assembly", {"id", "units", "interface", "components"})
    assembly_id = _string(assembly["id"], "assembly.id")
    if not OUTPUT_STEM_RE.fullmatch(assembly_id):
        _fail("assembly.id", f"{assembly_id!r} is not usable as an output file stem: one path "
              "component of [A-Za-z0-9_.-], 1 to 64 characters, starting alphanumeric")
    units = _string(assembly["units"], "assembly.units")
    if units != "um":
        _fail("assembly.units", "v2 resolved coordinates must use 'um'")
    interface = _mapping(assembly["interface"], "assembly.interface")
    _keys(interface, "assembly.interface", {"kind", "plane"})
    kind = _string(interface["kind"], "assembly.interface.kind")
    plane = _string(interface["plane"], "assembly.interface.plane")
    if kind != "2.5d-single" or plane != "xy":
        _fail("assembly.interface",
              "v2 supports exactly one 2.5D interface with kind='2.5d-single', plane='xy'")
    components = tuple(_parse_component(item, i, source_path.parent, technology)
                       for i, item in enumerate(_sequence(assembly["components"],
                                                          "assembly.components")))
    if not components:
        _fail("assembly.components", "must not be empty")
    ids = [c.id for c in components]
    if len(ids) != len(set(ids)):
        _fail("assembly.components", "component IDs must be unique")
    interposers = [c for c in components if c.role == "interposer"]
    dies = [c for c in components if c.role == "die"]
    if len(interposers) != 1 or not dies:
        _fail("assembly.components", "v2 requires exactly one interposer and at least one die")
    pad_ids = [p.id for c in components for p in c.io_pads]
    collisions = sorted(set(ids) & set(pad_ids))
    if collisions:
        _fail("assembly.components", f"I/O pad IDs collide with component IDs: {collisions}")
    if len(pad_ids) != len(set(pad_ids)):
        _fail("assembly.components", "I/O pad IDs must be globally unique")
    by_id = {c.id: c for c in components}

    contracts = _mapping(data["contracts"], "contracts")
    _keys(contracts, "contracts",
          {"interface_contacts", "terminals", "internal_equivalences", "logical_nets"})
    contacts: List[InterfaceContact] = []
    for i, item in enumerate(_sequence(contracts["interface_contacts"],
                                       "contracts.interface_contacts")):
        cpath = f"contracts.interface_contacts[{i}]"
        contact = _mapping(item, cpath)
        _keys(contact, cpath, {"id", "layers", "roles"})
        layers = _string_list(contact["layers"], f"{cpath}.layers", unique=False)
        roles = _string_list(contact["roles"], f"{cpath}.roles")
        if len(layers) != 2 or len(roles) != 2:
            _fail(cpath, "layers and roles must each contain exactly two entries")
        if any(layer not in technology.conductors for layer in layers):
            _fail(f"{cpath}.layers", "interface contacts may reference only conductors")
        if set(roles) != {"die", "interposer"}:
            _fail(f"{cpath}.roles", "a v2 interface contact must join die and interposer")
        contacts.append(InterfaceContact(_string(contact["id"], f"{cpath}.id"),
                                         ((roles[0], layers[0]), (roles[1], layers[1]))))
    if not contacts:
        _fail("contracts.interface_contacts", "must not be empty")
    contact_ids = [c.id for c in contacts]
    if len(contact_ids) != len(set(contact_ids)):
        _fail("contracts.interface_contacts", "contact IDs must be unique")
    # Every declared contact is inspected, in either authored role order: a die-side
    # conductor pairs with exactly one interposer-side conductor, so a second
    # declaration (DIE_CONTACT -> TM1 beside DIE_CONTACT -> TM2, or the same pairing
    # repeated in reversed order) cannot hide behind a first-match lookup.
    partners: Dict[str, List[str]] = {}
    for contact in contacts:
        partners.setdefault(contact.layer_of("die"), []).append(contact.layer_of("interposer"))
    for die_layer, carrier_layers in sorted(partners.items()):
        if len(carrier_layers) != 1:
            _fail("contracts.interface_contacts",
                  f"die-side conductor {die_layer!r} is declared in {len(carrier_layers)} contacts "
                  f"(interposer side {carrier_layers}); one contact per die-side conductor")
    interposer_mapped = {m.technology_layer for m in interposers[0].layers}
    for i, contact in enumerate(contacts):
        cpath = f"contracts.interface_contacts[{i}]"
        if contact.layer_of("interposer") not in interposer_mapped:
            _fail(f"{cpath}.layers",
                  f"interposer-side conductor {contact.layer_of('interposer')!r} is not "
                  "mapped on the interposer")
    die_contact_layers = {c.layer_of("die") for c in contacts}

    terminals = tuple(_parse_terminal(item, f"contracts.terminals[{i}]", by_id, technology)
                      for i, item in enumerate(_sequence(contracts["terminals"],
                                                         "contracts.terminals")))
    for t in terminals:
        if t.technology_layer not in die_contact_layers:
            _fail("contracts.terminals",
                  f"terminal {t.id!r} must select a dedicated die-side interface-contact layer")
    terminal_ids = [t.id for t in terminals]
    if len(terminal_ids) != len(set(terminal_ids)):
        _fail("contracts.terminals", "terminal IDs must be unique")
    reserved = sorted(t for t in terminal_ids if t.startswith(IO_TERMINAL_NAMESPACE))
    if reserved:
        _fail("contracts.terminals",
              f"terminal IDs use reserved namespace {IO_TERMINAL_NAMESPACE!r}: {reserved}")

    equivalences = _sequence(contracts["internal_equivalences"],
                             "contracts.internal_equivalences")
    if equivalences:
        _fail("contracts.internal_equivalences",
              "profile: v1 reads no component certificate; internal_equivalences must be empty")

    # Die contact layers: exactly one dedicated declared conductor per die, mapped once.
    die_contact: Dict[str, Tuple[str, Tuple[int, int]]] = {}
    for die in dies:
        mapped = [m for m in die.layers if m.technology_layer in die_contact_layers]
        if len(mapped) != 1:
            _fail(f"assembly.components[{ids.index(die.id)}].layers",
                  f"die {die.id!r} must map exactly one die-side interface-contact conductor, "
                  f"maps {[m.technology_layer for m in mapped]}")
        die_contact[die.id] = (mapped[0].technology_layer, mapped[0].source)
    for t in terminals:
        if t.technology_layer != die_contact[t.component][0]:
            _fail("contracts.terminals",
                  f"terminal {t.id!r} uses {t.technology_layer!r}, not the die-contact "
                  f"layer {die_contact[t.component][0]!r} of {t.component!r}")

    # Port identities: one terminal per (die, pin) while equivalences are empty.
    by_port: Dict[Tuple[str, str], List[Terminal]] = {}
    for t in terminals:
        by_port.setdefault(t.key, []).append(t)
    for port, group in by_port.items():
        if len(group) > 1:
            _fail("contracts.terminals",
                  f"port {port!r} is carried by {len(group)} terminal geometries "
                  f"({[t.id for t in group]}); a multi-geometry port needs an internal "
                  "equivalence, which this profile does not read")
    io_pads = tuple(p for c in components for p in c.io_pads)
    available: Dict[Tuple[str, str], str] = {port: group[0].technology_layer
                                             for port, group in by_port.items()}
    collisions = sorted(set(available) & {p.key for p in io_pads})
    if collisions:
        _fail("contracts.terminals", f"terminal keys collide with I/O pads: {collisions}")
    available.update({p.key: p.technology_layer for p in io_pads})

    nets: List[LogicalNet] = []
    owner: Dict[Tuple[str, str], str] = {}
    for i, item in enumerate(_sequence(contracts["logical_nets"], "contracts.logical_nets")):
        npath = f"contracts.logical_nets[{i}]"
        net = _mapping(item, npath)
        _keys(net, npath, {"name", "connections"}, {"class", "external"})
        name = _string(net["name"], f"{npath}.name")
        connections = tuple(_parse_endpoint(e, f"{npath}.connections[{j}]")
                            for j, e in enumerate(_sequence(net["connections"],
                                                            f"{npath}.connections")))
        if not connections:
            _fail(f"{npath}.connections", "must not be empty")
        for j, endpoint in enumerate(connections):
            epath = f"{npath}.connections[{j}]"
            if endpoint.key not in available:
                _fail(epath, f"endpoint {endpoint.component!r}/{endpoint.pin!r} has no "
                             "physical terminal contract")
            previous = owner.get(endpoint.key)
            if previous is not None:
                _fail(epath, f"endpoint is already assigned to logical net {previous!r}")
            if endpoint.layer is not None and endpoint.layer != available[endpoint.key]:
                _fail(f"{epath}.layer", "declared layer does not match terminal layer "
                                        f"{available[endpoint.key]!r}")
            owner[endpoint.key] = name
        net_class = net.get("class")
        if net_class is not None:
            net_class = _string(net_class, f"{npath}.class")
        external = net.get("external")
        if external is not None:
            external = _boolean(external, f"{npath}.external")
        nets.append(LogicalNet(name, connections, net_class, external))
    if not nets:
        _fail("contracts.logical_nets", "must not be empty")
    names = [n.name for n in nets]
    if len(names) != len(set(names)):
        _fail("contracts.logical_nets", "logical net names must be unique")
    unused = sorted(set(available) - set(owner))
    if unused:
        _fail("contracts.logical_nets",
              "physical terminals missing from logical partition: "
              f"{[f'{c}/{p}' for c, p in unused]}")

    unc = _mapping(data["uncertainty"], "uncertainty")
    _keys(unc, "uncertainty", {"model", "component_translation_um"})
    model = _string(unc["model"], "uncertainty.model")
    if model != "bounded_linf_translation":
        _fail("uncertainty.model", "v2 supports only 'bounded_linf_translation'")
    bounds = _mapping(unc["component_translation_um"], "uncertainty.component_translation_um")
    unknown = sorted(bounds.keys() - set(ids))
    missing = sorted(set(ids) - bounds.keys())
    if unknown:
        _fail("uncertainty.component_translation_um", f"unknown components {unknown}")
    if missing:
        _fail("uncertainty.component_translation_um", f"missing components {missing}")
    uncertainty: Dict[str, Decimal] = {}
    for c in components:
        upath = f"uncertainty.component_translation_um.{c.id}"
        bound = _decimal(bounds[c.id], upath)
        _grid_units(bound, technology.grid_um, upath)
        if bound < 0:
            _fail(upath, "must be non-negative")
        uncertainty[c.id] = bound

    prov = _mapping(data["provenance"], "provenance")
    _keys(prov, "provenance", {"generator"}, {"source", "source_revision"})
    provenance = {
        "generator": _string(prov["generator"], "provenance.generator"),
        "source": None if prov.get("source") is None
        else _string(prov["source"], "provenance.source"),
        "source_revision": None if prov.get("source_revision") is None
        else _string(prov["source_revision"], "provenance.source_revision"),
    }

    if enforce_profile:
        interposer = interposers[0]
        if interposer.placement.rotation_deg != 0 or interposer.placement.mirror_x:
            _fail("assembly.components[interposer].placement",
                  "profile: the carrier must be placed at identity (rotation_deg 0, "
                  "mirror_x false)")
        nonzero = sorted(cid for cid, b in uncertainty.items() if b != 0)
        if nonzero:
            _fail("uncertainty.component_translation_um",
                  f"profile: nominal entry; non-zero translation bounds on {nonzero}")
        carrier_layers = sorted({c.layer_of("interposer") for c in contacts})
        if len(carrier_layers) != 1:
            _fail("contracts.interface_contacts",
                  f"profile: one interposer contact conductor, declared {carrier_layers}")

    return ResolvedDocument(
        schema_version=version, assembly_id=assembly_id, units=units,
        interface={"kind": kind, "plane": plane}, components=components,
        technology=technology, contacts=tuple(contacts), terminals=terminals,
        nets=tuple(nets), uncertainty_model=model, uncertainty=uncertainty,
        provenance=provenance, source_path=source_path,
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(), die_contact=die_contact,
    )


# --------------------------------------------------------------------------- #
# Sidecar                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class DieSpec:
    method: str
    technology: str
    thickness: Decimal
    width: Decimal
    height: Decimal
    layout: str
    top_cell: str


@dataclass
class Sidecar:
    path: Path
    sha256: str
    document: Path
    dies_root: Path
    binds_document: str
    binds_layouts: Dict[str, str]
    binds_die_layouts: Dict[str, str]
    binds_methods_manifest: str
    binds_adapter_deck: str
    interposer_adapter: str
    interposer_technology: str
    attachment_surface_z: Decimal
    interposer_position_z: Decimal
    interposer_thickness: Decimal
    attachment_layers: Dict[str, Tuple[int, int]]
    io_class: Optional[str]
    layer_names: Dict[str, str]
    interconnect_adapter: str
    methods_manifest: str
    interconnect_technology: Optional[Dict[str, Any]]
    technologies: Dict[str, Dict[str, Any]]
    components: Dict[str, DieSpec]
    expect_chiplet: Optional[Path]
    derived_from: Optional[Dict[str, Dict[str, str]]] = None
    expect_chiplet_sha256: Optional[str] = None
    derived_paths: Optional[Dict[str, Path]] = None


def _positive_decimal(value: Any, path: str) -> Decimal:
    result = _decimal(value, path)
    if result <= 0:
        _fail(path, "must be greater than zero")
    return result


def _layer_pair(value: Any, path: str) -> Tuple[int, int]:
    pair = _sequence(value, path)
    if len(pair) != 2:
        _fail(path, "expected [GDS layer, datatype]")
    layer = _integer(pair[0], f"{path}[0]")
    datatype = _integer(pair[1], f"{path}[1]")
    if not 0 <= layer <= 65535 or not 0 <= datatype <= 65535:
        _fail(path, "GDS layer and datatype must be in [0, 65535]")
    return (layer, datatype)


def _plain_json(value: Any, path: str) -> Any:
    """Convert a strictly parsed value (Decimals) to plain JSON-able Python."""
    if isinstance(value, Decimal):
        return _json_number(value)
    if isinstance(value, dict):
        return {k: _plain_json(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_json(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _json_number(value: Decimal) -> Any:
    """Render a Decimal as a JSON number: int when integral, else the nearest
    double. The exact value stays in the DBU integers beside it."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def load_sidecar(path: Path) -> Sidecar:
    path = Path(path)
    if not path.is_file():
        raise Usage(f"sidecar is not a file: {path}")
    path = path.resolve()
    raw_bytes = path.read_bytes()
    data = _mapping(load_strict_json(raw_bytes.decode("utf-8")), "sidecar")
    _keys(data, "sidecar",
          {"schema", "version", "document", "dies_root", "binds", "interposer",
           "interconnect", "technologies", "components"},
          {"expect_chiplet", "derived_from"})
    if data["schema"] != SIDECAR_SCHEMA:
        _fail("sidecar.schema", f"expected {SIDECAR_SCHEMA!r}")
    if data["version"] != SIDECAR_VERSION:
        _fail("sidecar.version", f"expected {SIDECAR_VERSION!r}")
    base = path.parent
    document = (base / _string(data["document"], "sidecar.document")).resolve()
    _regular_file(document, "sidecar.document")
    dies_root = (base / _string(data["dies_root"], "sidecar.dies_root")).resolve()
    if not dies_root.is_dir():
        _fail("sidecar.dies_root", f"not a directory: {dies_root}")

    binds = _mapping(data["binds"], "sidecar.binds")
    _keys(binds, "sidecar.binds",
          {"document", "layouts", "die_layouts", "methods_manifest", "adapter_deck"})
    binds_layouts = {k: _sha256_text(v, f"sidecar.binds.layouts.{k}")
                     for k, v in _mapping(binds["layouts"], "sidecar.binds.layouts").items()}
    binds_dies = {k: _sha256_text(v, f"sidecar.binds.die_layouts.{k}")
                  for k, v in _mapping(binds["die_layouts"],
                                       "sidecar.binds.die_layouts").items()}

    inter = _mapping(data["interposer"], "sidecar.interposer")
    _keys(inter, "sidecar.interposer",
          {"adapter", "technology", "attachment_surface_z", "position_z", "thickness",
           "attachment_layers"},
          {"io_class", "layer_names"})
    layers_raw = _mapping(inter["attachment_layers"], "sidecar.interposer.attachment_layers")
    _keys(layers_raw, "sidecar.interposer.attachment_layers", ATTACHMENT_ROLES)
    attachment_layers = {role: _layer_pair(layers_raw[role],
                                           f"sidecar.interposer.attachment_layers.{role}")
                         for role in ATTACHMENT_ROLES}
    if len(set(attachment_layers.values())) != len(ATTACHMENT_ROLES):
        _fail("sidecar.interposer.attachment_layers", "the five roles must name five layers")
    io_class = inter.get("io_class")
    if io_class is not None:
        io_class = _string(io_class, "sidecar.interposer.io_class")
    layer_names = {k: _string(v, f"sidecar.interposer.layer_names.{k}")
                   for k, v in _mapping(inter.get("layer_names", {}),
                                        "sidecar.interposer.layer_names").items()}

    ixn = _mapping(data["interconnect"], "sidecar.interconnect")
    _keys(ixn, "sidecar.interconnect", {"adapter", "methods_manifest"}, {"technology"})
    ixn_tech = ixn.get("technology")
    if ixn_tech is not None:
        ixn_tech = _plain_json(_mapping(ixn_tech, "sidecar.interconnect.technology"),
                               "sidecar.interconnect.technology")

    technologies: Dict[str, Dict[str, Any]] = {}
    for name, tech in _mapping(data["technologies"], "sidecar.technologies").items():
        tpath = f"sidecar.technologies.{name}"
        tech = _mapping(tech, tpath)
        if "dbu" not in tech:
            _fail(tpath, "missing required key 'dbu'")
        _positive_decimal(tech["dbu"], f"{tpath}.dbu")
        technologies[name] = _plain_json(tech, tpath)
        technologies[name]["dbu"] = _decimal(tech["dbu"], f"{tpath}.dbu")

    components: Dict[str, DieSpec] = {}
    for cid, spec in _mapping(data["components"], "sidecar.components").items():
        cpath = f"sidecar.components.{cid}"
        spec = _mapping(spec, cpath)
        _keys(spec, cpath, {"method", "technology", "thickness", "dimensions", "layout",
                            "top_cell"})
        dims = _mapping(spec["dimensions"], f"{cpath}.dimensions")
        _keys(dims, f"{cpath}.dimensions", {"width", "height"})
        technology = _string(spec["technology"], f"{cpath}.technology")
        if technology not in technologies:
            _fail(f"{cpath}.technology", f"unknown technology {technology!r}")
        components[cid] = DieSpec(
            method=_string(spec["method"], f"{cpath}.method"),
            technology=technology,
            thickness=_positive_decimal(spec["thickness"], f"{cpath}.thickness"),
            width=_positive_decimal(dims["width"], f"{cpath}.dimensions.width"),
            height=_positive_decimal(dims["height"], f"{cpath}.dimensions.height"),
            layout=_portable_relative_path(spec["layout"], f"{cpath}.layout"),
            top_cell=_string(spec["top_cell"], f"{cpath}.top_cell"),
        )
    if not components:
        _fail("sidecar.components", "must not be empty")
    if _string(inter["technology"], "sidecar.interposer.technology") not in technologies:
        _fail("sidecar.interposer.technology", "unknown technology")

    expect = data.get("expect_chiplet")
    expect_path = None
    expect_sha = None
    if expect is not None:
        expect_path = _regular_file((base / _string(expect, "sidecar.expect_chiplet")).resolve(),
                                    "sidecar.expect_chiplet")
        expect_sha = sha256_of(expect_path)   # bound here; the round trip re-reads and compares
    # Derivation-input identities (written by derive_attachment_sidecar.py): the accepted
    # .chiplet and the pillar manifest by path and sha256. Both files are resolved relative
    # to the sidecar, opened and authenticated against the recorded sha256 (a recorded
    # identity that does not match its bytes, or a missing file, is a refusal); the chiplet
    # identity must be the expect_chiplet file's. Both enter the closure through binds.
    derived = data.get("derived_from")
    derived_from = None
    derived_paths = None
    if derived is not None:
        derived = _mapping(derived, "sidecar.derived_from")
        _keys(derived, "sidecar.derived_from", {"chiplet", "pillars"})
        derived_from = {}
        derived_paths = {}
        for key in ("chiplet", "pillars"):
            item = _mapping(derived[key], f"sidecar.derived_from.{key}")
            _keys(item, f"sidecar.derived_from.{key}", {"path", "sha256"})
            rel = _string(item["path"], f"sidecar.derived_from.{key}.path")
            recorded = _sha256_text(item["sha256"], f"sidecar.derived_from.{key}.sha256")
            resolved = _regular_file((base / rel).resolve(), f"sidecar.derived_from.{key}.path")
            _read_bound(resolved, recorded, f"sidecar.derived_from.{key}")
            derived_from[key] = {"path": rel, "sha256": recorded}
            derived_paths[key] = resolved
        if expect_sha is not None and derived_from["chiplet"]["sha256"] != expect_sha:
            _fail("sidecar.derived_from.chiplet", "sha256 differs from the expect_chiplet file: "
                  "the derivation input is not the round-trip target")

    return Sidecar(
        path=path, sha256=hashlib.sha256(raw_bytes).hexdigest(), document=document,
        dies_root=dies_root,
        binds_document=_sha256_text(binds["document"], "sidecar.binds.document"),
        binds_layouts=binds_layouts, binds_die_layouts=binds_dies,
        binds_methods_manifest=_sha256_text(binds["methods_manifest"],
                                            "sidecar.binds.methods_manifest"),
        binds_adapter_deck=_sha256_text(binds["adapter_deck"], "sidecar.binds.adapter_deck"),
        interposer_adapter=_string(inter["adapter"], "sidecar.interposer.adapter"),
        interposer_technology=inter["technology"],
        attachment_surface_z=_positive_decimal(inter["attachment_surface_z"],
                                               "sidecar.interposer.attachment_surface_z"),
        interposer_position_z=_decimal(inter["position_z"], "sidecar.interposer.position_z"),
        interposer_thickness=_positive_decimal(inter["thickness"],
                                               "sidecar.interposer.thickness"),
        attachment_layers=attachment_layers, io_class=io_class, layer_names=layer_names,
        interconnect_adapter=_string(ixn["adapter"], "sidecar.interconnect.adapter"),
        methods_manifest=_portable_relative_path(ixn["methods_manifest"],
                                                 "sidecar.interconnect.methods_manifest"),
        interconnect_technology=ixn_tech, technologies=technologies, components=components,
        expect_chiplet=expect_path, derived_from=derived_from,
        expect_chiplet_sha256=expect_sha, derived_paths=derived_paths,
    )


# --------------------------------------------------------------------------- #
# Adapter deck (data, never executed) and the interconnect methods manifest    #
# --------------------------------------------------------------------------- #
_DECK_POLYGONS = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*polygons\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*(?:#.*)?$")
_DECK_ATTACH = re.compile(
    r"^\s*chiplet_attachment_input\s*=\s*([A-Za-z_]\w*)\.and\(\s*([A-Za-z_]\w*)\s*\)"
    r"\.join\(\s*([A-Za-z_]\w*)\.and\(\s*([A-Za-z_]\w*)\s*\)\s*\)\s*(?:#.*)?$")


def parse_attachment_deck(text: str) -> Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Read the attachment layer pairs literally from an interposer adapter deck.

    Returns ``{"pillar": (opening, dfpad), "sbump": (opening, dfpad)}`` from the
    ``chiplet_attachment_input = A.and(B).join(C.and(D))`` line and the
    ``name = polygons(l, d)`` definitions it names. Any other shape is refused.
    """
    named: Dict[str, Tuple[int, int]] = {}
    attach: List[Tuple[str, str, str, str]] = []
    for line in text.splitlines():
        m = _DECK_POLYGONS.match(line)
        if m:
            named[m.group(1)] = (int(m.group(2)), int(m.group(3)))
            continue
        m = _DECK_ATTACH.match(line)
        if m:
            attach.append(m.groups())
    if len(attach) != 1:
        _fail("adapter_deck", "expected exactly one 'chiplet_attachment_input = "
                              f"A.and(B).join(C.and(D))' line, found {len(attach)}")
    names = attach[0]
    unknown = [n for n in names if n not in named]
    if unknown:
        _fail("adapter_deck", f"attachment line names undefined polygons {unknown}")
    pillar = (named[names[0]], named[names[1]])
    sbump = (named[names[2]], named[names[3]])
    if len({*pillar, *sbump}) != 4:
        _fail("adapter_deck", "the pillar and sbump branches must name four distinct layers")
    return {"pillar": pillar, "sbump": sbump}


@dataclass
class Method:
    id: str
    description: str
    adapter: str
    body_diameter_um: Decimal
    passiv_opening_um: Decimal
    tm2_enclosure_um: Decimal
    ixn_spacing: Decimal
    ixn_pitch: Decimal
    stack_layers: List[Dict[str, Any]]

    @property
    def stack_height(self) -> Decimal:
        return sum((layer["height"] for layer in self.stack_layers), Decimal(0))


#: The newest methods-registry minor this bridge has been CHECKED AGAINST.
#:
#: Not a floor and not a pin, and the distinction is easy to get wrong because
#: `additive_minor` and the word "floor" are both used around this artifact. It
#: sets no minimum: a lower same-major minor reads silently, so raising this
#: number refuses nothing that a lower one accepted. All it moves is where the
#: "newer than I know" warning starts. Measured, not inferred:
#: check_contract_version("1.0", "1.1") is a silent accept, ("1.2", "1.1")
#: warns, and only a different major refuses.
#:
#: So the reason this still says 1.0 is not that 1.1 would be rejected; it is
#: that nobody here has read the 1.1 shape yet. Raise it after looking at the
#: manifest, and the only effect is that a 1.1 registry stops warning on every
#: run, which is a warning nobody can act on.
#:
#: What the code used to do was demand an exact "1.0", which read a MINOR as a
#: MAJOR and refused a compatible manifest outright, contradicting the
#: `additive_minor` rule its own ci/contract-registry.json row declares.
SUPPORTED_METHODS_SCHEMA_VERSION = "1.0"


def load_methods_manifest(path: Path, needed: Iterable[str], adapter: str,
                          raw: Optional[bytes] = None,
                          on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Method]:
    """``raw`` are the bytes already bound by the caller; when given they are parsed
    instead of a second read of ``path`` (hash the bytes actually parsed).

    The schema version goes through the shared policy rather than an equality
    test: same major at or below the checked minor is silent, a higher minor is
    accepted and reported, any other major is refused. Only the MAJOR refuses;
    an older minor is read without a word. ``on_warn`` receives the
    higher-minor report undeduped; with none supplied it still reaches
    ``warnings``, so an accepted-but-newer manifest is never silent.
    """
    if raw is None:
        raw = path.read_bytes()
    data = _mapping(load_strict_json(raw.decode("utf-8")), "methods_manifest")
    if "schema_version" not in data:
        _fail("methods_manifest.schema_version", "missing required key")
    try:
        cfio.check_contract_version(
            data["schema_version"], SUPPORTED_METHODS_SCHEMA_VERSION,
            name="methods_manifest", on_warn=on_warn)
    except cfio.ContractVersionError as exc:
        # Keep the bridge's own refusal shape: callers catch Refusal, not a
        # reader exception, and an escaping ContractVersionError would exit
        # differently from every other malformed-input path in this file.
        _fail("methods_manifest.schema_version",
              str(exc).split(": ", 1)[-1])
    methods = _mapping(data.get("methods"), "methods_manifest.methods")
    result: Dict[str, Method] = {}
    for mid in sorted(set(needed)):
        mpath = f"methods_manifest.methods.{mid}"
        if mid not in methods:
            _fail(mpath, "method not in the manifest")
        m = _mapping(methods[mid], mpath)
        for key in ("description", "adapter", "body_diameter_um", "fab_params", "pitch_rules",
                    "connection_stack"):
            if key not in m:
                _fail(mpath, f"missing required key {key!r}")
        if _string(m["adapter"], f"{mpath}.adapter") != adapter:
            _fail(f"{mpath}.adapter", f"method belongs to adapter {m['adapter']!r}, the run "
                                      f"binds {adapter!r}")
        fab = _mapping(m["fab_params"], f"{mpath}.fab_params")
        pitch = _mapping(m["pitch_rules"], f"{mpath}.pitch_rules")
        stack = _mapping(m["connection_stack"], f"{mpath}.connection_stack")
        layers_raw = _sequence(stack.get("layers"), f"{mpath}.connection_stack.layers")
        if not layers_raw:
            _fail(f"{mpath}.connection_stack.layers", "must not be empty")
        layers: List[Dict[str, Any]] = []
        for i, layer in enumerate(layers_raw):
            lpath = f"{mpath}.connection_stack.layers[{i}]"
            layer = _mapping(layer, lpath)
            _keys(layer, lpath, {"name", "material", "height", "diameter"})
            layers.append({
                "name": _string(layer["name"], f"{lpath}.name"),
                "material": _string(layer["material"], f"{lpath}.material"),
                "height": _positive_decimal(layer["height"], f"{lpath}.height"),
                "diameter": _positive_decimal(layer["diameter"], f"{lpath}.diameter"),
            })
        result[mid] = Method(
            id=mid, description=_string(m["description"], f"{mpath}.description"),
            adapter=m["adapter"],
            body_diameter_um=_positive_decimal(m["body_diameter_um"], f"{mpath}.body_diameter_um"),
            passiv_opening_um=_positive_decimal(fab.get("passiv_opening_um"),
                                                f"{mpath}.fab_params.passiv_opening_um"),
            tm2_enclosure_um=_positive_decimal(fab.get("tm2_enclosure_um"),
                                               f"{mpath}.fab_params.tm2_enclosure_um"),
            ixn_spacing=_positive_decimal(pitch.get("IXN_spacing"),
                                          f"{mpath}.pitch_rules.IXN_spacing"),
            ixn_pitch=_positive_decimal(pitch.get("IXN_pitch"), f"{mpath}.pitch_rules.IXN_pitch"),
            stack_layers=layers,
        )
        if result[mid].ixn_pitch <= result[mid].passiv_opening_um:
            _fail(f"{mpath}.pitch_rules", "IXN_pitch must exceed IXN_pad_size "
                                          "(passiv_opening_um)")
    return result


# --------------------------------------------------------------------------- #
# Frames                                                                       #
# --------------------------------------------------------------------------- #
def orient(x: int, y: int, rotation_deg: int, mirror_x: bool) -> Tuple[int, int]:
    """Resolved v2 orientation: mirror (y -> -y) first, then quarter turns."""
    if mirror_x:
        y = -y
    for _ in range(rotation_deg // 90):
        x, y = -y, x
    return x, y


def orient_legacy(x: int, y: int, rotation_deg: int, flip: bool) -> Tuple[int, int]:
    """ADK legacy orientation: mirror (x -> -x) first, then quarter turns."""
    if flip:
        x = -x
    for _ in range(rotation_deg // 90):
        x, y = -y, x
    return x, y


@dataclass(frozen=True)
class DieFrame:
    die_id: str
    pos_d: Tuple[int, int]
    anchor_d: Tuple[int, int]
    theta_d: int
    mirror_d: bool
    pos_i: Tuple[int, int]
    anchor_i: Tuple[int, int]
    corner: Tuple[int, int]

    def c(self, p: Tuple[int, int]) -> Tuple[int, int]:
        ox, oy = orient(p[0] - self.anchor_d[0], p[1] - self.anchor_d[1],
                        self.theta_d, self.mirror_d)
        return (self.pos_d[0] + ox - self.pos_i[0] + self.anchor_i[0] - self.corner[0],
                self.pos_d[1] + oy - self.pos_i[1] + self.anchor_i[1] - self.corner[1])

    def g(self, p: Tuple[int, int]) -> Tuple[int, int]:
        cx, cy = self.c(p)
        return (cx + self.corner[0], cy + self.corner[1])

    def a(self, p: Tuple[int, int]) -> Tuple[int, int]:
        ox, oy = orient(p[0] - self.anchor_d[0], p[1] - self.anchor_d[1],
                        self.theta_d, self.mirror_d)
        return (self.pos_d[0] + ox, self.pos_d[1] + oy)

    @property
    def legacy_rotation(self) -> int:
        return (self.theta_d + 180) % 360 if self.mirror_d else self.theta_d

    @property
    def orientation(self) -> str:
        return "flip_chip" if self.mirror_d else "face_up"

    def legacy(self, p: Tuple[int, int]) -> Tuple[int, int]:
        """The ADK legacy chain from origin_um = g(0): origin + R(legacy) M_x^m p."""
        ox, oy = orient_legacy(p[0], p[1], self.legacy_rotation, self.mirror_d)
        g0 = self.g((0, 0))
        return (g0[0] + ox, g0[1] + oy)

    def box_g(self, box: "db.Box") -> "db.Box":
        p1 = self.g((box.left, box.bottom))
        p2 = self.g((box.right, box.top))
        return db.Box(min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))

    def as_record(self) -> Dict[str, Any]:
        return {
            "die": self.die_id, "pos_d": list(self.pos_d), "anchor_d": list(self.anchor_d),
            "theta_d": self.theta_d, "mirror_d": self.mirror_d, "pos_i": list(self.pos_i),
            "anchor_i": list(self.anchor_i), "corner": list(self.corner),
            "legacy_rotation_deg": self.legacy_rotation, "orientation": self.orientation,
            "c_origin": list(self.c((0, 0))), "g_origin": list(self.g((0, 0))),
            "klayout_trans": {"rot": self.theta_d // 90, "mirror": self.mirror_d,
                              "disp": list(self.g((0, 0)))},
        }


# --------------------------------------------------------------------------- #
# Layout helpers (klayout.db)                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class LayoutView:
    path: Path
    sha256: str
    layout: Any
    cell: Any


def _require_klayout() -> None:
    if db is None:
        raise Usage("klayout.db is required (pip install klayout)")


def _layout_dbu(layout: Any) -> Decimal:
    return Decimal(repr(layout.dbu))


def open_layout(path: Path, grid: Decimal, top_cell: str, where: str,
                expected: Optional[str] = None) -> LayoutView:
    """Open a layout by path (KLayout reads from a path, not from bytes), binding the
    bytes actually read: the file is hashed immediately before and after the read and
    both hashes must equal each other and, when given, the bound ``expected`` sha256."""
    _require_klayout()
    before = sha256_of(path)
    if expected is not None and before != expected:
        _fail(where, f"layout {path.name} is {before[:12]}..., bound {expected[:12]}...")
    layout = db.Layout()
    try:
        layout.read(str(path))
    except Exception as exc:  # noqa: BLE001 - klayout raises RuntimeError
        _fail(where, f"cannot read layout {path}: {exc}")
    after = sha256_of(path)
    if after != before:
        _fail(where, f"layout {path.name} changed while being read ({before[:12]}... to {after[:12]}...)")
    dbu = _layout_dbu(layout)
    if dbu != grid:
        _fail(where, f"layout {path.name} has dbu {dbu}, technology.grid_um is {grid}; "
                     "no rescale is performed")
    cell = layout.cell(top_cell)
    if cell is None:
        _fail(where, f"cell {top_cell!r} not found in {path.name}")
    return LayoutView(path, before, layout, cell)


def _layer_index(layout: Any, pair: Tuple[int, int]) -> Optional[int]:
    return layout.find_layer(pair[0], pair[1])


def region_of(view: LayoutView, pair: Tuple[int, int], merged: bool = True) -> Any:
    index = _layer_index(view.layout, pair)
    if index is None:
        return db.Region()
    region = db.Region(view.cell.begin_shapes_rec(index))
    return region.merged() if merged else region


def texts_of(view: LayoutView, pair: Tuple[int, int]) -> List[Tuple[str, Tuple[int, int]]]:
    index = _layer_index(view.layout, pair)
    if index is None:
        return []
    result = []
    it = view.cell.begin_shapes_rec(index)
    while not it.at_end():
        shape = it.shape()
        if shape.is_text():
            pos = it.trans().trans(shape.text_pos)
            result.append((shape.text_string, (pos.x, pos.y)))
        it.next()
    return result


def inside(inner: Any, outer: Any) -> bool:
    """``inner`` covered by ``outer`` (shared edges allowed)."""
    inner_region = inner if isinstance(inner, db.Region) else db.Region(inner)
    return (inner_region - outer).is_empty()


def strictly_inside(inner: Any, outer: Any) -> bool:
    """``inner`` lies in the interior of ``outer``: covered by it and sharing no boundary
    point (exact on integer coordinates). No clearance is implied: a slanted boundary at
    a positive sub-DBU distance is still strictly inside."""
    inner_region = inner if isinstance(inner, db.Region) else db.Region(inner)
    outer_region = outer if isinstance(outer, db.Region) else db.Region(outer)
    if not (inner_region - outer_region).is_empty():
        return False
    return inner_region.edges().interacting(outer_region.edges()).count() == 0


def point_strictly_inside(point: Tuple[int, int], polygon: Any) -> bool:
    """The point lies in the interior of the polygon: inside and on no hull or hole edge
    (exact on integer coordinates; no clearance implied, the same interior semantics as
    :func:`strictly_inside`)."""
    pt = db.Point(point[0], point[1])
    poly = polygon if isinstance(polygon, db.Polygon) else db.Polygon(polygon)
    if any(edge.contains(pt) for edge in poly.each_edge()):
        return False
    return bool(poly.inside(pt))


def point_symmetric(polygon: Any) -> bool:
    centre = polygon.bbox().center()
    rotated = polygon.transformed(db.Trans(2, False, 2 * centre.x, 2 * centre.y))
    return (db.Region(polygon) ^ db.Region(rotated)).is_empty()


def _square_centre(polygon: Any, where: str) -> Tuple[int, int]:
    box = polygon.bbox()
    if box.width() != box.height():
        _fail(where, f"bbox {box} is not square")
    if (box.left + box.right) % 2 or (box.bottom + box.top) % 2:
        _fail(where, f"bbox {box} has no integer centre")
    return ((box.left + box.right) // 2, (box.bottom + box.top) // 2)


# --------------------------------------------------------------------------- #
# Attachment bodies on the carrier                                             #
# --------------------------------------------------------------------------- #
@dataclass
class Body:
    centre: Tuple[int, int]
    polygons: Dict[str, Any]              # role -> Polygon
    diameters: Dict[str, int]             # role -> bbox extent in DBU
    vertices: Dict[str, int]              # role -> vertex count
    enclosure_edge_pairs: Optional[int] = None
    tm2_polygons: int = 0
    dfpad_strictly_inside_contact: Optional[bool] = None
    terminal: Optional[str] = None
    die: Optional[str] = None
    pin: Optional[str] = None
    method: Optional[str] = None

    def predicates(self) -> Dict[str, Any]:
        """The authored profile predicates beside the literal physical predicates.

        ``profile`` is what profile 1.0.0 binds (concentric shapes with the exact
        bbox-diameter relation to the method manifest; the whole dfpad covered by
        one merged contact conductor, shared boundaries allowed). ``literal`` is
        the outcome of the literal reading measured on the same body and recorded as
        data: a Euclidean ``enclosing_check`` at the manifest enclosure and a
        strict (no shared edge) containment in the contact conductor. Neither
        literal value is bound, and neither is reported as a clearance.
        """
        return {
            "profile": {
                "bbox_diameter_relation_exact": True,
                "dfpad_covered_by_contact": True,
                "contact_polygons_met": self.tm2_polygons,
            },
            "literal": {
                "enclosing_check_edge_pairs_at_manifest_value": self.enclosure_edge_pairs,
                "enclosing_check_empty_at_manifest_value":
                    None if self.enclosure_edge_pairs is None else self.enclosure_edge_pairs == 0,
                "dfpad_strictly_inside_contact": self.dfpad_strictly_inside_contact,
            },
        }


def extract_bodies(carrier: LayoutView, attachment: Dict[str, Tuple[int, int]],
                   deck_pairs: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]],
                   contact_pair: Tuple[int, int]) -> Dict[Tuple[int, int], Body]:
    """Enumerate every attachment body on the carrier with its five shapes."""
    if deck_pairs["pillar"] != (attachment["opening"], attachment["dfpad"]):
        _fail("sidecar.interposer.attachment_layers",
              f"opening/dfpad {attachment['opening']}/{attachment['dfpad']} differ from the "
              f"adapter deck's pillar branch {deck_pairs['pillar']}")
    sb_open, sb_pad = deck_pairs["sbump"]
    sbump = region_of(carrier, sb_open) & region_of(carrier, sb_pad)
    if not sbump.is_empty():
        _fail("carrier", f"sbump branch {sb_open}/{sb_pad} is not empty ({sbump.count()} "
                         "shapes); profile v1 binds the pillar branch only")
    regions = {role: region_of(carrier, attachment[role]) for role in ATTACHMENT_ROLES}
    attach = (regions["opening"] & regions["dfpad"]).merged()
    by_role: Dict[str, Dict[Tuple[int, int], Any]] = {}
    for role, region in list(regions.items()) + [("attach", attach)]:
        table: Dict[Tuple[int, int], Any] = {}
        for polygon in region.each():
            where = f"carrier.{role}@{polygon.bbox()}"
            if polygon.holes() != 0:
                _fail(where, f"shape has {polygon.holes()} hole(s)")
            centre = _square_centre(polygon, where)
            if not point_symmetric(polygon):
                _fail(where, "shape is not point-symmetric about its centre")
            if centre in table:
                _fail(where, f"two {role} shapes share the centre {centre}")
            table[centre] = polygon
        by_role[role] = table
    centres = set(by_role["attach"])
    for role in ATTACHMENT_ROLES:
        if set(by_role[role]) != centres:
            missing = sorted(centres - set(by_role[role]))[:3]
            extra = sorted(set(by_role[role]) - centres)[:3]
            _fail(f"carrier.{role}", f"centre set differs from the attachment set "
                                     f"(count {len(by_role[role])} vs {len(centres)}; "
                                     f"missing {missing}, extra {extra})")
    tm2 = region_of(carrier, contact_pair)
    bodies: Dict[Tuple[int, int], Body] = {}
    for centre in sorted(centres):
        polys = {role: by_role[role][centre] for role in ATTACHMENT_ROLES}
        where = f"carrier.body@{centre}"
        opening, dfpad, body, mark = polys["opening"], polys["dfpad"], polys["body"], polys["mark"]
        dfpad_region = db.Region(dfpad)
        if not strictly_inside(opening, dfpad_region):
            _fail(where, "opening is not strictly inside its dfpad")
        if not strictly_inside(body, dfpad_region):
            _fail(where, "body is not strictly inside its dfpad")
        if not strictly_inside(opening, db.Region(body)):
            _fail(where, "opening is not strictly inside its body (pillar does not cover it)")
        if mark.bbox() != dfpad.bbox():
            _fail(where, f"mark bbox {mark.bbox()} differs from dfpad bbox {dfpad.bbox()} "
                         "(corpus-binding expectation)")
        if not inside(dfpad, tm2):
            _fail(where, f"dfpad is not covered by the contact conductor {contact_pair}")
        touching = tm2.interacting(dfpad_region).count()
        if touching != 1:
            _fail(where, f"dfpad meets {touching} conductor polygons on {contact_pair}, "
                         "expected exactly one")
        bodies[centre] = Body(
            centre=centre, polygons=polys,
            diameters={role: polys[role].bbox().width() for role in ATTACHMENT_ROLES},
            vertices={role: polys[role].num_points() for role in ATTACHMENT_ROLES},
            tm2_polygons=touching,
            # literal reading, measured and recorded, not bound (see Body.predicates)
            dfpad_strictly_inside_contact=strictly_inside(dfpad, tm2),
        )
    return bodies


def _units(value: Decimal, grid: Decimal, path: str) -> int:
    return _grid_units(value, grid, path)


def verify_body_method(body: Body, method: Method, grid: Decimal) -> None:
    where = f"carrier.body@{body.centre}"
    opening = _units(method.passiv_opening_um, grid, f"{method.id}.passiv_opening_um")
    enclosure = _units(method.tm2_enclosure_um, grid, f"{method.id}.tm2_enclosure_um")
    diameter = _units(method.body_diameter_um, grid, f"{method.id}.body_diameter_um")
    expected = {"opening": opening, "dfpad": opening + 2 * enclosure, "body": diameter}
    for role, value in expected.items():
        if body.diameters[role] != value:
            _fail(where, f"{role} extent {body.diameters[role]} DBU differs from the method "
                         f"{method.id!r} value {value} DBU")
    # The profile binds the nominal bbox-diameter relation to the method manifest on
    # concentric shapes; it enforces neither a regular 256-gon geometry nor a numerical
    # local-clearance bound. The literal physical predicate (a Euclidean
    # enclosing_check at the manifest enclosure) is measured on every body and recorded
    # as data beside it; on a faceted body its edge-pair count is non-zero because a
    # polygon's apothem is below its circumradius (docs/resolved_v2_entry.md).
    body.enclosure_edge_pairs = db.Region(body.polygons["dfpad"]).enclosing_check(
        db.Region(body.polygons["opening"]), enclosure).count()


# --------------------------------------------------------------------------- #
# Correspondence                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class DieView:
    die: Component
    frame: DieFrame
    adk: LayoutView
    proxy: LayoutView
    pads: List[Any]
    outline: Any
    labels: List[Tuple[str, Tuple[int, int]]]
    proxies: List[Any]
    method: Method
    rows: List[Dict[str, Any]] = field(default_factory=list)


def _one_containing(candidates: List[Tuple[int, Any]], test: Callable[[Any], bool],
                    what: str, where: str) -> int:
    hits = [i for i, poly in candidates if test(poly)]
    if len(hits) != 1:
        _fail(where, f"{what}: expected exactly one host, found {len(hits)}")
    return hits[0]


def correspond(view: DieView, doc: ResolvedDocument, bodies: Dict[Tuple[int, int], Body],
               pad_pair: Tuple[int, int]) -> None:
    """Build and check the eight correspondence maps for one die."""
    die_id = view.die.id
    frame = view.frame
    terminals = doc.terminals_of(die_id)
    where = f"die.{die_id}"
    if len(view.outline) != 1:
        _fail(where, f"outline layer carries {len(view.outline)} polygons, expected one")
    if view.outline[0].holes() != 0:
        # the boundary export writes the hull only; a holed outline is refused, not filled
        _fail(where, f"outline polygon carries {view.outline[0].holes()} holes; the "
                     "single-outline profile exports the hull and refuses holes")
    outline = db.Region(view.outline[0])
    for i, pad in enumerate(view.pads):
        if not strictly_inside(pad, outline):
            _fail(where, f"pad {i} at {pad.bbox()} is not strictly inside the outline")
    n = len(terminals)
    for name, count in (("pads", len(view.pads)), ("labels", len(view.labels)),
                        ("proxies", len(view.proxies))):
        if count != n:
            _fail(where, f"{count} {name} for {n} terminals; every die pad must carry a terminal")
    pads = list(enumerate(view.pads))
    proxies = list(enumerate(view.proxies))
    for i, proxy in proxies:
        pwhere = f"{where}.proxy[{i}]"
        if proxy.holes() != 0:
            _fail(pwhere, "proxy has holes")
        _square_centre(proxy, pwhere)
        if not point_symmetric(proxy):
            _fail(pwhere, "proxy is not point-symmetric")
    # P -> L
    label_host: Dict[int, int] = {}
    pad_labels: Dict[int, List[int]] = {i: [] for i, _ in pads}
    for li, (text, pos) in enumerate(view.labels):
        hosts = [i for i, pad in pads if pad.bbox().contains(db.Point(*pos))
                 and point_strictly_inside(pos, pad)]
        if len(hosts) != 1:
            _fail(where, f"label {text!r} at {pos} is strictly inside {len(hosts)} pads")
        label_host[li] = hosts[0]
        pad_labels[hosts[0]].append(li)
    for i, labels in pad_labels.items():
        if len(labels) != 1:
            _fail(where, f"pad {i} at {view.pads[i].bbox()} hosts {len(labels)} labels")
    used_pads: Dict[int, str] = {}
    used_proxies: Dict[int, str] = {}
    used_bodies: Dict[Tuple[int, int], str] = {}
    vertex_counts: Dict[str, Dict[str, int]] = {}
    for t in terminals:
        twhere = f"{where}.terminal[{t.id}]"
        hx, hy = t.size[0] // 2, t.size[1] // 2
        window = db.Box(t.position[0] - hx, t.position[1] - hy,
                        t.position[0] + hx, t.position[1] + hy)
        centre = db.Point(*t.position)
        pad_i = _one_containing([(i, p) for i, p in pads if p.bbox().contains(centre)],
                                lambda p: strictly_inside(window, db.Region(p)),
                                "window strictly inside a pad (T -> P)", twhere)
        if pad_i in used_pads:
            _fail(twhere, f"pad {pad_i} already hosts terminal {used_pads[pad_i]!r}")
        used_pads[pad_i] = t.id
        label_i = pad_labels[pad_i][0]
        label_text, label_pos = view.labels[label_i]
        if label_text != t.pin:
            _fail(twhere, f"label {label_text!r} on the hosting pad differs from pin {t.pin!r}")
        proxy_i = _one_containing([(i, p) for i, p in proxies if p.bbox().contains(centre)],
                                  lambda p: strictly_inside(window, db.Region(p)),
                                  "window strictly inside a proxy (T -> X)", twhere)
        if proxy_i in used_proxies:
            _fail(twhere, f"proxy {proxy_i} already hosts terminal {used_proxies[proxy_i]!r}")
        used_proxies[proxy_i] = t.id
        g_window = frame.box_g(window)
        g_centre = frame.g(t.position)
        body = bodies.get(g_centre)
        if body is None:
            _fail(twhere, f"window centre maps to {g_centre} in G(int), which is no body centre")
        if body.terminal is not None:
            _fail(twhere, f"body at {g_centre} already hosts terminal {body.terminal!r}")
        opening_hosts = [c for c, b in bodies.items()
                         if b.polygons["opening"].bbox().contains(db.Point(*g_centre))
                         and strictly_inside(g_window, db.Region(b.polygons["opening"]))]
        if len(opening_hosts) != 1:
            _fail(twhere, f"mapped window {g_window} is strictly inside {len(opening_hosts)} "
                          "openings (T -> O)")
        if opening_hosts[0] != g_centre:
            _fail(twhere, f"T -> O -> B gives {opening_hosts[0]}, T -> B gives {g_centre}")
        proxy_box = frame.box_g(view.proxies[proxy_i].bbox())
        if proxy_box != body.polygons["body"].bbox():
            _fail(twhere, f"proxy bbox mapped to G(int) {proxy_box} differs from the body bbox "
                          f"{body.polygons['body'].bbox()} (X -> B)")
        verify_body_method(body, view.method, doc.technology.grid_um)
        counts = vertex_counts.setdefault(view.method.id, dict(body.vertices))
        if counts != body.vertices:
            _fail(twhere, f"vertex counts {body.vertices} differ from other bodies of "
                          f"{view.method.id!r} {counts}")
        body.terminal, body.die, body.pin, body.method = t.id, die_id, t.pin, view.method.id
        used_bodies[g_centre] = t.id
        view.rows.append({
            "terminal": t.id, "die": die_id, "pin": t.pin,
            "window_local_dbu": [window.left, window.bottom, window.right, window.top],
            "window_g_dbu": [g_window.left, g_window.bottom, g_window.right, g_window.top],
            "pad_index": pad_i, "pad_bbox_dbu": _box_list(view.pads[pad_i].bbox()),
            "label": {"text": label_text, "position_dbu": list(label_pos)},
            "proxy_index": proxy_i,
            "proxy_bbox_g_dbu": _box_list(proxy_box),
            "opening_centre_g_dbu": list(opening_hosts[0]),
            "body_centre_g_dbu": list(g_centre),
            "body_centre_c_dbu": [g_centre[0] - frame.corner[0], g_centre[1] - frame.corner[1]],
            "method": view.method.id,
            "diameters_dbu": dict(body.diameters),
            "vertices": dict(body.vertices),
            "enclosure_edge_pairs_at_manifest_value": body.enclosure_edge_pairs,
            "predicates": body.predicates(),
        })
    if len(used_pads) != n or len(used_proxies) != n or len(used_bodies) != n:
        _fail(where, "correspondence is not a bijection")


def _box_list(box: Any) -> List[int]:
    return [box.left, box.bottom, box.right, box.top]


# --------------------------------------------------------------------------- #
# Carrier preservation                                                         #
# --------------------------------------------------------------------------- #
def _layer_pairs(layout: Any) -> List[Tuple[int, int]]:
    result = []
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        result.append((info.layer, info.datatype))
    return sorted(result)


def _layer_stats(view_cell: Any, layout: Any, pair: Tuple[int, int]) -> Dict[str, Any]:
    index = layout.find_layer(pair[0], pair[1])
    if index is None:
        return {"shapes": 0, "texts": 0, "area_dbu2": 0, "bbox_dbu": None}
    shapes = texts = 0
    area = 0
    it = view_cell.begin_shapes_rec(index)
    while not it.at_end():
        shape = it.shape()
        if shape.is_text():
            texts += 1
        else:
            shapes += 1
            area += shape.polygon.transformed(it.trans()).area()
        it.next()
    region = db.Region(view_cell.begin_shapes_rec(index))
    box = region.bbox()
    return {"shapes": shapes, "texts": texts, "area_dbu2": area,
            "bbox_dbu": None if box.empty() else _box_list(box)}


def copy_carrier(source: LayoutView, target_layout: Any, cell_name: str) -> Any:
    """Copy the source top cell flattened into ``target_layout`` under ``cell_name``,
    every layer and every shape kind (boxes, paths, polygons, texts) with its
    accumulated transformation."""
    cell = target_layout.create_cell(cell_name)
    for pair in _layer_pairs(source.layout):
        src_index = source.layout.find_layer(pair[0], pair[1])
        dst_index = target_layout.layer(db.LayerInfo(pair[0], pair[1]))
        target = cell.shapes(dst_index)
        it = source.cell.begin_shapes_rec(src_index)
        while not it.at_end():
            target.insert(it.shape(), it.trans())
            it.next()
    return cell


def prove_preservation(source: LayoutView, carrier_cell: Any, carrier_layout: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {"layers": {}, "bbox_source_dbu": _box_list(source.cell.bbox()),
                              "bbox_carrier_dbu": _box_list(carrier_cell.bbox())}
    if source.cell.bbox() != carrier_cell.bbox():
        _fail("carrier", f"carrier bbox {carrier_cell.bbox()} differs from the source "
                         f"{source.cell.bbox()}")
    src_layers = {pair for pair in _layer_pairs(source.layout)
                  if _layer_stats(source.cell, source.layout, pair)["shapes"]
                  or _layer_stats(source.cell, source.layout, pair)["texts"]}
    dst_layers = {pair for pair in _layer_pairs(carrier_layout)
                  if _layer_stats(carrier_cell, carrier_layout, pair)["shapes"]
                  or _layer_stats(carrier_cell, carrier_layout, pair)["texts"]}
    if src_layers != dst_layers:
        _fail("carrier", f"layer set differs: source {sorted(src_layers)} carrier "
                         f"{sorted(dst_layers)}")
    for pair in sorted(src_layers):
        s = _layer_stats(source.cell, source.layout, pair)
        d = _layer_stats(carrier_cell, carrier_layout, pair)
        src_region = db.Region(source.cell.begin_shapes_rec(
            source.layout.find_layer(pair[0], pair[1]))).merged()
        dst_region = db.Region(carrier_cell.begin_shapes_rec(
            carrier_layout.find_layer(pair[0], pair[1]))).merged()
        xor = (src_region ^ dst_region)
        entry = {"source": s, "carrier": d, "xor_area_dbu2": xor.area(),
                 "merged_area_dbu2": src_region.area()}
        record["layers"][f"{pair[0]}/{pair[1]}"] = entry
        if s != d or not xor.is_empty():
            _fail("carrier", f"layer {pair[0]}/{pair[1]} not preserved: {entry}")
    return record


# --------------------------------------------------------------------------- #
# The entry                                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class EntryResult:
    out_dir: Path
    manifest: Dict[str, Any]
    outputs: Dict[str, Path]


def _die_z(sidecar: Sidecar, method: Method) -> Decimal:
    return sidecar.interposer_position_z + sidecar.attachment_surface_z + method.stack_height


def _um(value: int, grid: Decimal) -> Decimal:
    return Decimal(value) * grid


def _yaml_number(value: Decimal) -> float:
    return float(value)


def _write_json(path: Path, data: Any) -> str:
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bind(expected: str, actual_path: Path, where: str) -> str:
    actual = sha256_of(actual_path)
    if actual != expected:
        _fail(where, f"sidecar binds {expected[:12]}..., file {actual_path.name} is {actual[:12]}...")
    return actual


def _read_bound(path: Path, expected: Optional[str], where: str) -> Tuple[bytes, str]:
    """Read ``path`` once; the returned bytes are the bytes that get parsed, and their
    sha256 is what is recorded (and compared with ``expected`` when bound)."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        _fail(where, f"sidecar binds {expected[:12]}..., file {path.name} is {digest[:12]}...")
    return raw, digest


def run_entry(sidecar_path: Path, out_dir: Path) -> EntryResult:
    """Run the entry; refusals raise :class:`Refusal` and leave ``out_dir`` absent."""
    _require_klayout()
    out_dir = Path(out_dir)
    if out_dir.exists():
        _fail("out", f"output directory exists: {out_dir} (a fresh directory is required)")
    sidecar = load_sidecar(sidecar_path)
    doc = decode_resolved_v2(sidecar.document)
    grid = doc.technology.grid_um
    # Numeric value, not spelling: 0.0010 and 1E-3 are the same power of ten as 0.001.
    if grid.normalize().as_tuple().digits != (1,):
        _fail("technology.grid_um", f"{grid} is not a power of ten")

    # Bindings: document, layouts, methods manifest, deck, ADK die layouts.
    binds: Dict[str, Any] = {"document": {"path": str(doc.source_path), "sha256": doc.source_sha256},
                             "sidecar": {"path": str(sidecar.path), "sha256": sidecar.sha256}}
    if sidecar.expect_chiplet is not None:
        binds["expect_chiplet"] = {"path": str(sidecar.expect_chiplet), "sha256": sidecar.expect_chiplet_sha256}
    for key, resolved in (sidecar.derived_paths or {}).items():
        binds[f"derived_from.{key}"] = {"path": str(resolved), "sha256": sidecar.derived_from[key]["sha256"]}
    if doc.source_sha256 != sidecar.binds_document:
        _fail("sidecar.binds.document", f"sidecar binds {sidecar.binds_document[:12]}..., "
                                        f"document is {doc.source_sha256[:12]}...")
    if set(sidecar.binds_layouts) != {c.id for c in doc.components}:
        _fail("sidecar.binds.layouts", f"inventory {sorted(sidecar.binds_layouts)} must name "
                                       f"exactly the components {sorted(c.id for c in doc.components)}")
    if set(sidecar.binds_die_layouts) != {d.id for d in doc.dies}:
        _fail("sidecar.binds.die_layouts", "inventory must name exactly the dies")
    if set(sidecar.components) != {d.id for d in doc.dies}:
        _fail("sidecar.components", f"{sorted(sidecar.components)} must name exactly the dies "
                                    f"{sorted(d.id for d in doc.dies)}")
    for c in doc.components:
        _bind(sidecar.binds_layouts[c.id], c.layout_resolved, f"sidecar.binds.layouts.{c.id}")
        binds[f"layout.{c.id}"] = {"path": str(c.layout_resolved),
                                   "sha256": sidecar.binds_layouts[c.id]}

    try:
        adapter_id = adk_registry.validate_adapter_id(sidecar.interposer_adapter)
        interposer_res = adk_registry.resolve_adapter(adapter_id)
        ixn_id = adk_registry.validate_adapter_id(sidecar.interconnect_adapter)
        ixn_res = adk_registry.resolve_adapter(ixn_id)
        ixn_root = adk_registry.resolve_root("INTERCONNECT_PDK")
    except adk_registry.RegistryError:
        raise
    deck_path = _regular_file(interposer_res.value, "sidecar.interposer.adapter")
    deck_raw, _ = _read_bound(deck_path, sidecar.binds_adapter_deck, "sidecar.binds.adapter_deck")
    deck_pairs = parse_attachment_deck(deck_raw.decode("utf-8"))
    binds["adapter_deck"] = {"path": str(deck_path), "sha256": sidecar.binds_adapter_deck,
                             "registry_source": str(interposer_res.source)}
    manifest_path = _in_root_file(ixn_root.value, sidecar.methods_manifest,
                                  "sidecar.interconnect.methods_manifest")
    manifest_raw, _ = _read_bound(manifest_path, sidecar.binds_methods_manifest,
                                  "sidecar.binds.methods_manifest")
    binds["methods_manifest"] = {"path": str(manifest_path),
                                 "sha256": sidecar.binds_methods_manifest,
                                 "registry_root": str(ixn_root.value)}
    methods = load_methods_manifest(manifest_path, [s.method for s in sidecar.components.values()],
                                    ixn_id, raw=manifest_raw)

    for name, tech in sidecar.technologies.items():
        if tech["dbu"] != grid:
            _fail(f"sidecar.technologies.{name}.dbu", f"{tech['dbu']} differs from grid_um {grid}")

    # Carrier: geometry-preserved copy; die layouts: ADK views and proxies.
    interposer = doc.interposer
    carrier_src = open_layout(interposer.layout_resolved, grid, interposer.top_cell,
                              "assembly.components[interposer].layout",
                              expected=sidecar.binds_layouts[interposer.id])
    corner_box = carrier_src.cell.bbox()
    if corner_box.empty():
        _fail("carrier", "interposer top cell is empty")
    corner = (corner_box.left, corner_box.bottom)
    contact_layer_tm2 = None
    contacts_record = []
    for contact in doc.contacts:
        contacts_record.append({"id": contact.id, "pairs": [list(p) for p in contact.pairs]})
    interposer_maps = {m.technology_layer: m.source for m in interposer.layers}

    out_layout = db.Layout()
    out_layout.dbu = float(grid)
    if _layout_dbu(out_layout) != grid:
        _fail("out", f"assembled layout dbu {_layout_dbu(out_layout)} is not {grid}")
    if doc.assembly_id == interposer.top_cell:
        _fail("assembly.id", "must differ from the interposer top cell name")
    top = out_layout.create_cell(doc.assembly_id)
    carrier_cell = copy_carrier(carrier_src, out_layout, interposer.top_cell)
    preservation = prove_preservation(carrier_src, carrier_cell, out_layout)
    top.insert(db.CellInstArray(carrier_cell.cell_index(), db.Trans()))

    pads_path = ADK_ROOT / "config" / "chiplet_pads.json"
    pads_raw, pads_sha = _read_bound(pads_path, None, "config.chiplet_pads")   # one read: parsed == hashed
    pads_cfg = json.loads(pads_raw.decode("utf-8"))["layers"]
    binds["config.chiplet_pads"] = {"path": str(pads_path), "sha256": pads_sha}
    pad_pair = (pads_cfg["pad_drawing"]["gds_layer"], pads_cfg["pad_drawing"]["gds_datatype"])
    text_pair = (pads_cfg["pad_text"]["gds_layer"], pads_cfg["pad_text"]["gds_datatype"])
    outline_pair = (pads_cfg["outline"]["gds_layer"], pads_cfg["outline"]["gds_datatype"])

    views: List[DieView] = []
    die_cells: Dict[Tuple[str, str], Any] = {}
    frames: List[DieFrame] = []
    for die in doc.dies:
        spec = sidecar.components[die.id]
        adk_path = _in_root_file(sidecar.dies_root, spec.layout, f"sidecar.components.{die.id}.layout")
        _bind(sidecar.binds_die_layouts[die.id], adk_path, f"sidecar.binds.die_layouts.{die.id}")
        binds[f"die_layout.{die.id}"] = {"path": str(adk_path),
                                         "sha256": sidecar.binds_die_layouts[die.id]}
        adk = open_layout(adk_path, grid, spec.top_cell, f"sidecar.components.{die.id}",
                          expected=sidecar.binds_die_layouts[die.id])
        proxy = open_layout(die.layout_resolved, grid, die.top_cell,
                            f"assembly.components[{die.id}].layout",
                            expected=sidecar.binds_layouts[die.id])
        contact_layer, contact_source = doc.die_contact[die.id]
        matching = [c for c in doc.contacts if c.layer_of("die") == contact_layer]
        if len(matching) != 1:   # already refused by the decoder; never a first-match lookup
            _fail("contracts.interface_contacts",
                  f"{len(matching)} contacts declare die-side conductor {contact_layer!r}")
        contact_of_die = matching[0]
        tm2_layer = contact_of_die.layer_of("interposer")
        tm2_pair = interposer_maps[tm2_layer]
        if contact_layer_tm2 is None:
            contact_layer_tm2 = tm2_pair
        elif contact_layer_tm2 != tm2_pair:
            _fail("contracts.interface_contacts", "profile: one interposer contact conductor")
        frame = DieFrame(die.id, die.placement.position, die.placement.anchor,
                         die.placement.rotation_deg, die.placement.mirror_x,
                         interposer.placement.position, interposer.placement.anchor, corner)
        frames.append(frame)
        outline = list(region_of(adk, outline_pair).each())
        views.append(DieView(
            die=die, frame=frame, adk=adk, proxy=proxy,
            pads=list(region_of(adk, pad_pair).each()), outline=outline,
            labels=texts_of(adk, text_pair),
            proxies=list(region_of(proxy, contact_source).each()),
            method=methods[spec.method],
        ))
        key = (adk.sha256, spec.top_cell)
        if key not in die_cells:
            cell = out_layout.create_cell(spec.top_cell)
            cell.copy_tree(adk.cell)
            die_cells[key] = cell
        trans = db.Trans(frame.theta_d // 90, frame.mirror_d, db.Vector(*frame.g((0, 0))))
        top.insert(db.CellInstArray(die_cells[key].cell_index(), trans))

    bodies = extract_bodies(carrier_src, sidecar.attachment_layers, deck_pairs, contact_layer_tm2)
    for view in views:
        correspond(view, doc, bodies, pad_pair)
    unused = sorted(c for c, b in bodies.items() if b.terminal is None)
    if unused:
        _fail("carrier", f"{len(unused)} attachment bodies host no terminal, first {unused[:3]}")
    # Method-wide vertex-count invariant across all dies (the per-die check inside
    # correspond is die-local): every body of one method has the same vertex counts.
    per_method: Dict[str, Dict[str, int]] = {}
    for centre, body in sorted(bodies.items()):
        counts = per_method.setdefault(body.method, dict(body.vertices))
        if body.vertices != counts:
            _fail("carrier", f"body at {centre} ({body.die}.{body.pin}) has vertex counts "
                             f"{body.vertices}; other bodies of method {body.method!r} across "
                             f"all dies have {counts}")

    # Boundaries, with the transform/polygon agreement control.
    boundaries = []
    outline_records = {}
    for view in views:
        frame = view.frame
        poly = view.outline[0]
        pts = [(p.x, p.y) for p in poly.each_point_hull()]
        mapped = [frame.g(p) for p in pts]
        legacy = [frame.legacy(p) for p in pts]
        if mapped != legacy:
            _fail(f"die.{view.die.id}", "legacy transform reconstruction disagrees with c(p)")
        box = poly.bbox()
        spec = sidecar.components[view.die.id]
        width_units = _units(spec.width, grid, f"sidecar.components.{view.die.id}.dimensions.width")
        height_units = _units(spec.height, grid, f"sidecar.components.{view.die.id}.dimensions.height")
        if (box.width(), box.height()) != (width_units, height_units):
            _fail(f"sidecar.components.{view.die.id}.dimensions",
                  f"outline {box.width()}x{box.height()} DBU is not the declared "
                  f"{width_units}x{height_units} DBU; a die's declared size is its outline")
        outline_records[view.die.id] = {
            "outline_dbu": [box.width(), box.height()],
            "declared_dbu": [width_units, height_units],
            "difference_dbu": [width_units - box.width(), height_units - box.height()],
        }
        g0 = frame.g((0, 0))
        boundaries.append({
            "instance": view.die.id, "source_die": spec.top_cell, "cell": spec.top_cell,
            "class": "chiplet",
            "transform": {"origin_um": [_json_number(_um(g0[0], grid)), _json_number(_um(g0[1], grid))],
                          "rotation_deg": frame.legacy_rotation, "mirror_x": frame.mirror_d,
                          "magnification": 1},
            "polygon_dbu": [list(p) for p in mapped],
            "polygon_um": [[_json_number(_um(p[0], grid)), _json_number(_um(p[1], grid))]
                           for p in mapped],
        })

    # Write the assembled GDS, re-read it, run the instance read-back control.
    out_dir.mkdir(parents=True)
    try:
        return _write_outputs(out_dir, sidecar, doc, methods, views, bodies, frames, boundaries,
                              outline_records, preservation, binds, out_layout, top, carrier_src,
                              interposer_res, ixn_res, ixn_root, pad_pair, die_cells,
                              contacts_record, outline_pair)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


def _output_name(out_dir: Path, name: str) -> Path:
    """``out_dir / name`` for a plain file name; anything that would resolve outside
    ``out_dir`` (a separator, ``.`` or ``..``, an empty name) is a refusal. Second gate
    behind the assembly.id stem rule so no output can be written outside a fresh out_dir."""
    if not name or name in {".", ".."} or Path(name).name != name or not OUTPUT_NAME_RE.fullmatch(name):
        _fail("out", f"output name {name!r} is not a plain file name inside {out_dir}")
    path = out_dir / name
    if path.parent != out_dir:
        _fail("out", f"output {path} would leave {out_dir}")
    return path


def _write_outputs(out_dir, sidecar, doc, methods, views, bodies, frames, boundaries,
                   outline_records, preservation, binds, out_layout, top, carrier_src,
                   interposer_res, ixn_res, ixn_root, pad_pair, die_cells, contacts_record,
                   outline_pair) -> EntryResult:
    grid = doc.technology.grid_um
    stem = f"{doc.assembly_id}_assembled"
    gds_path = _output_name(out_dir, f"{stem}.gds")
    out_layout.write(str(gds_path))
    gds_sha = sha256_of(gds_path)
    reread = db.Layout()
    reread.read(str(gds_path))
    if _layout_dbu(reread) != grid:
        _fail("out.gds", f"re-read dbu {_layout_dbu(reread)} is not {grid}")
    tops = [c.name for c in reread.top_cells()]
    if tops != [doc.assembly_id]:
        _fail("out.gds", f"re-read top cells {tops}, expected [{doc.assembly_id!r}]")
    die_names = {cell.name for cell in die_cells.values()}
    pad_index = reread.find_layer(pad_pair[0], pad_pair[1])
    observed: List[Tuple[int, int]] = []
    if pad_index is not None:
        it = reread.top_cell().begin_shapes_rec(pad_index)
        while not it.at_end():
            if it.cell().name in die_names and not it.shape().is_text():
                box = it.trans().trans(it.shape().bbox())
                observed.append((box.center().x, box.center().y))
            it.next()
    expected: List[Tuple[int, int]] = []
    for view in views:
        for pad in view.pads:
            expected.append(view.frame.g((pad.bbox().center().x, pad.bbox().center().y)))
    if sorted(observed) != sorted(expected):
        _fail("out.gds", "instance read-back: placed pad centres differ from the integer chain")

    dbu_number = _json_number(grid)
    boundaries_doc = {
        "schema": "adk-boundary-manifest", "version": "1.0.0", "generator": GENERATOR,
        "assembly_gds": gds_path.name, "assembly_gds_sha256": gds_sha,
        "dbu_um": dbu_number, "top_cell": doc.assembly_id, "boundaries": boundaries,
    }
    used_methods: Dict[str, List[str]] = {}
    for view in views:
        used_methods.setdefault(view.method.id, []).append(view.die.id)
    methods_block = {}
    ixn_methods = {}
    for mid, dies in used_methods.items():
        m = methods[mid]
        numbers = {"IXN_spacing": _json_number(m.ixn_spacing), "IXN_pitch": _json_number(m.ixn_pitch),
                   "IXN_pad_size": _json_number(m.passiv_opening_um)}
        methods_block[mid] = dict(numbers)
        ixn_methods[mid] = {"dies": list(dies), **numbers}
    ixn_doc = {"schema": "adk-ixn-methods", "version": "1.0.0", "generator": GENERATOR,
               "assembly_gds": gds_path.name, "source_resolved_document": doc.source_path.name,
               "methods": ixn_methods}
    pillars = []
    for centre in sorted(bodies):
        b = bodies[centre]
        pillars.append({
            "device_ref": b.die, "pin_name": b.pin, "method": b.method,
            "x_um": _json_number(_um(centre[0] - frames[0].corner[0], grid)),
            "y_um": _json_number(_um(centre[1] - frames[0].corner[1], grid)),
            "diameter_um": _json_number(_um(b.diameters["body"], grid)),
            "moved_by_auto_resolve": False, "terminal": b.terminal,
        })
    pillars_doc = {
        "schema": "adk-pillar-manifest", "version": "1.1.0",
        "generator": GENERATOR + " (bodies measured on the carrier; the entry resolves nothing)",
        "assembly_gds": gds_path.name, "units": "um", "methods": methods_block, "pillars": pillars,
    }

    # .chiplet
    corner = frames[0].corner
    carrier_box = carrier_src.cell.bbox()
    width = _um(carrier_box.width(), grid)
    height = _um(carrier_box.height(), grid)
    interposer = doc.interposer
    io_pads = []
    for pad in interposer.io_pads:
        entry = {"id": pad.id}
        if sidecar.io_class is not None:
            entry["io_class"] = sidecar.io_class
        if pad.net is not None:
            entry["net"] = pad.net
        entry["position"] = {"x": _yaml_number(_um(pad.position[0] - corner[0], grid)),
                             "y": _yaml_number(_um(pad.position[1] - corner[1], grid))}
        entry["size"] = {"x": _yaml_number(_um(pad.size[0], grid)),
                         "y": _yaml_number(_um(pad.size[1], grid))}
        entry["layer"] = sidecar.layer_names.get(pad.technology_layer, pad.technology_layer)
        io_pads.append(entry)
    technologies = {}
    for name, tech in sidecar.technologies.items():
        technologies[name] = {k: (_yaml_number(v) if isinstance(v, Decimal) else v)
                              for k, v in tech.items()}
    stacks = {}
    for mid in used_methods:
        m = methods[mid]
        stacks[mid] = {"description": m.description,
                       "layers": [{"name": l["name"], "material": l["material"],
                                   "height": _yaml_number(l["height"]),
                                   "diameter": _yaml_number(l["diameter"])}
                                  for l in m.stack_layers]}
    components = [{
        "id": interposer.id, "type": "interposer", "anchor": "bbox_center",
        "technology": sidecar.interposer_technology,
        "layout": os.path.relpath(interposer.layout_resolved, out_dir).replace(os.sep, "/"),
        "top_cell": interposer.top_cell,
        "dimensions": {"width": _yaml_number(width), "height": _yaml_number(height),
                       "thickness": _yaml_number(sidecar.interposer_thickness)},
        "position": {"x": _yaml_number(width / 2), "y": _yaml_number(height / 2),
                     "z": _yaml_number(sidecar.interposer_position_z)},
        "io_pads": io_pads,
        "attachment_surface_z": _yaml_number(sidecar.attachment_surface_z),
    }]
    z_records = {}
    for view in views:
        spec = sidecar.components[view.die.id]
        z = _die_z(sidecar, view.method)
        c0 = view.frame.c((0, 0))
        z_records[view.die.id] = {
            "interposer_z": str(sidecar.interposer_position_z),
            "attachment_surface_z": str(sidecar.attachment_surface_z),
            "stack_height": str(view.method.stack_height), "z": str(z),
        }
        components.append({
            "id": view.die.id, "type": "die", "anchor": "gds_origin",
            "technology": spec.technology, "connection": view.method.id,
            "layout": os.path.relpath(view.adk.path, out_dir).replace(os.sep, "/"),
            "orientation": view.frame.orientation,
            "dimensions": {"width": _yaml_number(spec.width), "height": _yaml_number(spec.height),
                           "thickness": _yaml_number(spec.thickness)},
            "position": {"x": _yaml_number(_um(c0[0], grid)), "y": _yaml_number(_um(c0[1], grid)),
                         "z": _yaml_number(z)},
            "rotation": {"z": float(view.frame.legacy_rotation)},
            "top_cell": spec.top_cell,
        })
    nets = []
    for net in doc.nets:
        entry: Dict[str, Any] = {"name": net.name}
        if net.net_class is not None:
            entry["class"] = net.net_class
        if net.external is not None:
            entry["external"] = net.external
        entry["connections"] = [{"component": e.component, "pin": e.pin} for e in net.connections]
        nets.append(entry)
    chiplet = {
        "format_version": "1.0",
        "assembly": {"name": doc.assembly_id, "units": "um"},
        "interposer": {"adapter": interposer_res.id},
        "technologies": technologies,
        "connection_stacks": stacks,
        "components": components,
        "interconnect": {"adapter": ixn_res.id},
        "netlist": {"nets": nets},
    }
    if sidecar.interconnect_technology is not None:
        chiplet["interconnect"]["technology"] = sidecar.interconnect_technology
    try:
        chiplet_text = cfio.dumps(chiplet)
    except cfio.ChipletFormatError as exc:
        _fail("out.chiplet", f"vendored reader refuses the emitted document: {exc}")
    chiplet_path = _output_name(out_dir, f"{doc.assembly_id}.chiplet")
    chiplet_path.write_text(chiplet_text, encoding="utf-8")
    try:
        reloaded = cfio.load(chiplet_path)
    except cfio.ChipletFormatError as exc:
        _fail("out.chiplet", f"vendored reader refuses the written file: {exc}")
    mount_z = Decimal(str(reloaded["components"][0]["position"]["z"])) + \
        Decimal(str(reloaded["components"][0]["attachment_surface_z"]))
    for comp in reloaded["components"][1:]:
        stack = reloaded["connection_stacks"][comp["connection"]]["layers"]
        total = sum((Decimal(str(l["height"])) for l in stack), Decimal(0))
        z = Decimal(str(comp["position"]["z"]))
        if z - mount_z != total:
            _fail("out.chiplet", f"die {comp['id']} z identity fails: {z} - {mount_z} != {total}")

    expect_record = None
    if sidecar.expect_chiplet is not None:
        expect_record = compare_expect_chiplet(sidecar.expect_chiplet, reloaded, outline_records,
                                               sidecar.expect_chiplet_sha256)

    boundaries_path = _output_name(out_dir, f"{stem}.boundaries.json")
    ixn_path = _output_name(out_dir, f"{stem}.ixn_methods.json")
    pillars_path = _output_name(out_dir, f"{stem}.pillars.json")
    hashes = {
        "boundaries": _write_json(boundaries_path, boundaries_doc),
        "ixn_methods": _write_json(ixn_path, ixn_doc),
        "pillars": _write_json(pillars_path, pillars_doc),
        "chiplet": sha256_of(chiplet_path),
        "gds": gds_sha,
    }
    manifest = {
        "schema": ENTRY_SCHEMA, "version": ENTRY_VERSION, "generator": GENERATOR,
        "profile": PROFILE, "scope": SCOPE_STATEMENT,
        "adk_revision": _adk_revision(),
        "sidecar": {"path": str(sidecar.path), "sha256": sidecar.sha256},
        "inputs": binds,
        "resolved": {
            "schema_version": doc.schema_version, "assembly_id": doc.assembly_id,
            "technology_id": doc.technology.id, "grid_um": format(grid.normalize(), "f"),
            "source_top_cell": interposer.top_cell,
            "interface_contacts": contacts_record,
            "uncertainty_model": doc.uncertainty_model,
            "uncertainty_bounds_um": {k: str(v) for k, v in doc.uncertainty.items()},
            "all_bounds_zero": all(v == 0 for v in doc.uncertainty.values()),
            "provenance": doc.provenance,
            "counts": {"dies": len(doc.dies), "terminals": len(doc.terminals),
                       "terminals_per_die": {d.id: len(doc.terminals_of(d.id)) for d in doc.dies},
                       "io_pads": len(interposer.io_pads), "logical_nets": len(doc.nets),
                       "endpoints": sum(len(n.connections) for n in doc.nets),
                       "bodies": len(bodies)},
        },
        "adapters": {
            "interposer": {"id": interposer_res.id, "path": str(interposer_res.value),
                           "source": str(interposer_res.source)},
            "interconnect": {"id": ixn_res.id, "path": str(ixn_res.value),
                             "source": str(ixn_res.source)},
            "interconnect_root": str(ixn_root.value),
        },
        "frames": {"corner_dbu": list(corner),
                   "interposer_outline_bbox_dbu": _box_list(
                       region_of(carrier_src, (235, 0)).bbox())
                   if not region_of(carrier_src, (235, 0)).is_empty() else None,
                   "dies": [f.as_record() for f in frames]},
        "outline_vs_declared": outline_records,
        "z": z_records,
        "carrier_preservation": preservation,
        "bodies": [{"centre_g_dbu": list(c), "diameters_dbu": b.diameters, "vertices": b.vertices,
                    "terminal": b.terminal, "die": b.die, "pin": b.pin, "method": b.method,
                    "tm2_polygons": b.tm2_polygons,
                    "enclosure_edge_pairs_at_manifest_value": b.enclosure_edge_pairs,
                    "predicates": b.predicates()}
                   for c, b in sorted(bodies.items())],
        "correspondence": {v.die.id: v.rows for v in views},
        "expect_chiplet": expect_record,
        "derived_from": sidecar.derived_from,
        "outputs": {
            "gds": {"path": gds_path.name, "sha256": hashes["gds"]},
            "boundaries": {"path": boundaries_path.name, "sha256": hashes["boundaries"]},
            "ixn_methods": {"path": ixn_path.name, "sha256": hashes["ixn_methods"]},
            "pillars": {"path": pillars_path.name, "sha256": hashes["pillars"]},
            "chiplet": {"path": chiplet_path.name, "sha256": hashes["chiplet"]},
        },
        "next_commands": [
            f"klayout/drc/run_drc.py --path {gds_path.name} --interposer-adapter "
            f"{interposer_res.id} --interconnect-adapter {ixn_res.id} --interconnect-methods "
            f"{ixn_path.name}",
            f"checks/pads_vs_pillars.py --chiplet {chiplet_path.name} --pillars {pillars_path.name}",
        ],
    }
    # Closure: every consumed input (document, layouts, die layouts, adapter deck, methods
    # manifest, the chiplet_pads config) is hashed again after the outputs exist; a
    # mutation during the run is a refusal and the outputs are discarded by the caller.
    closure = {}
    for name, bind in sorted(binds.items()):
        actual = sha256_of(Path(bind["path"]))
        if actual != bind["sha256"]:
            _fail("binds", f"input {name} ({bind['path']}) changed during the run: bound "
                           f"{bind['sha256'][:12]}..., now {actual[:12]}...")
        closure[name] = actual
    manifest["inputs_rechecked_after_outputs"] = closure
    manifest_path = _output_name(out_dir, "entry_manifest.json")
    _write_json(manifest_path, manifest)
    return EntryResult(out_dir, manifest, {
        "gds": gds_path, "boundaries": boundaries_path, "ixn_methods": ixn_path,
        "pillars": pillars_path, "chiplet": chiplet_path, "manifest": manifest_path})


def _accepted_decimal(comp: Dict[str, Any], section: str, axis: str, cid: str) -> Decimal:
    """``comp[section][axis]`` of the accepted ``.chiplet`` as the decimal of its text.
    The vendored reader checks a component's id and type only, so anything but a
    finite number there, an absent one included, is a refusal naming the path."""
    path = f"sidecar.expect_chiplet.{cid}.{section}.{axis}"
    values = comp.get(section)
    value = values.get(axis) if isinstance(values, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "expected a finite number")
    result = Decimal(str(value))
    if not result.is_finite():
        _fail(path, "expected a finite number")
    return result


def compare_expect_chiplet(expect_path: Path, produced: Dict[str, Any],
                           outline_records: Dict[str, Any],
                           bound_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Round trip against an accepted ``.chiplet``: ids, methods, orientations,
    rotations, positions, stacks, z and each die's width and height must be equal;
    the carrier's dimensions and a die's thickness are recorded, never hidden. The
    file is read once; the bytes parsed are the bytes hashed, and they must still be
    the bytes bound when the sidecar was loaded."""
    raw, digest = _read_bound(expect_path, bound_sha256, "sidecar.expect_chiplet")
    try:
        expected = cfio.loads(raw.decode("utf-8"))
    except cfio.ChipletFormatError as exc:
        _fail("sidecar.expect_chiplet", f"vendored reader refuses it: {exc}")
    exp = {c["id"]: c for c in expected.get("components", [])}
    got = {c["id"]: c for c in produced.get("components", [])}
    if set(exp) != set(got):
        _fail("sidecar.expect_chiplet", f"component ids differ: {sorted(exp)} vs {sorted(got)}")
    diffs = []
    dims = {}
    carrier_top_cell = None
    for cid, e in exp.items():
        g = got[cid]
        keys = ["type", "connection", "orientation", "anchor", "technology", "top_cell"]
        if g.get("type") == "interposer":
            # The carrier top cell is the resolved layout's own name (a variant may
            # rename it); the view is the same, so it is recorded, not compared
            # (source_top_cell in the manifest).
            keys.remove("top_cell")
            carrier_top_cell = {"expected": e.get("top_cell"), "produced": g.get("top_cell")}
        for key in keys:
            if e.get(key) != g.get(key):
                diffs.append(f"{cid}.{key}: {e.get(key)!r} vs {g.get(key)!r}")
        for axis in ("x", "y", "z"):
            if _accepted_decimal(e, "position", axis, cid) != Decimal(str(g["position"][axis])):
                diffs.append(f"{cid}.position.{axis}: {e['position'][axis]} vs {g['position'][axis]}")
        if e.get("rotation") is not None or g.get("rotation") is not None:
            er = Decimal(str((e.get("rotation") or {}).get("z", 0)))
            gr = Decimal(str((g.get("rotation") or {}).get("z", 0)))
            if er != gr:
                diffs.append(f"{cid}.rotation.z: {er} vs {gr}")
        if g.get("type") != "interposer":
            # a die's size is its outline (checked above against 206/0), so an accepted
            # file sized otherwise, e.g. from a courtyard, is not the same assembly
            for axis in ("width", "height"):
                ev = _accepted_decimal(e, "dimensions", axis, cid)
                gv = (g.get("dimensions") or {}).get(axis)
                if ev != Decimal(str(gv)):
                    diffs.append(f"{cid}.dimensions.{axis}: {ev} vs {gv}")
        dims[cid] = {"expected": e.get("dimensions"), "produced": g.get("dimensions")}
    es = expected.get("connection_stacks", {})
    gs = produced.get("connection_stacks", {})
    for mid in gs:
        if mid not in es:
            diffs.append(f"connection_stacks.{mid}: absent in the expected file")
            continue
        el = [(l["name"], l["material"], Decimal(str(l["height"])), Decimal(str(l["diameter"])))
              for l in es[mid]["layers"]]
        gl = [(l["name"], l["material"], Decimal(str(l["height"])), Decimal(str(l["diameter"])))
              for l in gs[mid]["layers"]]
        if el != gl:
            diffs.append(f"connection_stacks.{mid}: {el} vs {gl}")
    if diffs:
        _fail("sidecar.expect_chiplet", "round trip differs: " + "; ".join(diffs))
    return {"path": str(expect_path), "sha256": digest, "equal": True,
            "carrier_top_cell": carrier_top_cell,
            "dimensions": dims, "outline_vs_declared": outline_records}


def _adk_revision() -> Optional[str]:
    head = ADK_ROOT / ".git"
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(ADK_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None
    finally:
        del head


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resolved_v2_entry.py",
        description="ADK-side entry for an assembly-lvs resolved v2 document.")
    parser.add_argument("--sidecar", required=True,
                        help="ADK attachment sidecar (<resolved stem>.adk_attachment.json)")
    parser.add_argument("--out", required=True, help="fresh output directory")
    args = parser.parse_args(argv)
    try:
        result = run_entry(Path(args.sidecar), Path(args.out))
    except Usage as exc:
        print(f"resolved_v2_entry: usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Refusal as exc:
        print(f"resolved_v2_entry: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except adk_registry.RegistryError as exc:
        print(f"resolved_v2_entry: registry: {exc}", file=sys.stderr)
        return adk_registry.exit_code_for(exc)
    for name, path in result.outputs.items():
        print(f"{name}: {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
