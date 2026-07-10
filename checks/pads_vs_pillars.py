# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""pads_vs_pillars -- verify die pad positions against as-drawn pillar positions.

Manifest-level alignment check: for every die of a finalized ``.chiplet``
assembly with a known pad source, the die-local pad centers are transformed
into the canonical interposer GDS-bbox-corner frame (micrometers, y-up --
the frame ``.chiplet`` die positions live in) and compared against the
Cu-pillar/bump centers recorded in the assembly's
``<gds-stem>.pillars.json`` sidecar (the pillar manifest,
``config/schema/pillar_manifest.schema.json``), which the producer emits in
that same frame. The manifest's ``x_um/y_um`` are the as-drawn pillar
centers (post collision auto-resolve, up to the constant frame rebase) and
are authoritative for this check; the assembly GDS remains fab ground truth.

Frame contract (die-local pad -> canonical assembly frame). Pad coordinates
are die-local GDS micrometers, y-up, relative to the die GDS origin (the
``gds_origin`` anchor); the die's ``.chiplet`` ``position`` is where that
origin lands in the canonical GDS-bbox-corner frame and ``rotation.z`` is
counter-clockwise degrees in that y-up frame (arbitrary angles supported).
``flip_chip`` dies are realized in the assembly as an x-mirror of the die
artwork applied BEFORE rotation: the assembly generator places the flipped
die with translate * R(rotation.z) * mirror-x, and the pillar producer
consumes footprint-frame pin lists that already carry that same mirror. This
check therefore maps a die-local pad ``p`` to::

    global = position + R(rotation.z) * M * p

with ``M = diag(-1, 1)`` for ``flip_chip`` and identity for ``face_up``.
``face_down`` is unmapped and a hard error (same policy as chiplet2dbx).

Pad sources per die (KiCad reference, e.g. ``U1``):

  - ``--pins REF=PINS_JSON``: a gds_to_kicad ``*.pins.json`` pin list
    (die-local, unmirrored), loaded via ``chiplet2dbx.load_pinlist``.
  - ``--gds-pads REF``: pads extracted from the die's ``.chiplet`` layout
    GDS: centers of ``pad_drawing`` polygons, names from ``pad_text`` labels
    contained in (or nearest to) each pad. Layer numbers come from
    ``config/chiplet_pads.json``, never hardcoded. Requires the
    ``klayout.db`` Python module.

Matching per device: by ``pin_name`` when both sides are named; entries
where at least one side is unnamed fall back to greedy nearest-unique
matching within tolerance. Findings: ``MISALIGNED`` (named match beyond
tolerance -- except pillars flagged ``moved_by_auto_resolve: true`` **with**
a recorded ``auto_resolve_shift_um`` magnitude, whose deviation is the
producer's own collision auto-resolve shift and demotes to a warning when it
stays within shift + tolerance (beyond that it is MISALIGNED again). A bare
``moved_by_auto_resolve`` boolean with no magnitude cannot bound the excuse
and does not demote -- it is advisory only), ``PAD_WITHOUT_PILLAR``,
``PILLAR_WITHOUT_PAD``,
``AMBIGUOUS_MATCH`` (nearest-unique fallback cannot decide), and -- with
``--strict`` -- ``NO_PAD_SOURCE`` for dies that declare a connection method
but were given no pad source (a warning otherwise).

Exit codes: 0 clean, 1 findings, 2 usage/validation/tooling errors.

Importable entry points:

    load_chiplet(path) -> dict                  (vendored chiplet_format_io)
    load_pillar_manifest(path) -> dict          (validated, exact version pin)
    load_pads_config(path=None) -> dict         (pad layer vocabulary)
    extract_gds_pads(gds_path) -> [{"name", "x_um", "y_um"}, ...]
    transform_pads(component, pads) -> [{"name", "x_um", "y_um"}, ...]
    match_device(ref, pads, pillars) -> (findings, matched_count)
    run_check(assembly, manifest, die_pads) -> report dict
    expand_path_vars(path) -> str               (${VAR} discovery expansion)

New options must be appended as trailing keyword arguments with defaults so
importers stay source-compatible (see docs/integration.md).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ADK_ROOT = Path(__file__).resolve().parents[1]
OPENROAD_DIR = ADK_ROOT / "openroad"
if str(OPENROAD_DIR) not in sys.path:
    sys.path.insert(0, str(OPENROAD_DIR))

