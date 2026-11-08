#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Derive an ADK attachment sidecar from an accepted ``.chiplet`` plus its pillar
manifest, for a given assembly-lvs resolved v2 document.

The sidecar (``adk-resolved-attachment`` 1.0.0) authors what the resolved
contract lacks and the ADK view needs: adapter ids, attachment layers, the
attachment surface z, the carrier thickness and z, per-die method, technology,
thickness and declared dimensions, and it binds by sha256 every file the entry
will open. This tool copies those facts from the ``.chiplet`` (dimensions
verbatim), recomputes every die z from the
pinned methods manifest and refuses when the ``.chiplet`` disagrees, checks the
pillar manifest's ``methods`` numbers against the same manifest, and records the
``.chiplet`` as ``expect_chiplet`` so the entry round-trips against it.

Exit codes: 0 written, 1 usage, 2 refusal (nothing written), registry codes for
adapter id problems.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import resolved_v2_entry as entry  # noqa: E402
from resolved_v2_entry import Refusal, Usage, _fail, _in_root_file, _json_number, _regular_file  # noqa: E402

cfio = entry.cfio
adk_registry = entry.adk_registry


def _pair_text(text: str, where: str):
    try:
        layer, datatype = text.split("/")
        return [int(layer), int(datatype)]
    except ValueError:
        _fail(where, f"expected 'layer/datatype', got {text!r}")


def _decimal_of(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        _fail(where, "expected a number")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        _fail(where, f"expected a number, got {value!r}")


def derive(chiplet_path: Path, pillars_path: Path, document_path: Path, dies_root: Path,
           methods_manifest: str, out_path: Path) -> Dict[str, Any]:
    chiplet_path = _regular_file(Path(chiplet_path).resolve(), "chiplet")
    pillars_path = _regular_file(Path(pillars_path).resolve(), "pillars")
    dies_root = Path(dies_root).resolve()
    if not dies_root.is_dir():
        raise Usage(f"dies root is not a directory: {dies_root}")
    out_path = Path(out_path).resolve()
    if out_path.exists():
        _fail("out", f"sidecar exists: {out_path}")
    # Both derivation inputs are read once: the bytes parsed are the bytes whose sha256 is
    # recorded in derived_from.
    chiplet_raw, chiplet_sha = entry._read_bound(chiplet_path, None, "chiplet")
    pillars_raw, pillars_sha = entry._read_bound(pillars_path, None, "pillars")
    try:
        chiplet = cfio.loads(chiplet_raw.decode("utf-8"))
    except cfio.ChipletFormatError as exc:
        _fail("chiplet", f"vendored reader refuses it: {exc}")
    doc = entry.decode_resolved_v2(Path(document_path))
    grid = doc.technology.grid_um

    components = chiplet.get("components") or []
    carriers = [c for c in components if c.get("type") == "interposer"]
    dies = [c for c in components if c.get("type") == "die"]
    if len(carriers) != 1:
        _fail("chiplet.components", "expected exactly one interposer component")
    carrier = carriers[0]
    die_ids = {d.id for d in doc.dies}
    if {d.get("id") for d in dies} != die_ids:
        _fail("chiplet.components", f"die ids {sorted(d.get('id') for d in dies)} differ from the "
                                    f"resolved document {sorted(die_ids)}")
    interposer_adapter = (chiplet.get("interposer") or {}).get("adapter")
    ixn_adapter = (chiplet.get("interconnect") or {}).get("adapter")
    if not interposer_adapter or not ixn_adapter:
        _fail("chiplet", "interposer.adapter and interconnect.adapter are required")
    interposer_id = adk_registry.validate_adapter_id(interposer_adapter)
    ixn_id = adk_registry.validate_adapter_id(ixn_adapter)
    deck_path = adk_registry.resolve_adapter(interposer_id).value
    adk_registry.resolve_adapter(ixn_id)
    ixn_root = adk_registry.resolve_root("INTERCONNECT_PDK").value
    manifest_path = _in_root_file(ixn_root, methods_manifest, "methods_manifest")
    deck_pairs = entry.parse_attachment_deck(deck_path.read_text(encoding="utf-8"))
    raw_manifest = entry.load_strict_json(manifest_path.read_text(encoding="utf-8"))
    methods = entry.load_methods_manifest(manifest_path, [d.get("connection") for d in dies], ixn_id)

    # Attachment layers from the fab anchor and the 3D layer registry of the methods.
    anchors = set()
    bodies: List[List[int]] = []
    for die in dies:
        m = raw_manifest["methods"][die["connection"]]
        anchors.add(m.get("fab_anchor"))
        layers_3d = m.get("layers_3d") or []
        if len(layers_3d) != 2:
            _fail(f"methods_manifest.methods.{die['connection']}.layers_3d", "expected two layers")
        registry = raw_manifest.get("layer_registry") or {}
        pair = []
        for name in layers_3d:
            if name not in registry:
                _fail("methods_manifest.layer_registry", f"unknown 3D layer {name!r}")
            pair.append([int(registry[name]["gds_layer"]), int(registry[name]["gds_datatype"])])
        bodies.append(pair)
    if len(anchors) != 1 or len({json.dumps(b) for b in bodies}) != 1:
        _fail("methods_manifest", "the dies' methods must share one fab anchor and one 3D layer pair")
    anchor = (raw_manifest.get("fab_anchors") or {}).get(next(iter(anchors)))
    if not anchor:
        _fail("methods_manifest.fab_anchors", f"unknown fab anchor {next(iter(anchors))!r}")
    layers = anchor.get("layers") or {}
    attachment_layers = {
        "opening": _pair_text(layers.get("passiv", ""), "fab_anchors.layers.passiv"),
        "dfpad": _pair_text(layers.get("dfpad", ""), "fab_anchors.layers.dfpad"),
        "body": bodies[0][0], "body2": bodies[0][1],
        "mark": _pair_text(layers.get("recog", ""), "fab_anchors.layers.recog"),
    }
    if deck_pairs["pillar"] != (tuple(attachment_layers["opening"]), tuple(attachment_layers["dfpad"])):
        _fail("methods_manifest.fab_anchors", f"passiv/dfpad {attachment_layers['opening']}/"
                                              f"{attachment_layers['dfpad']} differ from the deck's "
                                              f"pillar branch {deck_pairs['pillar']}")

    surface_z = _decimal_of(carrier.get("attachment_surface_z"), "chiplet.interposer.attachment_surface_z")
    carrier_z = _decimal_of((carrier.get("position") or {}).get("z"), "chiplet.interposer.position.z")
    thickness = _decimal_of((carrier.get("dimensions") or {}).get("thickness"),
                            "chiplet.interposer.dimensions.thickness")
    stacks = chiplet.get("connection_stacks") or {}
    spec: Dict[str, Any] = {}
    for die in dies:
        did = die["id"]
        where = f"chiplet.components[{did}]"
        method = methods[die["connection"]]
        z = _decimal_of((die.get("position") or {}).get("z"), f"{where}.position.z")
        expected = carrier_z + surface_z + method.stack_height
        if z != expected:
            _fail(f"{where}.position.z", f"{z} differs from the recomputed {expected} "
                                         f"(carrier {carrier_z} + surface {surface_z} + stack "
                                         f"{method.stack_height})")
        stack = (stacks.get(method.id) or {}).get("layers")
        if stack is None:
            _fail(f"chiplet.connection_stacks.{method.id}", "missing")
        got = [(l.get("name"), l.get("material"), _decimal_of(l.get("height"), where),
                _decimal_of(l.get("diameter"), where)) for l in stack]
        want = [(l["name"], l["material"], l["height"], l["diameter"]) for l in method.stack_layers]
        if got != want:
            _fail(f"chiplet.connection_stacks.{method.id}", f"layers {got} differ from the manifest {want}")
        dims = die.get("dimensions") or {}
        layout_ref = die.get("layout")
        if not layout_ref:
            _fail(f"{where}.layout", "missing")
        layout_abs = (chiplet_path.parent / os.path.expandvars(layout_ref)).resolve()
        try:
            rel = layout_abs.relative_to(dies_root)
        except ValueError:
            _fail(f"{where}.layout", f"{layout_abs} is not under the dies root {dies_root}")
        _regular_file(layout_abs, f"{where}.layout")
        if die.get("anchor") != "gds_origin":
            _fail(f"{where}.anchor", "the entry writes and expects anchor gds_origin")
        spec[did] = {
            "method": method.id, "technology": die.get("technology"),
            "thickness": _json_number(_decimal_of(dims.get("thickness"), f"{where}.dimensions.thickness")),
            "dimensions": {"width": _json_number(_decimal_of(dims.get("width"), f"{where}.dimensions.width")),
                           "height": _json_number(_decimal_of(dims.get("height"), f"{where}.dimensions.height"))},
            "layout": rel.as_posix(), "top_cell": die.get("top_cell"),
        }
        if not spec[did]["technology"] or not spec[did]["top_cell"]:
            _fail(where, "technology and top_cell are required")

    # Pillar manifest: methods block against the same manifest numbers.
    pillars = entry.load_strict_json(pillars_raw.decode("utf-8"))
    if pillars.get("schema") != "adk-pillar-manifest":
        _fail("pillars.schema", "not an ADK pillar manifest")
    # The intended method inventory is required (every placed die's method present),
    # every declared method must exist in the methods manifest, and none is skipped.
    declared = pillars.get("methods")
    if not isinstance(declared, dict) or not declared:
        _fail("pillars.methods", "the pillar manifest carries no methods block (1.1.0 with methods is required)")
    needed = sorted({spec[did]["method"] for did in spec})
    missing = sorted(set(needed) - set(declared))
    if missing:
        _fail("pillars.methods", f"pillar manifest lacks the methods of the placed dies: {missing}")
    declared_methods = entry.load_methods_manifest(manifest_path, declared.keys(), ixn_id)
    for mid, numbers in declared.items():
        m = declared_methods[mid]
        want = {"IXN_spacing": m.ixn_spacing, "IXN_pitch": m.ixn_pitch, "IXN_pad_size": m.passiv_opening_um}
        for key, value in want.items():
            if _decimal_of(numbers.get(key), f"pillars.methods.{mid}.{key}") != value:
                _fail(f"pillars.methods.{mid}.{key}", f"{numbers.get(key)} differs from the manifest {value}")

    # io_class and layer names from the .chiplet io_pads, matched by id.
    io_pads = {p.get("id"): p for p in carrier.get("io_pads") or []}
    io_classes = {p.get("io_class") for p in io_pads.values() if p.get("io_class") is not None}
    if len(io_classes) > 1:
        _fail("chiplet.interposer.io_pads", f"io_class is not uniform: {sorted(io_classes)}")
    layer_names: Dict[str, str] = {}
    for pad in doc.interposer.io_pads:
        chip_pad = io_pads.get(pad.id)
        if chip_pad is None or chip_pad.get("layer") is None:
            continue
        previous = layer_names.get(pad.technology_layer)
        if previous is not None and previous != chip_pad["layer"]:
            _fail("chiplet.interposer.io_pads", f"layer {pad.technology_layer!r} maps to both "
                                                f"{previous!r} and {chip_pad['layer']!r}")
        layer_names[pad.technology_layer] = chip_pad["layer"]

    technologies = {}
    for name, tech in (chiplet.get("technologies") or {}).items():
        technologies[name] = dict(tech)
        if "dbu" not in tech:
            _fail(f"chiplet.technologies.{name}", "dbu is required")
        if _decimal_of(tech["dbu"], f"chiplet.technologies.{name}.dbu") != grid:
            _fail(f"chiplet.technologies.{name}.dbu", f"{tech['dbu']} differs from grid_um {grid}")
    if carrier.get("technology") not in technologies:
        _fail("chiplet.interposer.technology", "unknown technology")

    out_dir = out_path.parent
    sidecar = {
        "schema": entry.SIDECAR_SCHEMA, "version": entry.SIDECAR_VERSION,
        "document": os.path.relpath(doc.source_path, out_dir).replace(os.sep, "/"),
        "dies_root": os.path.relpath(dies_root, out_dir).replace(os.sep, "/"),
        "binds": {
            "document": doc.source_sha256,
            "layouts": {c.id: entry.sha256_of(c.layout_resolved) for c in doc.components},
            "die_layouts": {did: entry.sha256_of(dies_root / spec[did]["layout"]) for did in spec},
            "methods_manifest": entry.sha256_of(manifest_path),
            "adapter_deck": entry.sha256_of(deck_path),
        },
        "interposer": {
            "adapter": interposer_id, "technology": carrier.get("technology"),
            "attachment_surface_z": _json_number(surface_z), "position_z": _json_number(carrier_z),
            "thickness": _json_number(thickness), "attachment_layers": attachment_layers,
        },
        "interconnect": {"adapter": ixn_id, "methods_manifest": methods_manifest},
        "technologies": technologies,
        "components": spec,
        "expect_chiplet": os.path.relpath(chiplet_path, out_dir).replace(os.sep, "/"),
        "derived_from": {
            "chiplet": {"path": os.path.relpath(chiplet_path, out_dir).replace(os.sep, "/"),
                        "sha256": chiplet_sha},
            "pillars": {"path": os.path.relpath(pillars_path, out_dir).replace(os.sep, "/"),
                        "sha256": pillars_sha},
        },
    }
    if io_classes:
        sidecar["interposer"]["io_class"] = next(iter(io_classes))
    if layer_names:
        sidecar["interposer"]["layer_names"] = layer_names
    ixn_tech = (chiplet.get("interconnect") or {}).get("technology")
    if ixn_tech is not None:
        sidecar["interconnect"]["technology"] = ixn_tech
    # Validate before publication: the candidate is written beside the destination (so
    # its relative paths resolve exactly as the published file's will), re-read by the
    # entry's loader, and only then published by os.link, which creates the destination
    # atomically with the full content and fails if any destination exists, including one
    # that appeared during validation (no clobber, ever). A validation refusal leaves no
    # candidate behind; a refused destination is never touched.
    if out_path.exists() or out_path.is_symlink():
        _fail("out", f"{out_path} exists; a fresh path is required (left untouched)")
    text = json.dumps(sidecar, indent=1, ensure_ascii=False) + "\n"
    fd, candidate = tempfile.mkstemp(prefix=f".{out_path.name}.", suffix=".tmp", dir=str(out_path.parent))
    candidate = Path(candidate)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        entry.load_sidecar(candidate)
        try:
            os.link(candidate, out_path)
        except FileExistsError:
            _fail("out", f"{out_path} appeared during validation; left untouched, nothing published")
    finally:
        if candidate.exists():
            candidate.unlink()
    return sidecar


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="derive_attachment_sidecar.py",
                                     description="Derive an ADK attachment sidecar from an accepted "
                                                 ".chiplet plus pillar manifest.")
    parser.add_argument("--chiplet", required=True)
    parser.add_argument("--pillars", required=True, help="pillar manifest (1.1.0, with methods)")
    parser.add_argument("--document", required=True, help="the resolved v2 document")
    parser.add_argument("--dies-root", required=True, help="root directory of the ADK die layouts")
    parser.add_argument("--methods-manifest", default="manifest/interconnect_methods.json",
                        help="in-root path under the registry root INTERCONNECT_PDK")
    parser.add_argument("--out", required=True, help="sidecar path to write (must not exist)")
    args = parser.parse_args(argv)
    try:
        derive(Path(args.chiplet), Path(args.pillars), Path(args.document), Path(args.dies_root),
               args.methods_manifest, Path(args.out))
    except Usage as exc:
        print(f"derive_attachment_sidecar: usage error: {exc}", file=sys.stderr)
        return entry.EXIT_USAGE
    except Refusal as exc:
        print(f"derive_attachment_sidecar: refused: {exc}", file=sys.stderr)
        return entry.EXIT_REFUSED
    except adk_registry.RegistryError as exc:
        print(f"derive_attachment_sidecar: registry: {exc}", file=sys.stderr)
        return adk_registry.exit_code_for(exc)
    print(f"sidecar: {args.out}")
    return entry.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