# chiplet2dbx puts the vendored chiplet_format_io on sys.path and is the
# documented import home of load_pinlist (reused here, not copied).
import chiplet2dbx  # noqa: E402

import chiplet_format_io as cfio  # noqa: E402

#: Manifest identity: readers pin schema string and EXACT version, mirroring
#: the boundary-manifest policy (docs/pillar_manifest.md, version policy).
PILLAR_MANIFEST_SCHEMA = "adk-pillar-manifest"
SUPPORTED_PILLAR_MANIFEST_VERSION = "1.0.0"

#: Only micrometer manifests/assemblies are supported.
SUPPORTED_UNITS = "um"

#: Default pad-to-pillar distance tolerance in micrometers.
DEFAULT_TOLERANCE_UM = 1.0

#: Two fallback candidates whose distances differ by no more than this are
#: indistinguishable: the nearest-unique fallback cannot decide between them.
AMBIGUITY_EPS_UM = 1e-6

#: Canonical black-box pad layer vocabulary (pad_drawing / pad_text).
DEFAULT_PADS_CONFIG = ADK_ROOT / "config" / "chiplet_pads.json"

# Ecosystem-root variables accepted inside .chiplet path inputs, with the
# sibling-directory candidates and marker subpath of the discovery walk.
# Same convention and table as the producers (docs/integration.md).
_PATH_VAR_MARKERS = {
    "INTERPOSER_PDK_ROOT": (("interposer", "OpenIntM4TM2"),
                            ("libs.tech", "klayout")),
    "GDS_TO_KICAD_ROOT": (("gds_to_kicad", "gds-to-kicad"), ("pdks",)),
    "ADK_ROOT": (("adk", "ADK"), ("klayout", "drc")),
    "INTERCONNECT_PDK_ROOT": (("interconnect_pdk",
                               "IHP-Interconnect-IntM4TM2"), ("manifest",)),
    "PDK_ROOT": (("IHP-Open-PDK",), ("ihp-sg13g2", "libs.tech", "klayout")),
}

_PATH_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CheckError(ValueError):
    """Raised on usage/validation/tooling errors (exit code 2)."""


def load_chiplet(path) -> Dict[str, Any]:
    """Load and validate a finalized .chiplet file."""
    return cfio.load(path)


def _discover_path_var(name: str) -> Optional[str]:
    """Resolve an ecosystem-root variable: env first, then sibling walk.

    A set-but-invalid environment value (marker subpath missing) falls
    through to the walk. Returns the root as a string, or None.
    """
    marker = _PATH_VAR_MARKERS.get(name)
    if marker is None:
        # Only the known ecosystem-root variables are expandable; an unknown
        # ${NAME} is a typo, not a licence to read arbitrary environment.
        return None
    env = os.environ.get(name)
    if env and Path(env).joinpath(*marker[1]).is_dir():
        return env
    dirnames, sub = marker
    for base in ADK_ROOT.parents:
        for dirname in dirnames:
            cand = base / dirname
            if cand.joinpath(*sub).is_dir():
                return str(cand)
    return None


def expand_path_vars(path: str) -> str:
    """Expand ``${VAR}`` ecosystem-root references in a path input.

    Resolution per variable: environment -> sibling-checkout walk -> loud
    failure (a path that silently keeps a literal ``${VAR}`` component would
    just "not exist" downstream and mask the real problem). Paths without
    ``${`` pass through untouched.
    """
    if not path or "${" not in path:
        return path

    def _repl(match):
        name = match.group(1)
        value = _discover_path_var(name)
        if value is None:
            raise CheckError(
                "cannot resolve ${%s} in path '%s'. Set the %s environment "
                "variable or keep the checkout next to the adk checkout "
                "(ecosystem discovery convention, see docs/integration.md)."
                % (name, path, name))
        return value

    result = _PATH_VAR_RE.sub(_repl, path)
    if "${" in result:
        raise CheckError(
            "malformed variable reference in path '%s'. Use ${NAME} with an "
            "ecosystem-root name (see docs/integration.md)." % path)
    return result


def load_pillar_manifest(path) -> Dict[str, Any]:
    """Load and loudly validate a pillar manifest (exact-version pin).

    Mirrors the boundary-manifest validation in run_drc.py: a stale, foreign
    or malformed sidecar must fail here with a readable message, never be
    silently reinterpreted under new semantics.
    """
    path = Path(path)
    if not path.is_file():
        raise CheckError(
            "pillar manifest not found: %s\n"
            "The manifest (<assembly-gds-stem>.pillars.json) is written next "
            "to the assembly GDS by hyp_to_gds when the Cu-pillar/bump path "
            "runs. See docs/pillar_manifest.md." % path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(
            "pillar manifest is not readable JSON: %s\n%s" % (path, exc)
        ) from None
    if not isinstance(manifest, dict):
        raise CheckError(
            "pillar manifest is not a JSON object: %s (got %s). See "
            "docs/pillar_manifest.md."
            % (path, type(manifest).__name__))
    if manifest.get("schema") != PILLAR_MANIFEST_SCHEMA:
        raise CheckError(
            "not an ADK pillar manifest: %s (schema=%r; expected %r). See "
            "docs/pillar_manifest.md."
            % (path, manifest.get("schema"), PILLAR_MANIFEST_SCHEMA))
    if manifest.get("version") != SUPPORTED_PILLAR_MANIFEST_VERSION:
        raise CheckError(
            "unsupported pillar-manifest version in %s: found %r, this "
            "checker expects %r. Regenerate the sidecar with a current "
            "hyp_to_gds, or update the ADK. Version policy: "
            "docs/pillar_manifest.md."
            % (path, manifest.get("version"),
               SUPPORTED_PILLAR_MANIFEST_VERSION))
    if manifest.get("units") != SUPPORTED_UNITS:
        raise CheckError(
            "pillar manifest %s: units is %r; only %r is supported "
            "(x_um/y_um are a coordinate contract)."
            % (path, manifest.get("units"), SUPPORTED_UNITS))
    pillars = manifest.get("pillars")
    if not isinstance(pillars, list):
        raise CheckError(
            "pillar manifest %s: 'pillars' must be a list (got %s). See "
            "docs/pillar_manifest.md."
            % (path, type(pillars).__name__))
    for i, entry in enumerate(pillars):
        if not isinstance(entry, dict):
            raise CheckError(
                "pillar manifest %s: pillars[%d] is not an object" % (path, i))
        for key, types in (("device_ref", str), ("pin_name", str),
                           ("method", str)):
            if not isinstance(entry.get(key), types):
                raise CheckError(
                    "pillar manifest %s: pillars[%d].%s must be a string"
                    % (path, i, key))
        if not entry["device_ref"]:
            raise CheckError(
                "pillar manifest %s: pillars[%d].device_ref is empty"
                % (path, i))
        for key in ("x_um", "y_um", "diameter_um"):
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CheckError(
                    "pillar manifest %s: pillars[%d].%s must be a number"
                    % (path, i, key))
            if not math.isfinite(float(value)):
                # NaN/Inf would make every distance comparison fail-open
                # ('dist > tol' is False for NaN); reject at read time.
                raise CheckError(
                    "pillar manifest %s: pillars[%d].%s must be finite "
                    "(got %r)" % (path, i, key, value))
        if float(entry["diameter_um"]) <= 0:
            raise CheckError(
                "pillar manifest %s: pillars[%d].diameter_um must be > 0"
                % (path, i))
        moved = entry.get("moved_by_auto_resolve")
        if moved is not None and not isinstance(moved, bool):
            raise CheckError(
                "pillar manifest %s: pillars[%d].moved_by_auto_resolve must "
                "be a boolean when present (omit it when unknown)" % (path, i))
        shift = entry.get("auto_resolve_shift_um")
        if shift is not None:
            if (isinstance(shift, bool)
                    or not isinstance(shift, (int, float))
                    or not math.isfinite(float(shift)) or float(shift) < 0):
                raise CheckError(
                    "pillar manifest %s: pillars[%d].auto_resolve_shift_um "
                    "must be a finite non-negative number when present"
                    % (path, i))
    return manifest


def load_pads_config(path=None) -> Dict[str, Tuple[int, int]]:
    """Read the pad layer vocabulary (pad_drawing / pad_text) from
    config/chiplet_pads.json. Layer numbers are never hardcoded here."""
    path = Path(path) if path else DEFAULT_PADS_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(
            "cannot read pad layer config %s: %s" % (path, exc)) from None
    layers = data.get("layers") or {}
    result = {}
    for name in ("pad_drawing", "pad_text"):
        entry = layers.get(name) or {}
        layer = entry.get("gds_layer")
        datatype = entry.get("gds_datatype")
        if not isinstance(layer, int) or not isinstance(datatype, int):
            raise CheckError(
                "pad layer config %s lacks layers.%s.gds_layer/gds_datatype"
                % (path, name))
        result[name] = (layer, datatype)
    return result


def extract_gds_pads(gds_path, *, pads_config_path=None
                     ) -> List[Dict[str, Any]]:
    """Extract die-local pad centers (+ names) from a die layout GDS.

    Pads are the merged ``pad_drawing`` polygons (one per pad); each center
    is the polygon's bounding-box center in micrometers, relative to the GDS
    origin. Names come from ``pad_text`` labels contained in the pad polygon
    (nearest to the center when several are), else the nearest label overall;
    ``""`` when the die carries no labels.
    """
    try:
        import klayout.db as kdb
    except ImportError:
        raise CheckError(
            "--gds-pads needs the klayout.db Python module "
            "(pip install klayout); it is not importable in this "
            "interpreter") from None
    gds_path = Path(gds_path)
    if not gds_path.is_file():
        raise CheckError("die layout GDS not found: %s" % gds_path)
    layers = load_pads_config(pads_config_path)
    layout = kdb.Layout()
    try:
        layout.read(str(gds_path))
    except Exception as exc:  # KLayout raises RuntimeError subclasses
        raise CheckError("cannot read die GDS %s: %s" % (gds_path, exc)
                         ) from None
    try:
        top = layout.top_cell()
    except Exception as exc:
        raise CheckError(
            "die GDS %s has no unique top cell: %s" % (gds_path, exc)
        ) from None
    dbu = layout.dbu
    pad_li = layout.layer(*layers["pad_drawing"])
    text_li = layout.layer(*layers["pad_text"])

    region = kdb.Region(top.begin_shapes_rec(pad_li))
    region.merge()
    if region.is_empty():
        raise CheckError(
            "die GDS %s carries no pad_drawing polygons on layer %d/%d "
            "(config/chiplet_pads.json); cannot derive pads. If this die is "
            "not a black-box/pads-only chiplet, supply --pins instead."
            % (gds_path, *layers["pad_drawing"]))

    labels: List[Tuple[str, int, int]] = []
    it = top.begin_shapes_rec(text_li)
    while not it.at_end():
        shape = it.shape()
        if shape.is_text():
            text = shape.text.transformed(it.trans())
            labels.append((text.string, text.x, text.y))
        it.next()

    pads = []
    for poly in region.each():
        center = poly.bbox().center()
        name = ""
        if labels:
            contained = [lab for lab in labels
                         if poly.inside(kdb.Point(lab[1], lab[2]))]
            pool = contained if contained else labels
            name = min(pool, key=lambda lab: (lab[1] - center.x) ** 2
                       + (lab[2] - center.y) ** 2)[0]
        pads.append({
            "name": name,
            "x_um": center.x * dbu,
            "y_um": center.y * dbu,
        })
    pads.sort(key=lambda p: (p["x_um"], p["y_um"], p["name"]))
    return pads


def _die_placement(comp: Dict[str, Any]) -> Tuple[float, float, float, bool]:
    """(x, y, rotation_deg, mirrored) of a die; loud on face_down/unknown."""
    pos = comp.get("position") or {}
    try:
        x, y = float(pos["x"]), float(pos["y"])
    except (KeyError, TypeError, ValueError):
        raise CheckError(
            "die %r has no usable position.x/y; cannot place its pads"
            % comp.get("id")) from None
    rotation = float((comp.get("rotation") or {}).get("z", 0.0))
    orientation = comp.get("orientation", "face_up")
    if orientation == "face_up":
        mirrored = False
    elif orientation == "flip_chip":
        mirrored = True
    elif orientation == "face_down":
        raise CheckError(
            "die %r uses orientation face_down, which this check "
            "deliberately leaves unmapped (same policy as chiplet2dbx)"
            % comp.get("id"))
    else:
        raise CheckError(
            "die %r has unknown orientation %r"
            % (comp.get("id"), orientation))
    return x, y, rotation, mirrored


def transform_pads(comp: Dict[str, Any], pads: List[Dict[str, Any]]
                   ) -> List[Dict[str, Any]]:
    """Map die-local pads into the canonical interposer GDS-bbox-corner frame.

    Applies ``global = position + R(rotation.z) * M * pad`` per the frame
    contract in the module docstring (M = x-mirror for flip_chip). The die
    ``position`` already lives in that canonical frame, so the result is
    directly comparable to the rebased pillar-manifest coordinates.
    """
    x0, y0, rotation, mirrored = _die_placement(comp)
    angle = math.radians(rotation)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    result = []
    for pad in pads:
        px = -pad["x_um"] if mirrored else pad["x_um"]
        py = pad["y_um"]
        result.append({
            "name": pad.get("name", ""),
            "x_um": x0 + px * cos_a - py * sin_a,
            "y_um": y0 + px * sin_a + py * cos_a,
        })
    return result


def _distance(pad: Dict[str, Any], pillar: Dict[str, Any]) -> float:
    return math.hypot(pad["x_um"] - pillar["x_um"],
                      pad["y_um"] - pillar["y_um"])


def _finding(kind: str, ref: str, message: str, **extra) -> Dict[str, Any]:
    finding = {"type": kind, "device_ref": ref, "message": message}
    finding.update(extra)
    return finding


def match_device(ref: str, pads: List[Dict[str, Any]],
                 pillars: List[Dict[str, Any]], *,
                 tolerance_um: float = DEFAULT_TOLERANCE_UM,
                 warnings: Optional[List[str]] = None
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """Match one die's global-frame pads against its pillar entries.

    Named entries (both sides non-empty) match by exact pin name; a named
    pair beyond tolerance is MISALIGNED. Exception: when the pillar carries
    ``moved_by_auto_resolve: true`` AND a recorded ``auto_resolve_shift_um``
    magnitude, the producer's collision auto-resolve shifted that bump away
    from its pad on purpose, so a deviation within shift + tolerance is
    reported to the ``warnings`` list instead of as a finding (the
    CLI/run_check path); beyond shift + tolerance the flag cannot explain it
    and it is MISALIGNED again. A bare ``moved_by_auto_resolve`` boolean
    with no magnitude does NOT demote (it cannot bound the excuse) and stays
    a MISALIGNED finding; so does any moved pillar when no ``warnings`` list
    is supplied (legacy importers). Unnamed moved pillars beyond tolerance
    still surface as PAD_WITHOUT_PILLAR/PILLAR_WITHOUT_PAD leftovers:
    without a name the pair cannot be attributed.

    Two conflicting names never match (they surface as PAD_WITHOUT_PILLAR +
    PILLAR_WITHOUT_PAD). Remaining entries where at least one side is
    unnamed fall back to greedy nearest-unique matching within tolerance;
    near-ties the fallback cannot decide become AMBIGUOUS_MATCH. Returns
    (findings, matched_count).
    """
    if not math.isfinite(tolerance_um) or tolerance_um < 0:
        # NaN would fail-open ('dist > nan' is always False): a pipeline
        # interpolating a failed computation must die here, not go green.
        raise CheckError(
            "tolerance must be a finite non-negative number of micrometers, "
            "got %r" % tolerance_um)

    findings: List[Dict[str, Any]] = []
    matched = 0
    pad_free = set(range(len(pads)))
    pillar_free = set(range(len(pillars)))

    # 1) Exact pin-name matches (nearest-first inside each name group, so
    #    duplicate names pair deterministically).
    pads_by_name: Dict[str, List[int]] = {}
    pillars_by_name: Dict[str, List[int]] = {}
    for i, pad in enumerate(pads):
        if pad["name"]:
            pads_by_name.setdefault(pad["name"], []).append(i)
    for j, pillar in enumerate(pillars):
        if pillar["pin_name"]:
            pillars_by_name.setdefault(pillar["pin_name"], []).append(j)
    for name in sorted(set(pads_by_name) & set(pillars_by_name)):
        pairs = sorted(
            (_distance(pads[i], pillars[j]), i, j)
            for i in pads_by_name[name] for j in pillars_by_name[name])
        for dist, i, j in pairs:
            if i not in pad_free or j not in pillar_free:
                continue
            pad_free.discard(i)
            pillar_free.discard(j)
            matched += 1
            if dist > tolerance_um:
                pillar = pillars[j]
                shift = pillar.get("auto_resolve_shift_um")
                moved = pillar.get("moved_by_auto_resolve") is True
                if warnings is not None and moved and shift is not None:
                    # The auto-resolve flag excuses exactly the recorded
                    # shift: bound the demotion to shift+tolerance so a
                    # genuine placement bug that happens to hit a
                    # collision-moved bump is not hidden.
                    if dist <= float(shift) + tolerance_um:
                        warnings.append(
                            "die %s: pillar %r sits %.6f um from pad %r "
                            "(tolerance %.6f um) but is flagged "
                            "moved_by_auto_resolve (shift %.6f um) — the "
                            "producer's collision auto-resolve shifted it on "
                            "purpose; not a finding"
                            % (ref, name, dist, name, tolerance_um,
                               float(shift)))
                        continue
                    # Beyond shift+tolerance: the flag cannot explain this.
                    findings.append(_finding(
                        "MISALIGNED", ref,
                        "pad %r sits %.6f um from its pillar, more than the "
                        "recorded auto-resolve shift %.6f um plus tolerance "
                        "%.6f um" % (name, dist, float(shift), tolerance_um),
                        pad_name=name, pillar_pin_name=name,
                        pad_x_um=pads[i]["x_um"], pad_y_um=pads[i]["y_um"],
                        pillar_x_um=pillar["x_um"], pillar_y_um=pillar["y_um"],
                        distance_um=dist,
                        auto_resolve_shift_um=float(shift)))
                    continue
                # A moved flag WITHOUT a recorded magnitude cannot bound the
                # excuse, so it does NOT demote: a bare boolean is advisory
                # only. Our producer always records the magnitude alongside
                # the flag, so real manifests are unaffected; this closes an
                # unbounded false-pass for foreign/hand-edited manifests.
                findings.append(_finding(
                    "MISALIGNED", ref,
                    "pad %r sits %.6f um from its pillar (tolerance %.6f um)"
                    % (name, dist, tolerance_um),
                    pad_name=name, pillar_pin_name=name,
                    pad_x_um=pads[i]["x_um"], pad_y_um=pads[i]["y_um"],
                    pillar_x_um=pillars[j]["x_um"],
                    pillar_y_um=pillars[j]["y_um"],
                    distance_um=dist))

    # 2) Nearest-unique fallback for pairs with at least one unnamed side.
    candidates = []
    for i in pad_free:
        for j in pillar_free:
            if pads[i]["name"] and pillars[j]["pin_name"]:
                continue  # conflicting names never cross-match
            dist = _distance(pads[i], pillars[j])
            if dist <= tolerance_um:
                candidates.append((dist, i, j))
    candidates.sort()
    while candidates:
        candidates = [(d, i, j) for (d, i, j) in candidates
                      if i in pad_free and j in pillar_free]
        if not candidates:
            break
        dist0, i0, j0 = candidates[0]
        rivals = [(d, i, j) for (d, i, j) in candidates[1:]
                  if (i == i0 or j == j0)
                  and (d - dist0) <= AMBIGUITY_EPS_UM]
        if rivals:
            amb_pads = sorted({i0} | {i for _, i, _ in rivals})
            amb_pillars = sorted({j0} | {j for _, _, j in rivals})
            findings.append(_finding(
                "AMBIGUOUS_MATCH", ref,
                "nearest-unique fallback cannot decide: %d pad(s) and %d "
                "pillar(s) at indistinguishable distance %.6f um"
                % (len(amb_pads), len(amb_pillars), dist0),
                pad_names=[pads[i]["name"] for i in amb_pads],
                pillar_pin_names=[pillars[j]["pin_name"]
                                  for j in amb_pillars],
                distance_um=dist0))
            pad_free -= set(amb_pads)
            pillar_free -= set(amb_pillars)
            continue
        pad_free.discard(i0)
        pillar_free.discard(j0)
        matched += 1  # within tolerance by construction

    # 3) Leftovers.
    for i in sorted(pad_free):
        findings.append(_finding(
            "PAD_WITHOUT_PILLAR", ref,
            "pad %r at (%.6f, %.6f) um has no matching pillar"
            % (pads[i]["name"], pads[i]["x_um"], pads[i]["y_um"]),
            pad_name=pads[i]["name"],
            pad_x_um=pads[i]["x_um"], pad_y_um=pads[i]["y_um"]))
    for j in sorted(pillar_free):
        findings.append(_finding(
            "PILLAR_WITHOUT_PAD", ref,
            "pillar %r (method %s) at (%.6f, %.6f) um has no matching pad"
            % (pillars[j]["pin_name"], pillars[j].get("method"),
               pillars[j]["x_um"], pillars[j]["y_um"]),
            pillar_pin_name=pillars[j]["pin_name"],
            method=pillars[j].get("method"),
            pillar_x_um=pillars[j]["x_um"],
            pillar_y_um=pillars[j]["y_um"]))
    return findings, matched


def run_check(assembly: Dict[str, Any], manifest: Dict[str, Any],
              die_pads: Dict[str, List[Dict[str, Any]]], *,
              tolerance_um: float = DEFAULT_TOLERANCE_UM,
              strict: bool = False) -> Dict[str, Any]:
    """Run the pads-vs-pillars check; returns the machine-readable report.

    ``die_pads`` maps die refs to DIE-LOCAL pad lists
    (``[{"name", "x_um", "y_um"}, ...]``); the placement transform is
    applied here. Unknown refs are a hard error.
    """
    if not math.isfinite(tolerance_um) or tolerance_um < 0:
        # Validate before the per-die loop so a NaN/negative tolerance is
        # rejected even when no die has a pad source (match_device, which
        # also guards, is not reached in that case). A CI checker must not
        # go green on a failed-computation tolerance.
        raise CheckError(
            "tolerance must be a finite non-negative number of micrometers, "
            "got %r" % tolerance_um)
    units = (assembly.get("assembly") or {}).get("units")
    if units is not None and units != SUPPORTED_UNITS:
        # The die positions and pad coordinates are compared as micrometers;
        # a mm-declared assembly would silently pass at 1000x scale. Reject
        # it loudly, same policy as chiplet2dbx.
        raise CheckError(
            "assembly.units is %r; only %r is supported (positions are "
            "compared as micrometers)" % (units, SUPPORTED_UNITS))
    dies = {str(c.get("id")): c
            for c in (assembly.get("components") or [])
            if c.get("type") == "die"}
    unknown = sorted(set(die_pads) - set(dies))
    if unknown:
        raise CheckError(
            "pad sources were supplied for unknown die refs: %s "
            "(dies in the .chiplet: %s)"
            % (", ".join(unknown), ", ".join(sorted(dies)) or "none"))

    pillars_by_ref: Dict[str, List[Dict[str, Any]]] = {}
    for entry in manifest.get("pillars") or []:
        pillars_by_ref.setdefault(str(entry["device_ref"]), []).append(entry)

    findings: List[Dict[str, Any]] = []
    warnings: List[str] = []
    devices: Dict[str, Dict[str, Any]] = {}

    for ref in sorted(set(pillars_by_ref) - set(dies)):
        warnings.append(
            "pillar manifest references device_ref %r which is not a die in "
            "the .chiplet (stale manifest?)" % ref)

    for ref in sorted(dies):
        comp = dies[ref]
        pillars = pillars_by_ref.get(ref, [])
        if ref not in die_pads:
            if comp.get("connection") or pillars:
                message = (
                    "die %r has a connection method (%r, %d pillar(s) in "
                    "the manifest) but no pad source was supplied; its "
                    "alignment is UNCHECKED. Pass --pins %s=... or "
                    "--gds-pads %s."
                    % (ref, comp.get("connection"), len(pillars), ref, ref))
                if strict:
                    findings.append(_finding("NO_PAD_SOURCE", ref, message))
                else:
                    warnings.append(message)
            continue
        pads_global = transform_pads(comp, die_pads[ref])
        device_findings, matched = match_device(
            ref, pads_global, pillars, tolerance_um=tolerance_um,
            warnings=warnings)
        findings.extend(device_findings)
        devices[ref] = {
            "pads": len(pads_global),
            "pillars": len(pillars),
            "matched": matched,
            "findings": len(device_findings),
        }

    return {
        "tool": "pads_vs_pillars",
        "tolerance_um": tolerance_um,
        "strict": strict,
        "devices": devices,
        "findings": findings,
        "warnings": warnings,
        "summary": {
            "devices_checked": len(devices),
            "findings": len(findings),
            "warnings": len(warnings),
            "passed": not findings,
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify die pad positions against the as-drawn pillar "
                    "positions of the assembly's pillar manifest "
                    "(<gds-stem>.pillars.json).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python checks/pads_vs_pillars.py --chiplet demo.chiplet \\\n"
               "      --pillars build/demo_interposer.pillars.json \\\n"
               "      --pins U1=chiplets/die_a.pins.json --gds-pads U2\n",
    )
    parser.add_argument("--chiplet", required=True,
                        help="finalized .chiplet file (die placements)")
    parser.add_argument("--pillars", required=True,
                        help="pillar manifest (<gds-stem>.pillars.json)")
    parser.add_argument("--pins", action="append", default=[],
                        metavar="REF=PINS_JSON",
                        help="per-die pin list (gds_to_kicad *.pins.json), "
                             "die-local coordinates; repeatable")
    parser.add_argument("--gds-pads", action="append", default=[],
                        metavar="REF", dest="gds_pads",
                        help="extract that die's pads from its .chiplet "
                             "layout GDS (pad_drawing/pad_text layers per "
                             "config/chiplet_pads.json); repeatable; needs "
                             "klayout.db")
    parser.add_argument("--tolerance-um", type=float,
                        default=DEFAULT_TOLERANCE_UM,
                        help="pad-to-pillar distance tolerance in um "
                             "(default: %(default)s)")
    parser.add_argument("--json", default=None, metavar="PATH",
                        help="write the machine-readable report here")
    parser.add_argument("--strict", action="store_true",
                        help="dies with a connection method but no pad "
                             "source become findings instead of warnings")
    return parser.parse_args(argv)


def _parse_pins_args(specs: List[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for spec in specs:
        ref, sep, path = spec.partition("=")
        if not sep or not ref or not path:
            raise CheckError("--pins expects REF=PATH, got %r" % spec)
        if ref in result:
            raise CheckError("--pins given twice for ref %r" % ref)
        result[ref] = path
    return result


def _gather_die_pads(assembly: Dict[str, Any], chiplet_path: Path,
                     pins_specs: List[str], gds_refs: List[str]
                     ) -> Dict[str, List[Dict[str, Any]]]:
    """Resolve all pad sources into die-local pad lists, loudly."""
    die_pads: Dict[str, List[Dict[str, Any]]] = {}
    for ref, pins_path in _parse_pins_args(pins_specs).items():
        die_pads[ref] = chiplet2dbx.load_pinlist(pins_path)

    seen_gds = set()
    dies = {str(c.get("id")): c
            for c in (assembly.get("components") or [])
            if c.get("type") == "die"}
    for ref in gds_refs:
        if ref in seen_gds:
            raise CheckError("--gds-pads given twice for ref %r" % ref)
        seen_gds.add(ref)
        if ref in die_pads:
            raise CheckError(
                "ref %r was given both --pins and --gds-pads; pick one pad "
                "source" % ref)
        comp = dies.get(ref)
        if comp is None:
            raise CheckError(
                "--gds-pads %s: no die with that id in the .chiplet (dies: "
                "%s)" % (ref, ", ".join(sorted(dies)) or "none"))
        layout_ref = comp.get("layout")
        if not layout_ref:
            raise CheckError(
                "--gds-pads %s: die has no 'layout' entry in the .chiplet; "
                "supply --pins instead" % ref)
        # ${VAR} discovery refs expand on read; relative paths resolve
        # against the .chiplet file's directory (docs/integration.md).
        gds_path = Path(expand_path_vars(str(layout_ref)))
        if not gds_path.is_absolute():
            gds_path = chiplet_path.parent / gds_path
        die_pads[ref] = extract_gds_pads(gds_path)
    return die_pads


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        assembly = load_chiplet(args.chiplet)
        manifest = load_pillar_manifest(args.pillars)
        die_pads = _gather_die_pads(
            assembly, Path(args.chiplet).resolve(), args.pins, args.gds_pads)
        report = run_check(assembly, manifest, die_pads,
                           tolerance_um=args.tolerance_um, strict=args.strict)
    except (cfio.ChipletFormatError, chiplet2dbx.ExportError, CheckError,
            OSError) as exc:
        print("pads_vs_pillars: error: %s" % exc, file=sys.stderr)
        return 2

    report["chiplet"] = str(args.chiplet)
    report["pillar_manifest"] = str(args.pillars)

    for warning in report["warnings"]:
        print("WARNING: %s" % warning, file=sys.stderr)
    for finding in report["findings"]:
        print("%s [%s]: %s"
              % (finding["type"], finding["device_ref"], finding["message"]))
    summary = report["summary"]
    print("pads_vs_pillars: %d finding(s), %d warning(s) across %d checked "
          "device(s): %s"
          % (summary["findings"], summary["warnings"],
             summary["devices_checked"],
             "PASSED" if summary["passed"] else "FAILED"))

    if args.json:
        try:
            Path(args.json).write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print("pads_vs_pillars: error: cannot write report %s: %s"
                  % (args.json, exc), file=sys.stderr)
            return 2
        print("Report: %s" % args.json)

    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
