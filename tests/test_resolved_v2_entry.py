# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for bridges/assembly_lvs/resolved_v2_entry.py.

Everything is synthesized: a resolved v2 document in the reference design's
shape, a carrier layout with real attachment artwork on the six layers
(256-gons), proxy layouts (128-gons), ADK die layouts (pads, labels, outline), a
methods manifest, two adapter decks and a registry layer. Expected positions
come from an independent matrix-form transform written in this module, so a
frame regression in the entry cannot hide behind its own arithmetic.

Two tests are gated and never count as ordinary unit evidence: the corpus run
(ADK_RESOLVED_V2_CORPUS_ROOT; a failure instead of a skip under
ADK_RESOLVED_V2_ACCEPTANCE=1) and the frozen-parser differential
(ADK_RESOLVED_V2_FROZEN_PARSER_ROOT). docs/resolved_v2_entry.md describes the
corpus layout.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db", reason="the entry needs klayout.db")
yaml = pytest.importorskip("yaml", reason="the entry needs PyYAML")

ADK_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = ADK_ROOT / "bridges" / "assembly_lvs"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import resolved_v2_entry as entry  # noqa: E402
import derive_attachment_sidecar as derive  # noqa: E402

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

SCHEMA_DIR = ADK_ROOT / "config" / "schema"
GRID = Decimal("0.001")
U = 1000  # DBU per um on the 0.001 grid

# --------------------------------------------------------------------------- #
# Independent reference transform (matrix form)                                #
# --------------------------------------------------------------------------- #
_ROT = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def ref_c(p, die, interposer, corner):
    """Resolved chain in matrix form: mirror M_y, then R(theta), then translate."""
    x, y = p[0] - die["anchor"][0], p[1] - die["anchor"][1]
    if die["mirror"]:
        y = -y
    c, s = _ROT[die["rotation"]]
    rx, ry = c * x - s * y, s * x + c * y
    ax, ay = die["position"][0] + rx, die["position"][1] + ry
    gx, gy = ax - interposer["position"][0] + interposer["anchor"][0], \
        ay - interposer["position"][1] + interposer["anchor"][1]
    return gx - corner[0], gy - corner[1]


def ref_g(p, die, interposer, corner):
    cx, cy = ref_c(p, die, interposer, corner)
    return cx + corner[0], cy + corner[1]


def ngon(cx, cy, d, n):
    """Regular n-gon (n multiple of 4) with an exactly four-fold symmetric integer
    vertex set, a vertex at angle 0 and bbox extent d (even)."""
    assert n % 4 == 0 and d % 2 == 0
    r = d // 2
    quadrant = []
    for k in range(n // 4):
        a = 2 * math.pi * k / n
        quadrant.append((round(r * math.cos(a)), round(r * math.sin(a))))
    pts = []
    for q in range(4):
        for x, y in quadrant:
            for _ in range(q):
                x, y = -y, x
            pts.append(db.Point(cx + x, cy + y))
    return db.Polygon(pts)


# --------------------------------------------------------------------------- #
# Synthetic world                                                              #
# --------------------------------------------------------------------------- #
METHODS = {
    "cupillar_opt1": {"opening": 35, "enclosure": Decimal("7.5"), "body": 44,
                      "layers": [("CuPillar", "Cu", "28.0", "44.0"), ("SnAgCap", "SnAg", "16.0", "44.0")],
                      "spacing": "40.0", "pitch": "75.0"},
    "cupillar_opt2": {"opening": 40, "enclosure": Decimal("7.5"), "body": 49,
                      "layers": [("CuPillar", "Cu", "32.0", "49.0"), ("SnAgCap", "SnAg", "16.0", "49.0")],
                      "spacing": "40.0", "pitch": "80.0"},
}
ATTACH = {"opening": (9, 35), "dfpad": (41, 35), "body": (500, 35), "body2": (501, 35),
          "mark": (99, 35)}
DECK = """# synthetic interposer adapter (test)
passiv_pillar = polygons(9, 35)
dfpad_pillar  = polygons(41, 35)
passiv_sbump  = polygons(9, 36)
dfpad_sbump   = polygons(41, 36)

chiplet_attachment_input = passiv_pillar.and(dfpad_pillar).join(passiv_sbump.and(dfpad_sbump))
"""


def base_world():
    return {
        "assembly_id": "syn",
        "corner": (100000, -105000),
        "carrier_size": (3000, 2000),
        "interposer": {"position": (1500, 1000), "anchor": (101500, -104000),
                       "rotation": 0, "mirror": False, "top_cell": "CARRIER"},
        "dies": {
            "U1": {"outline": (300, 200), "pads": {"a": (50, 50), "b": (150, 50), "c": (250, 150)},
                   "position": (600, 400), "rotation": 180, "mirror": True, "anchor": (0, 0),
                   "method": "cupillar_opt1", "top_cell": "die_u1", "proxy_cell": "u1_proxy"},
            "U2": {"outline": (200, 300), "pads": {"p": (40, 60), "q": (160, 240)},
                   "position": (1200, 300), "rotation": 90, "mirror": False, "anchor": (20, 30),
                   "method": "cupillar_opt2", "top_cell": "die_u2", "proxy_cell": "u2_proxy"},
        },
        "io_pads": {"J1": (102800, -103200), "J2": (102800, -103400)},
        "contact_order": "die_first",
        "nets": None,
    }


def default_nets(world):
    nets = [{"name": "n_a", "class": "signal", "external": True,
             "connections": [{"component": "U1", "pin": "a"}, {"component": "J1", "pin": "1"}]},
            {"name": "n_b", "connections": [{"component": "U1", "pin": "b"}, {"component": "U2", "pin": "p"}]},
            {"name": "n_c", "connections": [{"component": "U1", "pin": "c"}, {"component": "U2", "pin": "q"},
                                             {"component": "J2", "pin": "1"}]}]
    return nets


def doc_dict(world):
    """The resolved v2 document as plain Python (ints in um)."""
    interposer = world["interposer"]
    comps = [{
        "id": "interposer", "role": "interposer",
        "layout": {"path": "layout/carrier.gds", "top_cell": interposer["top_cell"]},
        "placement": {"position_um": list(interposer["position"]),
                      "rotation_deg": interposer["rotation"], "mirror_x": interposer["mirror"],
                      "anchor_um": list(interposer["anchor"])},
        "layers": [{"source": [50, 0], "technology_layer": "M4"},
                   {"source": [66, 0], "technology_layer": "V4"},
                   {"source": [134, 0], "technology_layer": "TM2"}],
        "io_pads": [{"id": jid, "pin": "1", "position_um": list(pos), "size_um": [100, 100],
                     "technology_layer": "TM2"} for jid, pos in world["io_pads"].items()],
    }]
    terminals = []
    for did, die in world["dies"].items():
        comps.append({
            "id": did, "role": "die",
            "layout": {"path": f"layout/{did.lower()}_proxy.gds", "top_cell": die["proxy_cell"]},
            "placement": {"position_um": list(die["position"]), "rotation_deg": die["rotation"],
                          "mirror_x": die["mirror"], "anchor_um": list(die["anchor"])},
            "layers": [{"source": [900, 0], "technology_layer": "DIE_CONTACT"}],
        })
        for pin, pos in die["pads"].items():
            terminals.append({"id": f"{did}.{pin}.pad0", "component": did, "pin": pin,
                              "technology_layer": "DIE_CONTACT", "position_um": list(pos),
                              "size_um": [22, 22]})
    if world["contact_order"] == "die_first":
        contact = {"id": "die-contact-to-tm2", "layers": ["DIE_CONTACT", "TM2"],
                   "roles": ["die", "interposer"]}
    else:
        contact = {"id": "die-contact-to-tm2", "layers": ["TM2", "DIE_CONTACT"],
                   "roles": ["interposer", "die"]}
    return {
        "schema_version": "assembly-lvs.resolved/v2",
        "assembly": {"id": world["assembly_id"], "units": "um",
                     "interface": {"kind": "2.5d-single", "plane": "xy"}, "components": comps},
        "technology": {"id": "synthetic-stack", "grid_um": 0.001,
                       "conductors": ["M4", "TM2", "DIE_CONTACT"],
                       "vias": [{"layer": "V4", "connects": ["M4", "TM2"]}]},
        "contracts": {"interface_contacts": [contact], "terminals": terminals,
                      "internal_equivalences": [],
                      "logical_nets": world["nets"] or default_nets(world)},
        "uncertainty": {"model": "bounded_linf_translation",
                        "component_translation_um": {"interposer": 0, **{d: 0 for d in world["dies"]}}},
        "provenance": {"generator": "tests/test_resolved_v2_entry.py"},
    }


def body_centres(world):
    """die id -> {pin: G-frame body centre in DBU} from the reference transform."""
    out = {}
    for did, die in world["dies"].items():
        out[did] = {pin: tuple(v * U for v in ref_g(pos, die, world["interposer"], world["corner"]))
                    for pin, pos in die["pads"].items()}
    return out


def _new_layout():
    ly = db.Layout()
    ly.dbu = 0.001
    return ly


def write_carrier(path, world, mutate=None):
    ly = _new_layout()
    top = ly.create_cell(world["interposer"]["top_cell"])
    L = lambda l, d: ly.layer(db.LayerInfo(l, d))  # noqa: E731
    cx, cy = world["corner"]
    w, h = world["carrier_size"]
    top.shapes(L(235, 0)).insert(db.Box(cx * U, cy * U, (cx + w) * U, (cy + h) * U))
    # some copper unrelated to the bodies: a TM2 trace, an M4 plate and a V4 cut
    top.shapes(L(134, 0)).insert(db.Box((cx + 100) * U, (cy + 1800) * U, (cx + 2900) * U, (cy + 1850) * U))
    top.shapes(L(50, 0)).insert(db.Box((cx + 100) * U, (cy + 100) * U, (cx + 400) * U, (cy + 300) * U))
    top.shapes(L(66, 0)).insert(db.Box((cx + 200) * U, (cy + 200) * U, (cx + 210) * U, (cy + 210) * U))
    for jid, (px, py) in world["io_pads"].items():
        top.shapes(L(134, 0)).insert(db.Box((px - 50) * U, (py - 50) * U, (px + 50) * U, (py + 50) * U))
    for did, centres in body_centres(world).items():
        m = METHODS[world["dies"][did]["method"]]
        opening, body = m["opening"] * U, m["body"] * U
        dfpad = opening + 2 * int(m["enclosure"] * U)
        for pin, (gx, gy) in centres.items():
            top.shapes(L(*ATTACH["opening"])).insert(ngon(gx, gy, opening, 256))
            top.shapes(L(*ATTACH["dfpad"])).insert(ngon(gx, gy, dfpad, 256))
            top.shapes(L(*ATTACH["mark"])).insert(ngon(gx, gy, dfpad, 256))
            top.shapes(L(*ATTACH["body"])).insert(ngon(gx, gy, body, 256))
            top.shapes(L(*ATTACH["body2"])).insert(ngon(gx, gy, body, 256))
            top.shapes(L(134, 0)).insert(ngon(gx, gy, dfpad, 256))
    if mutate:
        mutate(ly, top, L)
    ly.write(str(path))


def write_proxy(path, world, did, mutate=None):
    die = world["dies"][did]
    ly = _new_layout()
    top = ly.create_cell(die["proxy_cell"])
    layer = ly.layer(db.LayerInfo(900, 0))
    body = METHODS[die["method"]]["body"] * U
    for pin, (x, y) in die["pads"].items():
        top.shapes(layer).insert(ngon(x * U, y * U, body, 128))
    if mutate:
        mutate(ly, top, layer)
    ly.write(str(path))


def write_adk_die(path, world, did, mutate=None):
    die = world["dies"][did]
    ly = _new_layout()
    top = ly.create_cell(die["top_cell"])
    pads = ly.layer(db.LayerInfo(205, 0))
    texts = ly.layer(db.LayerInfo(205, 25))
    outline = ly.layer(db.LayerInfo(206, 0))
    w, h = die["outline"]
    top.shapes(outline).insert(db.Box(0, 0, w * U, h * U))
    for pin, (x, y) in die["pads"].items():
        top.shapes(pads).insert(db.Box((x - 35) * U, (y - 35) * U, (x + 35) * U, (y + 35) * U))
        top.shapes(texts).insert(db.Text(pin, db.Trans(db.Vector(x * U, y * U))))
    if mutate:
        mutate(ly, top, {"pads": pads, "texts": texts, "outline": outline})
    ly.write(str(path))


def methods_manifest(mutate=None):
    data = {"schema_version": "1.0", "pdk": "interconnect_pdk", "default_method": "cupillar_opt1",
            "methods": {}}
    for mid, m in METHODS.items():
        data["methods"][mid] = {
            "description": f"synthetic {mid}", "vendor": "test", "fab_anchor": "ihp_cupillar_anchor",
            "body_diameter_um": m["body"], "layers_3d": ["CuPillar", "SnAgCap"],
            "connection_stack": {"layers": [{"name": n, "material": mat, "height": float(h),
                                             "diameter": float(d)} for n, mat, h, d in m["layers"]]},
            "fab_params": {"passiv_opening_um": float(m["opening"]),
                           "tm2_enclosure_um": float(m["enclosure"])},
            "pitch_rules": {"IXN_spacing": float(m["spacing"]), "IXN_pitch": float(m["pitch"])},
            "adapter": "syn_ixn",
        }
    data["layer_registry"] = {"CuPillar": {"gds_layer": 500, "gds_datatype": 35},
                              "SnAgCap": {"gds_layer": 501, "gds_datatype": 35}}
    data["fab_anchors"] = {"ihp_cupillar_anchor": {"adk_attachment_adapter": "syn_interposer",
                                                   "layers": {"passiv": "9/35", "dfpad": "41/35",
                                                              "recog": "99/35"}}}
    if mutate:
        mutate(data)
    return data


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_world(root, world=None, *, mutate_doc=None, mutate_carrier=None, mutate_proxy=None,
                mutate_die=None, mutate_manifest=None, mutate_sidecar=None, deck=DECK,
                rebind=True):
    """Write the whole synthetic case under ``root``; return a dict of paths."""
    world = copy.deepcopy(world or base_world())
    root = Path(root)
    case = root / "case"
    (case / "layout").mkdir(parents=True)
    dies = root / "dies"
    dies.mkdir()
    ixn = root / "ixn_pdk" / "manifest"
    ixn.mkdir(parents=True)
    adapters = root / "adapters"
    adapters.mkdir()
    registry = root / "registry"
    registry.mkdir()

    doc = doc_dict(world)
    if mutate_doc:
        doc = mutate_doc(doc) or doc
    doc_path = case / "resolved.json"
    doc_path.write_text(json.dumps(doc, indent=1))
    write_carrier(case / "layout" / "carrier.gds", world, mutate_carrier)
    for did in world["dies"]:
        write_proxy(case / "layout" / f"{did.lower()}_proxy.gds", world, did,
                    mutate_proxy[1] if mutate_proxy and mutate_proxy[0] == did else None)
        write_adk_die(dies / f"{did.lower()}.gds", world, did,
                      mutate_die[1] if mutate_die and mutate_die[0] == did else None)
    manifest_path = ixn / "interconnect_methods.json"
    manifest_path.write_text(json.dumps(methods_manifest(mutate_manifest), indent=1))
    deck_path = adapters / "syn_interposer.drc"
    deck_path.write_text(deck)
    (adapters / "syn_ixn.drc").write_text("interconnect_rules['IXN_pitch'] = 75.0\n")
    (registry / "registry.json").write_text(json.dumps({
        "schema": "adk-registry", "version": "1.0",
        "adapters": {"syn_interposer": str(deck_path), "syn_ixn": str(adapters / "syn_ixn.drc")},
        "roots": {"INTERCONNECT_PDK": str(root / "ixn_pdk")},
    }))
    sidecar = {
        "schema": "adk-resolved-attachment", "version": "1.0.0",
        "document": "resolved.json", "dies_root": "../dies",
        "binds": {"document": sha(doc_path),
                  "layouts": {"interposer": sha(case / "layout" / "carrier.gds"),
                              **{d: sha(case / "layout" / f"{d.lower()}_proxy.gds") for d in world["dies"]}},
                  "die_layouts": {d: sha(dies / f"{d.lower()}.gds") for d in world["dies"]},
                  "methods_manifest": sha(manifest_path), "adapter_deck": sha(deck_path)},
        "interposer": {"adapter": "syn_interposer", "technology": "carrier_tech",
                       "attachment_surface_z": 13.83, "position_z": 0.0, "thickness": 300.0,
                       "attachment_layers": {k: list(v) for k, v in ATTACH.items()},
                       "io_class": "wire_bond", "layer_names": {"TM2": "TopMetal2"}},
        "interconnect": {"adapter": "syn_ixn", "methods_manifest": "manifest/interconnect_methods.json",
                         "technology": {"description": "synthetic attachment", "dbu": 0.001}},
        "technologies": {"carrier_tech": {"description": "synthetic carrier", "dbu": 0.001},
                         "generic": {"description": "synthetic die", "dbu": 0.001}},
        "components": {d: {"method": die["method"], "technology": "generic", "thickness": 250.0,
                           "dimensions": {"width": float(die["outline"][0]),
                                          "height": float(die["outline"][1])},
                           "layout": f"{d.lower()}.gds", "top_cell": die["top_cell"]}
                       for d, die in world["dies"].items()},
    }
    if mutate_sidecar:
        sidecar = mutate_sidecar(sidecar) or sidecar
    if not rebind:
        pass
    sidecar_path = case / "resolved.adk_attachment.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=1))
    return {"root": root, "world": world, "doc": doc_path, "sidecar": sidecar_path,
            "registry": registry, "out": root / "out", "manifest": manifest_path,
            "deck": deck_path, "case": case, "dies": dies}


@pytest.fixture
def registry_env(monkeypatch):
    def apply(paths):
        monkeypatch.setenv("ADK_REGISTRY_DIR", str(paths["registry"]))
    return apply


def run(paths, registry_env):
    registry_env(paths)
    return entry.run_entry(paths["sidecar"], paths["out"])


def refused(paths, registry_env, match):
    registry_env(paths)
    with pytest.raises(entry.Refusal, match=match):
        entry.run_entry(paths["sidecar"], paths["out"])
    assert not paths["out"].exists(), "a refusal must leave nothing behind"


def validate(schema_name, data):
    if jsonschema is None:
        pytest.skip("jsonschema not installed")
    schema = json.loads((SCHEMA_DIR / schema_name).read_text())
    jsonschema.Draft202012Validator(schema).validate(data)


# --------------------------------------------------------------------------- #
# Positive case                                                                #
# --------------------------------------------------------------------------- #
def test_positive_case_end_to_end(tmp_path, registry_env):
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    world = paths["world"]
    m = result.manifest
    assert m["schema"] == "adk-resolved-v2-entry" and m["version"] == "1.0.0"
    assert m["profile"] == entry.PROFILE and "does not prove clean DRC" in m["scope"]
    assert m["resolved"]["all_bounds_zero"] is True
    assert m["resolved"]["counts"] == {"dies": 2, "terminals": 5, "terminals_per_die": {"U1": 3, "U2": 2},
                                       "io_pads": 2, "logical_nets": 3, "endpoints": 7, "bodies": 5}
    # frames: corner, both dies' constants and the +180 identity
    assert m["frames"]["corner_dbu"] == [world["corner"][0] * U, world["corner"][1] * U]
    frames = {f["die"]: f for f in m["frames"]["dies"]}
    assert frames["U1"]["legacy_rotation_deg"] == 0 and frames["U1"]["orientation"] == "flip_chip"
    assert frames["U2"]["legacy_rotation_deg"] == 90 and frames["U2"]["orientation"] == "face_up"
    assert frames["U2"]["klayout_trans"] == {"rot": 1, "mirror": False,
                                             "disp": list(v * U for v in ref_g((0, 0), world["dies"]["U2"], world["interposer"], world["corner"]))}
    # correspondence rows against the reference transform
    centres = body_centres(world)
    bodies_by_centre = {tuple(b["centre_g_dbu"]): b for b in m["bodies"]}
    for did, rows in m["correspondence"].items():
        assert {r["pin"] for r in rows} == set(world["dies"][did]["pads"])
        for r in rows:
            assert tuple(r["body_centre_g_dbu"]) == centres[did][r["pin"]]
            assert r["label"]["text"] == r["pin"]
            assert r["diameters_dbu"]["opening"] == METHODS[world["dies"][did]["method"]]["opening"] * U
            assert r["vertices"] == {k: 256 for k in ATTACH}
            # faceted fixture: the literal enclosure diagnostic is non-zero on every body
            # (the 256-gon apothem is below the circumradius) and propagates faithfully to
            # the body record and the correspondence row; the literal strict containment in
            # the contact conductor is false because the conductor pad is the dfpad itself
            pairs = r["enclosure_edge_pairs_at_manifest_value"]
            assert isinstance(pairs, int) and pairs > 0
            body = bodies_by_centre[tuple(r["body_centre_g_dbu"])]
            assert body["enclosure_edge_pairs_at_manifest_value"] == pairs
            assert body["predicates"] == r["predicates"]
            assert r["predicates"]["literal"] == {
                "enclosing_check_edge_pairs_at_manifest_value": pairs,
                "enclosing_check_empty_at_manifest_value": False,
                "dfpad_strictly_inside_contact": False}
            assert r["predicates"]["profile"] == {"bbox_diameter_relation_exact": True,
                                                  "dfpad_covered_by_contact": True,
                                                  "contact_polygons_met": 1}
            assert body["terminal"] == r["terminal"] and body["method"] == r["method"]
    # outputs validate against the ADK schemas and the vendored reader
    boundaries = json.loads(result.outputs["boundaries"].read_text())
    validate("boundary_manifest.schema.json", boundaries)
    pillars = json.loads(result.outputs["pillars"].read_text())
    validate("pillar_manifest.schema.json", pillars)
    ixn = json.loads(result.outputs["ixn_methods"].read_text())
    validate("ixn_methods.schema.json", ixn)
    sidecar = json.loads(paths["sidecar"].read_text())
    validate("resolved_attachment.schema.json", sidecar)
    chiplet = entry.cfio.load(result.outputs["chiplet"])
    assert [c["id"] for c in chiplet["components"]] == ["interposer", "U1", "U2"]
    carrier = chiplet["components"][0]
    assert carrier["anchor"] == "bbox_center"
    assert (carrier["position"]["x"], carrier["position"]["y"]) == (1500.0, 1000.0)
    assert carrier["dimensions"]["width"] == 3000.0 and carrier["dimensions"]["height"] == 2000.0
    assert carrier["io_pads"][0]["layer"] == "TopMetal2" and carrier["io_pads"][0]["io_class"] == "wire_bond"
    assert carrier["io_pads"][0]["position"] == {"x": 2800.0, "y": 1800.0}
    u1 = chiplet["components"][1]
    assert u1["position"]["z"] == 0.0 + 13.83 + 28.0 + 16.0
    assert u1["rotation"]["z"] == 0.0 and u1["orientation"] == "flip_chip"
    assert u1["dimensions"] == {"width": 300.0, "height": 200.0, "thickness": 250.0}
    c0 = ref_c((0, 0), world["dies"]["U1"], world["interposer"], world["corner"])
    assert (u1["position"]["x"], u1["position"]["y"]) == (float(c0[0]), float(c0[1]))
    u2 = chiplet["components"][2]
    assert u2["position"]["z"] == 0.0 + 13.83 + 32.0 + 16.0 and u2["rotation"]["z"] == 90.0
    assert chiplet["connection_stacks"]["cupillar_opt2"]["layers"][0]["height"] == 32.0
    assert [n["name"] for n in chiplet["netlist"]["nets"]] == ["n_a", "n_b", "n_c"]
    assert chiplet["interconnect"]["technology"]["description"] == "synthetic attachment"
    # pillars in C from the bodies, methods block with the manifest numbers
    assert len(pillars["pillars"]) == 5 and pillars["methods"]["cupillar_opt2"] == {
        "IXN_spacing": 40, "IXN_pitch": 80, "IXN_pad_size": 40}
    by_pin = {(p["device_ref"], p["pin_name"]): p for p in pillars["pillars"]}
    cc = ref_c((250, 150), world["dies"]["U1"], world["interposer"], world["corner"])
    assert (by_pin[("U1", "c")]["x_um"], by_pin[("U1", "c")]["y_um"]) == cc
    assert by_pin[("U2", "p")]["diameter_um"] == 49 and by_pin[("U2", "p")]["moved_by_auto_resolve"] is False
    assert ixn["methods"] == {"cupillar_opt1": {"dies": ["U1"], "IXN_spacing": 40, "IXN_pitch": 75, "IXN_pad_size": 35},
                              "cupillar_opt2": {"dies": ["U2"], "IXN_spacing": 40, "IXN_pitch": 80, "IXN_pad_size": 40}}
    # boundaries: polygon = outline vertices through g(p); origin = g(0)
    b = {x["instance"]: x for x in boundaries["boundaries"]}
    die = world["dies"]["U1"]
    expected = {tuple(v * U for v in ref_g(p, die, world["interposer"], world["corner"]))
                for p in [(0, 0), (0, 200), (300, 200), (300, 0)]}
    assert {tuple(p) for p in b["U1"]["polygon_dbu"]} == expected
    assert b["U1"]["transform"] == {"origin_um": list(ref_g((0, 0), die, world["interposer"], world["corner"])),
                                    "rotation_deg": 0, "mirror_x": True, "magnification": 1}
    assert b["U2"]["transform"]["rotation_deg"] == 90 and b["U2"]["transform"]["mirror_x"] is False
    # each record names its die cell, which the assembled GDS holds (ASM.g prunes it)
    gds_layout = db.Layout()
    gds_layout.read(str(result.outputs["gds"]))
    for record in boundaries["boundaries"]:
        assert gds_layout.cell(record["cell"]) is not None, record["cell"]
    assert boundaries["dbu_um"] == 0.001 and boundaries["assembly_gds_sha256"] == sha(result.outputs["gds"])
    # carrier preservation recorded per layer, XOR zero, no source hash reuse
    pres = m["carrier_preservation"]
    assert set(pres["layers"]) >= {"134/0", "235/0", "9/35", "500/35", "66/0"}
    assert all(v["xor_area_dbu2"] == 0 for v in pres["layers"].values())
    assert m["outputs"]["gds"]["sha256"] != m["inputs"]["layout.interposer"]["sha256"]
    # the assembled GDS: one top cell, the carrier cell and two die cells
    ly = db.Layout()
    ly.read(str(result.outputs["gds"]))
    assert [c.name for c in ly.top_cells()] == ["syn"]
    assert {ly.cell(i).name for i in ly.top_cell().each_child_cell()} == {"CARRIER", "die_u1", "die_u2"}
    assert m["outline_vs_declared"]["U1"] == {"outline_dbu": [300 * U, 200 * U],
                                              "declared_dbu": [300 * U, 200 * U],
                                              "difference_dbu": [0, 0]}
    assert m["next_commands"][0].startswith("klayout/drc/run_drc.py --path syn_assembled.gds")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [False, True])
def test_transform_controls(tmp_path, registry_env, rotation, mirror):
    """Every quarter turn with and without mirror, non-zero anchor, anchor_i - pos_i
    != corner, a die near the carrier edge; hand-computed numbers for U1."""
    world = base_world()
    world["interposer"]["anchor"] = (101400, -104100)   # anchor_i - pos_i = (99900, -105100) != corner
    world["dies"]["U1"].update({"rotation": rotation, "mirror": mirror, "anchor": (10, 20),
                                "position": (900, 700)})
    # U2 near the carrier edge: with anchor_i - pos_i = (99900, -105100) its pad q lands at
    # G (102880, -104880), 92.5 um from the right edge and 120 um from the bottom
    world["dies"]["U2"].update({"position": (2820, 120), "rotation": 0, "mirror": False,
                                "anchor": (0, 0)})
    paths = build_world(tmp_path, world)
    result = run(paths, registry_env)
    m = result.manifest
    die = world["dies"]["U1"]
    # hand computation for the terminal 'c' at (250, 150): mirror then rotate
    x, y = 250 - 10, 150 - 20
    if mirror:
        y = -y
    c, s = _ROT[rotation]
    rx, ry = c * x - s * y, s * x + c * y
    ax, ay = 900 + rx, 700 + ry
    gx, gy = ax - 1500 + 101400, ay - 1000 - 104100
    row = next(r for r in m["correspondence"]["U1"] if r["pin"] == "c")
    assert row["body_centre_g_dbu"] == [gx * U, gy * U]
    assert row["body_centre_c_dbu"] == [(gx - 100000) * U, (gy + 105000) * U]
    frame = next(f for f in m["frames"]["dies"] if f["die"] == "U1")
    assert frame["legacy_rotation_deg"] == ((rotation + 180) % 360 if mirror else rotation)
    ox, oy = -10, -20
    if mirror:
        oy = -oy
    rx, ry = c * ox - s * oy, s * ox + c * oy
    assert frame["g_origin"] == [(900 + rx - 1500 + 101400) * U, (700 + ry - 1000 - 104100) * U]
    boundaries = json.loads(result.outputs["boundaries"].read_text())
    poly = {tuple(p) for p in next(b for b in boundaries["boundaries"] if b["instance"] == "U1")["polygon_dbu"]}
    for vertex in [(300, 0), (0, 200)]:
        assert tuple(v * U for v in ref_g(vertex, die, world["interposer"], world["corner"])) in poly


def test_both_interface_contact_orders(tmp_path, registry_env):
    world = base_world()
    world["contact_order"] = "interposer_first"
    paths = build_world(tmp_path, world)
    result = run(paths, registry_env)
    assert result.manifest["resolved"]["interface_contacts"][0]["pairs"] == [["interposer", "TM2"], ["die", "DIE_CONTACT"]]


def test_yaml_document_is_accepted(tmp_path, registry_env):
    paths = build_world(tmp_path)
    doc = json.loads(paths["doc"].read_text())
    ypath = paths["case"] / "resolved.yaml"
    ypath.write_text(yaml.safe_dump(doc, sort_keys=False))
    sidecar = json.loads(paths["sidecar"].read_text())
    sidecar["document"] = "resolved.yaml"
    sidecar["binds"]["document"] = sha(ypath)
    paths["sidecar"].write_text(json.dumps(sidecar))
    run(paths, registry_env)


def test_cli_exit_codes(tmp_path, registry_env, capsys):
    paths = build_world(tmp_path)
    registry_env(paths)
    assert entry.main(["--sidecar", str(paths["sidecar"]), "--out", str(paths["out"])]) == 0
    assert entry.main(["--sidecar", str(paths["sidecar"]), "--out", str(paths["out"])]) == 2
    assert "output directory exists" in capsys.readouterr().err
    assert entry.main(["--sidecar", str(tmp_path / "missing.json"), "--out", str(tmp_path / "o2")]) == 1
    sidecar = json.loads(paths["sidecar"].read_text())
    sidecar["interposer"]["adapter"] = "pdk_adapters/interposer/intm4tm2.drc"
    bad = tmp_path / "case" / "bad.json"
    bad.write_text(json.dumps(sidecar))
    assert entry.main(["--sidecar", str(bad), "--out", str(tmp_path / "o3")]) == 1
    assert not (tmp_path / "o3").exists()


# --------------------------------------------------------------------------- #
# Decoder refusals                                                             #
# --------------------------------------------------------------------------- #
def _terminal(doc, tid):
    return next(t for t in doc["contracts"]["terminals"] if t["id"] == tid)


def _component(doc, cid):
    return next(c for c in doc["assembly"]["components"] if c["id"] == cid)


DOC_REFUSALS = {
    "duplicate_terminal_id": (lambda d: _terminal(d, "U2.q.pad0").update(id="U1.a.pad0"), "terminal IDs must be unique"),
    "duplicate_component_id": (lambda d: _component(d, "U2").update(id="U1"), "component IDs must be unique"),
    "duplicate_net_name": (lambda d: d["contracts"]["logical_nets"][1].update(name="n_a"), "net names must be unique"),
    "duplicate_io_pad_id": (lambda d: _component(d, "interposer")["io_pads"][1].update(id="J1"), "I/O pad IDs must be unique"),
    "duplicate_io_pad_tuple": (lambda d: _component(d, "interposer")["io_pads"][1].update(id="J1"), "I/O pad IDs must be unique"),
    "technology_layer_mapped_twice": (lambda d: _component(d, "interposer")["layers"].append({"source": [135, 0], "technology_layer": "TM2"}), "only one GDS source"),
    "source_mapped_twice": (lambda d: _component(d, "interposer")["layers"].append({"source": [134, 0], "technology_layer": "M4"}), "mapped only once"),
    "off_grid_coordinate": (lambda d: _terminal(d, "U1.a.pad0").update(position_um=[50.0005, 50]), "not aligned"),
    "boolean_as_number": (lambda d: _terminal(d, "U1.a.pad0").update(position_um=[True, 50]), "finite decimal"),
    "string_as_number": (lambda d: _terminal(d, "U1.a.pad0").update(position_um=["50", 50]), "finite decimal"),
    "odd_window_extent": (lambda d: _terminal(d, "U1.a.pad0").update(size_um=[22.001, 22]), "representable on the technology grid"),
    "negative_size": (lambda d: _terminal(d, "U1.a.pad0").update(size_um=[-22, 22]), "greater than zero"),
    "outside_safe_range": (lambda d: _terminal(d, "U1.a.pad0").update(position_um=[1073741824, 50]), "exact-safe range"),
    "bad_rotation": (lambda d: _component(d, "U1")["placement"].update(rotation_deg=45), "one of 0, 90, 180, 270"),
    "layout_escapes": (lambda d: _component(d, "U1")["layout"].update(path="../u1_proxy.gds"), "must not escape"),
    "layout_absolute": (lambda d: _component(d, "U1")["layout"].update(path="/tmp/u1_proxy.gds"), "must be relative"),
    "layout_backslash": (lambda d: _component(d, "U1")["layout"].update(path="layout\\u1_proxy.gds"), "portable"),
    "layout_missing": (lambda d: _component(d, "U1")["layout"].update(path="layout/nope.gds"), "does not exist"),
    "unknown_pin_endpoint": (lambda d: d["contracts"]["logical_nets"][0]["connections"].append({"component": "U1", "pin": "zz"}), "no physical terminal contract"),
    "endpoint_in_two_nets": (lambda d: d["contracts"]["logical_nets"][1]["connections"].append({"component": "U1", "pin": "a"}), "already assigned"),
    "endpoint_twice_in_net": (lambda d: d["contracts"]["logical_nets"][0]["connections"].append({"component": "U1", "pin": "a"}), "already assigned"),
    "endpoint_in_no_net": (lambda d: d["contracts"]["logical_nets"][0]["connections"].pop(1), "missing from logical partition"),
    "endpoint_declared_layer_mismatch": (lambda d: d["contracts"]["logical_nets"][0]["connections"][0].update(layer="TM2"), "declared layer does not match"),
    "certificate_present": (lambda d: d["contracts"]["internal_equivalences"].append({"id": "e", "component": "U1", "terminals": ["U1.a.pad0"], "evidence": {}}), "internal_equivalences must be empty"),
    "interposer_rotated": (lambda d: _component(d, "interposer")["placement"].update(rotation_deg=90), "identity"),
    "interposer_mirrored": (lambda d: _component(d, "interposer")["placement"].update(mirror_x=True), "identity"),
    "unknown_key": (lambda d: d.update(extra=1), "unknown keys"),
    "unknown_key_nested": (lambda d: _terminal(d, "U1.a.pad0").update(note="x"), "unknown keys"),
    "missing_required_key": (lambda d: d["contracts"].pop("terminals"), "missing required keys"),
    "wrong_schema_version": (lambda d: d.update(schema_version="assembly-lvs.resolved/v1"), "schema_version"),
    "wrong_units": (lambda d: d["assembly"].update(units="mm"), "must use 'um'"),
    "via_connects_one": (lambda d: d["technology"]["vias"][0].update(connects=["M4"]), "exactly two conductors"),
    "via_connects_unknown": (lambda d: d["technology"]["vias"][0].update(connects=["M4", "M9"]), "unknown conductor"),
    "conductor_declared_twice": (lambda d: d["technology"].update(conductors=["M4", "TM2", "TM2", "DIE_CONTACT"]), "duplicate values"),
    "via_named_as_conductor": (lambda d: d["technology"]["vias"][0].update(layer="M4"), "duplicate technology layer"),
    "contact_roles_reversed_only": (lambda d: d["contracts"]["interface_contacts"][0].update(roles=["interposer", "die"]), "interposer-side conductor"),
    "contact_unknown_role": (lambda d: d["contracts"]["interface_contacts"][0].update(roles=["die", "package"]), "must join die and interposer"),
    "contact_layer_is_via": (lambda d: d["contracts"]["interface_contacts"][0].update(layers=["DIE_CONTACT", "V4"]), "only conductors"),
    "terminal_on_interposer": (lambda d: _terminal(d, "U1.a.pad0").update(component="interposer"), "die-side only"),
    "terminal_wrong_layer": (lambda d: _terminal(d, "U1.a.pad0").update(technology_layer="TM2"), "does not map layer"),
    "terminal_reserved_namespace": (lambda d: _terminal(d, "U1.a.pad0").update(id="io_pad:x"), "reserved namespace"),
    "two_terminals_one_port": (lambda d: d["contracts"]["terminals"].append(dict(_terminal(d, "U1.a.pad0"), id="U1.a.pad1", position_um=[50, 60])), "multi-geometry port"),
    "io_pads_on_die": (lambda d: _component(d, "U1").update(io_pads=[{"id": "K", "pin": "1", "position_um": [0, 0], "size_um": [10, 10], "technology_layer": "TM2"}]), "only the interposer"),
    "io_pad_unmapped_conductor": (lambda d: _component(d, "interposer")["io_pads"][0].update(technology_layer="DIE_CONTACT"), "unmapped conductor"),
    "io_pad_on_via": (lambda d: _component(d, "interposer")["io_pads"][0].update(technology_layer="V4"), "conductor layer"),
    "uncertainty_nonzero": (lambda d: d["uncertainty"]["component_translation_um"].update(U1=0.5), "non-zero translation bounds"),
    "uncertainty_negative": (lambda d: d["uncertainty"]["component_translation_um"].update(U1=-1), "non-negative"),
    "uncertainty_missing_component": (lambda d: d["uncertainty"]["component_translation_um"].pop("U2"), "missing components"),
    "uncertainty_unknown_component": (lambda d: d["uncertainty"]["component_translation_um"].update(U9=0), "unknown components"),
    "uncertainty_wrong_model": (lambda d: d["uncertainty"].update(model="gaussian"), "bounded_linf_translation"),
    "grid_not_positive": (lambda d: d["technology"].update(grid_um=0), "greater than zero"),
    "die_maps_two_contact_layers": (lambda d: (d["technology"]["conductors"].append("DIE_CONTACT2"),
                                                d["contracts"]["interface_contacts"].append({"id": "c2", "layers": ["DIE_CONTACT2", "TM2"], "roles": ["die", "interposer"]}),
                                                _component(d, "U1")["layers"].append({"source": [901, 0], "technology_layer": "DIE_CONTACT2"})), "exactly one die-side"),
    # LVS finding: a second contact on the same die-side conductor must not hide behind a
    # first-match lookup, in either authored role order
    "contact_second_carrier_conductor": (lambda d: d["contracts"]["interface_contacts"].append(
        {"id": "c2", "layers": ["DIE_CONTACT", "M4"], "roles": ["die", "interposer"]}), "declared in 2 contacts"),
    "contact_second_carrier_conductor_reversed": (lambda d: d["contracts"]["interface_contacts"].append(
        {"id": "c2", "layers": ["M4", "DIE_CONTACT"], "roles": ["interposer", "die"]}), "declared in 2 contacts"),
    "contact_pairing_repeated_reversed": (lambda d: d["contracts"]["interface_contacts"].append(
        {"id": "c2", "layers": ["TM2", "DIE_CONTACT"], "roles": ["interposer", "die"]}), "declared in 2 contacts"),
    "contacts_two_carrier_conductors": (lambda d: (d["technology"]["conductors"].append("DIE_CONTACT2"),
                                                   d["contracts"]["interface_contacts"].append({"id": "c2", "layers": ["DIE_CONTACT2", "M4"], "roles": ["die", "interposer"]})),
                                        "one interposer contact conductor, declared"),
    # LVS finding: assembly.id reached the output paths; only a plain path component is a stem
    "assembly_id_traversal": (lambda d: d["assembly"].update(id="../escape"), "not usable as an output file stem"),
    "assembly_id_absolute": (lambda d: d["assembly"].update(id="/tmp/escape"), "not usable as an output file stem"),
    "assembly_id_nested": (lambda d: d["assembly"].update(id="a/b"), "not usable as an output file stem"),
    "assembly_id_dot": (lambda d: d["assembly"].update(id=".hidden"), "not usable as an output file stem"),
    "empty_components": (lambda d: d["assembly"].update(components=[]), "must not be empty"),
    "two_interposers": (lambda d: _component(d, "U2").update(role="interposer"), "exactly one interposer"),
    "provenance_missing_generator": (lambda d: d["provenance"].pop("generator"), "missing required keys"),
    "net_without_connections": (lambda d: d["contracts"]["logical_nets"].append({"name": "empty", "connections": []}), "must not be empty"),
}


@pytest.mark.parametrize("name", sorted(DOC_REFUSALS))
def test_decoder_refusals(tmp_path, registry_env, name):
    mutate, match = DOC_REFUSALS[name]

    def apply(doc):
        mutate(doc)
        return doc

    paths = build_world(tmp_path, mutate_doc=apply)
    refused(paths, registry_env, match)


def test_syntax_level_refusals(tmp_path):
    with pytest.raises(entry.Refusal, match="duplicate key"):
        entry.load_strict_json('{"a": 1, "a": 2}')
    with pytest.raises(entry.Refusal, match="non-finite"):
        entry.load_strict_json('{"a": NaN}')
    with pytest.raises(entry.Refusal, match="non-finite"):
        entry.load_strict_json('{"a": Infinity}')
    with pytest.raises(entry.Refusal, match="duplicate key"):
        entry.load_strict_yaml("a: 1\na: 2\n")
    with pytest.raises(entry.Refusal, match="plain decimal"):
        entry.load_strict_yaml("a: 0x1F\n")
    assert entry.load_strict_json('{"a": 4800, "b": 0.001, "c": 1e3}') == {
        "a": Decimal("4800"), "b": Decimal("0.001"), "c": Decimal("1e3")}
    assert entry.load_strict_yaml("a: 4800\nb: 0.001\nc: .nan\n")["c"].is_nan()
    with pytest.raises(entry.Refusal, match="finite decimal"):
        entry._decimal(entry.load_strict_yaml("c: .nan\n")["c"], "c")
    with pytest.raises(entry.Refusal, match="finite decimal"):
        entry._decimal(entry.load_strict_yaml("c: .inf\n")["c"], "c")


def test_document_nan_via_yaml_is_refused(tmp_path, registry_env):
    paths = build_world(tmp_path)
    doc = json.loads(paths["doc"].read_text())
    _terminal(doc, "U1.a.pad0")["position_um"] = [".nan", 50]
    text = yaml.safe_dump(doc, sort_keys=False).replace("'.nan'", ".nan")
    ypath = paths["case"] / "resolved.yaml"
    ypath.write_text(text)
    sidecar = json.loads(paths["sidecar"].read_text())
    sidecar["document"] = "resolved.yaml"
    sidecar["binds"]["document"] = sha(ypath)
    paths["sidecar"].write_text(json.dumps(sidecar))
    refused(paths, registry_env, "finite decimal")


def test_layout_through_symlink_is_refused(tmp_path, registry_env):
    paths = build_world(tmp_path)
    real = paths["case"] / "layout" / "u1_proxy.gds"
    moved = tmp_path / "elsewhere.gds"
    shutil.move(str(real), str(moved))
    real.symlink_to(moved)
    refused(paths, registry_env, "symlink")


# --------------------------------------------------------------------------- #
# Sidecar, binding, registry and files                                         #
# --------------------------------------------------------------------------- #
def _sidecar_mut(fn):
    def apply(sidecar):
        fn(sidecar)
        return sidecar
    return apply


SIDECAR_REFUSALS = {
    "binds_document_stale": (lambda s: s["binds"].update(document="0" * 64), "sidecar.binds.document"),
    "binds_layout_stale": (lambda s: s["binds"]["layouts"].update(U1="0" * 64), "sidecar.binds.layouts.U1"),
    "binds_die_layout_stale": (lambda s: s["binds"]["die_layouts"].update(U2="0" * 64), "sidecar.binds.die_layouts.U2"),
    "binds_manifest_stale": (lambda s: s["binds"].update(methods_manifest="0" * 64), "methods_manifest"),
    "binds_deck_stale": (lambda s: s["binds"].update(adapter_deck="0" * 64), "adapter_deck"),
    "inventory_names_extra_layout": (lambda s: s["binds"]["layouts"].update(U9="0" * 64), "exactly the components"),
    "inventory_misses_die": (lambda s: s["binds"]["die_layouts"].pop("U2"), "exactly the dies"),
    "component_not_a_die": (lambda s: s["components"].update(U9=dict(s["components"]["U1"])), "exactly the dies"),
    "attachment_layers_differ_from_deck": (lambda s: s["interposer"]["attachment_layers"].update(opening=[9, 36]), "adapter deck"),
    "methods_manifest_escapes": (lambda s: s["interconnect"].update(methods_manifest="../x.json"), "must not escape"),
    "die_layout_escapes": (lambda s: s["components"]["U1"].update(layout="../case/resolved.json"), "must not escape"),
    "technology_dbu_differs": (lambda s: s["technologies"]["generic"].update(dbu=0.01), "differs from grid_um"),
    "unknown_method": (lambda s: s["components"]["U1"].update(method="cupillar_opt9"), "not in the manifest"),
    "declared_smaller_than_outline": (lambda s: s["components"]["U1"]["dimensions"].update(width=299.0), "is not the declared"),
    "declared_larger_than_outline": (lambda s: s["components"]["U1"]["dimensions"].update(height=201.0), "is not the declared"),
    "declared_wider_than_outline": (lambda s: s["components"]["U1"]["dimensions"].update(width=301.0), "is not the declared"),
    "declared_shorter_than_outline": (lambda s: s["components"]["U1"]["dimensions"].update(height=199.0), "is not the declared"),
    "declared_one_grid_step_off_outline": (lambda s: s["components"]["U1"]["dimensions"].update(height=200.001),
                                           "outline 300000x200000 DBU is not the declared 300000x200001 DBU"),
    "zero_thickness": (lambda s: s["components"]["U1"].update(thickness=0), "greater than zero"),
    "unknown_technology": (lambda s: s["components"]["U1"].update(technology="nope"), "unknown technology"),
    "wrong_version": (lambda s: s.update(version="2.0.0"), "sidecar.version"),
    "unknown_sidecar_key": (lambda s: s.update(extra=1), "unknown keys"),
    "top_cell_missing": (lambda s: s["components"]["U1"].update(top_cell="nope"), "not found"),
}


@pytest.mark.parametrize("name", sorted(SIDECAR_REFUSALS))
def test_sidecar_refusals(tmp_path, registry_env, name):
    mutate, match = SIDECAR_REFUSALS[name]
    paths = build_world(tmp_path, mutate_sidecar=_sidecar_mut(mutate))
    refused(paths, registry_env, match)


def test_registry_path_shaped_adapter_and_unknown_id(tmp_path, registry_env):
    paths = build_world(tmp_path, mutate_sidecar=_sidecar_mut(
        lambda s: s["interposer"].update(adapter="pdk_adapters/interposer/intm4tm2.drc")))
    registry_env(paths)
    with pytest.raises(entry.adk_registry.IdLookupError):
        entry.run_entry(paths["sidecar"], paths["out"])
    assert not paths["out"].exists()
    paths2 = build_world(tmp_path / "b", mutate_sidecar=_sidecar_mut(
        lambda s: s["interposer"].update(adapter="unknown_adapter")))
    registry_env(paths2)
    with pytest.raises(entry.adk_registry.IdLookupError):
        entry.run_entry(paths2["sidecar"], paths2["out"])


def test_method_adapter_must_match_run(tmp_path, registry_env):
    def mutate(m):
        m["methods"]["cupillar_opt1"]["adapter"] = "other_ixn"
    paths = build_world(tmp_path, mutate_manifest=mutate)
    refused(paths, registry_env, "belongs to adapter")


def test_deck_parse_shapes():
    pairs = entry.parse_attachment_deck(DECK)
    assert pairs == {"pillar": ((9, 35), (41, 35)), "sbump": ((9, 36), (41, 36))}
    fixture = (ADK_ROOT / "tests" / "fixtures" / "test_interposer_adapter.drc").read_text()
    with pytest.raises(entry.Refusal, match="exactly one"):
        entry.parse_attachment_deck(fixture)
    with pytest.raises(entry.Refusal, match="undefined polygons"):
        entry.parse_attachment_deck("chiplet_attachment_input = a.and(b).join(c.and(d))\n")
    real = (ADK_ROOT / "pdk_adapters" / "interposer" / "intm4tm2.drc").read_text()
    assert entry.parse_attachment_deck(real) == pairs


def test_grid_must_be_power_of_ten_and_layout_dbu_must_match(tmp_path, registry_env):
    def mutate(doc):
        doc["technology"]["grid_um"] = 0.002
        return doc
    paths = build_world(tmp_path, mutate_doc=mutate)
    refused(paths, registry_env, "not aligned|power of ten")

    def carrier_dbu(ly, top, L):
        ly.dbu = 0.0005
    paths2 = build_world(tmp_path / "b", mutate_carrier=carrier_dbu)
    refused(paths2, registry_env, "dbu")


# --------------------------------------------------------------------------- #
# Carrier bodies, shape controls and preservation (sections 3, 7)              #
# --------------------------------------------------------------------------- #
def _u1_a_centre(world):
    return body_centres(world)["U1"]["a"]


def test_body_with_hole_is_refused(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        hole = db.Box(gx - 2000, gy - 2000, gx + 2000, gy + 2000)
        region = db.Region(top.shapes(L(500, 35))) - db.Region(hole)
        top.shapes(L(500, 35)).clear()
        top.shapes(L(500, 35)).insert(region)
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "hole")


def test_non_square_opening_is_refused(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        top.shapes(L(9, 35)).insert(db.Box(gx - 20000, gy - 1000, gx + 20000, gy + 1000))
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "not square")


def test_offset_opening_is_refused(tmp_path, registry_env):
    """An opening shifted inside its dfpad: the centre sets no longer coincide."""
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        region = db.Region(top.shapes(L(9, 35)))
        moved = db.Region()
        for p in region.each():
            if p.bbox().contains(db.Point(gx, gy)):
                moved.insert(p.moved(3000, 0))
            else:
                moved.insert(p)
        top.shapes(L(9, 35)).clear()
        top.shapes(L(9, 35)).insert(moved)
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "centre set differs")


def test_tm2_gap_under_dfpad_is_refused(tmp_path, registry_env):
    """A 1 um gap cut through the contact pad under one dfpad removes copper under it,
    so the coverage predicate fails (typed refusal, no output). This is not an
    isolated test of the "exactly one merged polygon" count: a dfpad fully covered by
    the conductor lies inside one connected merged region, so the count is a
    consistency record that cannot fail on its own."""
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        region = db.Region(top.shapes(L(134, 0)))
        cut = db.Region(db.Box(gx - 500, gy - 30000, gx + 500, gy + 30000))
        pad = region.interacting(db.Region(db.Box(gx - 1, gy - 1, gx + 1, gy + 1)))
        rest = region - pad
        top.shapes(L(134, 0)).clear()
        top.shapes(L(134, 0)).insert(rest + (pad - cut))
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "not covered")


def _replace_shapes(top, layer, centres, make):
    """Replace, on ``layer``, the polygon whose bbox centre is each of ``centres`` by
    ``make(gx, gy)``; every other shape on the layer is kept."""
    region = db.Region(top.shapes(layer))
    out = db.Region()
    for p in region.each():
        c = p.bbox().center()
        hit = next((g for g in centres if (c.x, c.y) == g), None)
        out.insert(make(*hit) if hit else p)
    top.shapes(layer).clear()
    top.shapes(layer).insert(out)


def _square(cx, cy, d):
    return db.Polygon(db.Box(cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2))


def test_same_bbox_square_opening_is_refused_by_containment(tmp_path, registry_env):
    """Same bbox, distorted shape, precise refusal from an existing independent guard: a
    35 x 35 um square opening keeps the bbox diameter, the centre and the point symmetry,
    but its corners lie outside the 44 um body, so the strict-containment guard refuses
    it by name (no claim of general artwork conformity is involved)."""
    world = base_world()
    u1 = list(body_centres(world)["U1"].values())

    def mutate(ly, top, L):
        _replace_shapes(top, L(9, 35), u1, lambda gx, gy: _square(gx, gy, 35000))
    paths = build_world(tmp_path, world, mutate_carrier=mutate)
    refused(paths, registry_env, "opening is not strictly inside its body")


def test_same_bbox_square_dfpad_is_accepted_as_nominal_limit(tmp_path, registry_env):
    """Accepted nominal-profile limit, documented: every opt1 dfpad (and the contact pad
    under it) drawn as a 50 x 50 um square instead of a 256-gon keeps the bbox diameter,
    the integer centre, the point symmetry and every containment, so the profile accepts
    it and records 4 vertices; the guards enforce the bbox-diameter relation and
    concentricity, not a regular polygon. The literal enclosure diagnostic is 0 here (the
    opening's axis vertices sit exactly 7.5 um from the square's edges), which is data,
    not a clearance claim."""
    world = base_world()
    u1 = list(body_centres(world)["U1"].values())

    def mutate(ly, top, L):
        for layer in (L(41, 35), L(134, 0)):
            _replace_shapes(top, layer, u1, lambda gx, gy: _square(gx, gy, 50000))
    paths = build_world(tmp_path, world, mutate_carrier=mutate)
    result = run(paths, registry_env)
    rows = result.manifest["correspondence"]["U1"]
    assert all(r["vertices"]["dfpad"] == 4 and r["diameters_dbu"]["dfpad"] == 50000 for r in rows)
    assert all(r["predicates"]["literal"]["enclosing_check_edge_pairs_at_manifest_value"] == 0 for r in rows)
    assert all(r["predicates"]["profile"]["dfpad_covered_by_contact"] is True for r in rows)
    assert all(r["vertices"]["dfpad"] == 256 for r in result.manifest["correspondence"]["U2"])


def test_same_bbox_asymmetric_distortion_is_refused(tmp_path, registry_env):
    """Same bbox, one vertex pulled inward: the point-symmetry guard refuses it by name."""
    world = base_world()
    gx, gy = body_centres(world)["U1"]["a"]

    def mutate(ly, top, L):
        def distorted(cx, cy):
            pts = list(ngon(cx, cy, 35000, 256).each_point_hull())
            pts[10] = db.Point(cx + 9000, cy + 3000)   # inward, away from the axis extremes
            return db.Polygon(pts)
        _replace_shapes(top, L(9, 35), [(gx, gy)], distorted)
    paths = build_world(tmp_path, world, mutate_carrier=mutate)
    refused(paths, registry_env, "not point-symmetric")


def test_same_bbox_symmetric_notches_are_accepted_with_diagnostics(tmp_path, registry_env):
    """Accepted nominal-profile limit, documented: two point-symmetric notches cut into
    every opt1 dfpad on the 45 degree diagonals keep the bbox, the centre, the symmetry
    and the containments, so the profile accepts the body while the local clearance to
    the opening drops well below the manifest enclosure. The literal enclosure diagnostic
    grows and is recorded; the profile enforces no numerical local-clearance bound."""
    world = base_world()
    u1 = list(body_centres(world)["U1"].values())
    baseline = run(build_world(tmp_path / "base", world), registry_env)
    base_pairs = {r["pin"]: r["enclosure_edge_pairs_at_manifest_value"]
                  for r in baseline.manifest["correspondence"]["U1"]}

    def mutate(ly, top, L):
        def notched(cx, cy):
            region = db.Region(ngon(cx, cy, 50000, 256))
            for sx, sy in ((1, 1), (-1, -1)):
                region -= db.Region(db.Box(cx + sx * 16500, cy + sy * 16500, cx + sx * 19500, cy + sy * 19500))
            return list(region.merged().each())[0]
        _replace_shapes(top, L(41, 35), u1, notched)
    paths = build_world(tmp_path / "notched", world, mutate_carrier=mutate)
    result = run(paths, registry_env)
    for r in result.manifest["correspondence"]["U1"]:
        assert r["diameters_dbu"]["dfpad"] == 50000 and r["vertices"]["dfpad"] != 256
        assert r["enclosure_edge_pairs_at_manifest_value"] > base_pairs[r["pin"]]
        assert r["predicates"]["profile"]["bbox_diameter_relation_exact"] is True


def test_hostile_assembly_id_writes_nothing_outside_out_dir(tmp_path, registry_env):
    """Sentinel control for the LVS finding: an assembly.id carrying a path separator is
    refused before any output exists, and the tree around out_dir is unchanged (the
    pre-fix stem would have written ``<out_dir>/../escape_assembled.gds``)."""
    paths = build_world(tmp_path, mutate_doc=lambda d: (d["assembly"].update(id="../escape"), d)[1])
    sentinel = tmp_path / "escape_assembled.gds"
    assert not sentinel.exists()
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    refused(paths, registry_env, "not usable as an output file stem")
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after and not sentinel.exists()
    # second gate, independent of the document: no output name may leave out_dir
    for name in ("../x.gds", "a/b.gds", ".", "..", "", "/abs.gds", ".hidden"):
        with pytest.raises(entry.Refusal, match="not a plain file name"):
            entry._output_name(tmp_path, name)
    assert entry._output_name(tmp_path, "syn_assembled.gds") == tmp_path / "syn_assembled.gds"


def test_dfpad_on_no_copper_is_refused(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        region = db.Region(top.shapes(L(134, 0)))
        pad = region.interacting(db.Region(db.Box(gx - 1, gy - 1, gx + 1, gy + 1)))
        top.shapes(L(134, 0)).clear()
        top.shapes(L(134, 0)).insert(region - pad)
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "not covered")


def test_opening_not_covered_by_body_is_refused(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        for layer in ((500, 35), (501, 35)):
            region = db.Region(top.shapes(L(*layer)))
            small = db.Region()
            for p in region.each():
                if p.bbox().contains(db.Point(gx, gy)):
                    small.insert(ngon(gx, gy, 30000, 256))
                else:
                    small.insert(p)
            top.shapes(L(*layer)).clear()
            top.shapes(L(*layer)).insert(small)
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "opening is not strictly inside its body|differs from the method")


def test_body_diameter_against_altered_manifest(tmp_path, registry_env):
    def mutate(m):
        m["methods"]["cupillar_opt1"]["body_diameter_um"] = 45
    paths = build_world(tmp_path, mutate_manifest=mutate)
    refused(paths, registry_env, "body extent 44000 DBU differs from the method")

    def mutate2(m):
        m["methods"]["cupillar_opt2"]["fab_params"]["tm2_enclosure_um"] = 8.0
    paths2 = build_world(tmp_path / "b", mutate_manifest=mutate2)
    refused(paths2, registry_env, "dfpad extent 55000 DBU differs")


def test_mark_bbox_must_equal_dfpad_bbox(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        region = db.Region(top.shapes(L(99, 35)))
        out = db.Region()
        for p in region.each():
            out.insert(ngon(gx, gy, 52000, 256) if p.bbox().contains(db.Point(gx, gy)) else p)
        top.shapes(L(99, 35)).clear()
        top.shapes(L(99, 35)).insert(out)
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "mark bbox")


def test_sbump_branch_must_be_empty(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        top.shapes(L(9, 36)).insert(ngon(gx + 400000, gy, 80000, 256))
        top.shapes(L(41, 36)).insert(ngon(gx + 400000, gy, 90000, 256))
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "sbump branch")


def test_unused_body_is_refused(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        gx += 500000
        for role, d in (("opening", 35000), ("dfpad", 50000), ("mark", 50000), ("body", 44000), ("body2", 44000)):
            top.shapes(L(*ATTACH[role])).insert(ngon(gx, gy, d, 256))
        top.shapes(L(134, 0)).insert(ngon(gx, gy, 50000, 256))
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "host no terminal")


def test_vertex_count_must_agree_within_a_method(tmp_path, registry_env):
    def mutate(ly, top, L):
        gx, gy = _u1_a_centre(base_world())
        region = db.Region(top.shapes(L(500, 35)))
        out = db.Region()
        for p in region.each():
            out.insert(ngon(gx, gy, 44000, 128) if p.bbox().contains(db.Point(gx, gy)) else p)
        top.shapes(L(500, 35)).clear()
        top.shapes(L(500, 35)).insert(out)
    paths = build_world(tmp_path, mutate_carrier=mutate)
    refused(paths, registry_env, "vertex counts")


def test_carrier_preservation_records_every_layer_and_texts(tmp_path, registry_env):
    def mutate(ly, top, L):
        top.shapes(L(134, 0)).insert(db.Text("VDD", db.Trans(db.Vector(100500000, -103100000))))
        top.shapes(L(70, 0)).insert(db.Path([db.Point(100100000, -104900000), db.Point(100900000, -104900000)], 5000))
    paths = build_world(tmp_path, mutate_carrier=mutate)
    result = run(paths, registry_env)
    layers = result.manifest["carrier_preservation"]["layers"]
    assert layers["134/0"]["source"]["texts"] == 1 and layers["134/0"]["carrier"]["texts"] == 1
    assert layers["70/0"]["source"] == layers["70/0"]["carrier"] and layers["70/0"]["xor_area_dbu2"] == 0


def test_preservation_xor_detects_a_mutated_copy(tmp_path, registry_env):
    """Direct control of the proof: a carrier cell missing a shape fails the XOR."""
    paths = build_world(tmp_path)
    doc = json.loads(paths["doc"].read_text())
    src = entry.open_layout(paths["case"] / "layout" / "carrier.gds", GRID, "CARRIER", "t")
    out = db.Layout()
    out.dbu = 0.001
    cell = entry.copy_carrier(src, out, "CARRIER")
    assert entry.prove_preservation(src, cell, out)["layers"]["134/0"]["xor_area_dbu2"] == 0
    layer = out.find_layer(134, 0)
    region = db.Region(cell.shapes(layer))
    cell.shapes(layer).clear()
    cell.shapes(layer).insert(region - db.Region(db.Box(100200000, -103200000, 100300000, -103150000)))
    with pytest.raises(entry.Refusal, match="not preserved"):
        entry.prove_preservation(src, cell, out)


def test_hierarchical_carrier_is_flattened_and_preserved(tmp_path, registry_env):
    """A carrier whose top cell instances the artwork (the 'equivalent' shape)."""
    def mutate(ly, top, L):
        child = ly.create_cell("INNER")
        child.shapes(L(50, 0)).insert(db.Box(0, 0, 100000, 100000))
        top.insert(db.CellInstArray(child.cell_index(), db.Trans(db.Vector(100500000, -104500000))))
    paths = build_world(tmp_path, mutate_carrier=mutate)
    result = run(paths, registry_env)
    assert result.manifest["carrier_preservation"]["layers"]["50/0"]["source"]["shapes"] == 2


# --------------------------------------------------------------------------- #
# Correspondence controls                                                      #
# --------------------------------------------------------------------------- #
def test_moved_label_is_refused(tmp_path, registry_env):
    def mutate(ly, top, layers):
        for s in top.shapes(layers["texts"]).each():
            if s.text_string == "a":
                s.text_trans = db.Trans(db.Vector(150 * U, 50 * U))  # onto pad b
    paths = build_world(tmp_path, mutate_die=("U1", mutate))
    refused(paths, registry_env, "hosts (0|2) labels")


def test_pad_with_two_labels_is_refused(tmp_path, registry_env):
    def mutate(ly, top, layers):
        top.shapes(layers["texts"]).insert(db.Text("extra", db.Trans(db.Vector(52 * U, 52 * U))))
    paths = build_world(tmp_path, mutate_die=("U1", mutate))
    refused(paths, registry_env, "4 labels for 3 terminals|hosts 2 labels")


def test_label_text_must_equal_pin(tmp_path, registry_env):
    def mutate(ly, top, layers):
        for s in top.shapes(layers["texts"]).each():
            if s.text_string == "a":
                s.text_string = "A"
    paths = build_world(tmp_path, mutate_die=("U1", mutate))
    refused(paths, registry_env, "differs from pin")


def test_window_touching_pad_edge_is_refused(tmp_path, registry_env):
    def mutate(doc):
        _terminal(doc, "U1.a.pad0")["position_um"] = [50 + 24, 50]   # 22-wide window ends at pad edge 85
        return doc
    paths = build_world(tmp_path, mutate_doc=mutate)
    refused(paths, registry_env, "T -> P|T -> X")


def test_pad_without_terminal_is_refused(tmp_path, registry_env):
    def mutate(ly, top, layers):
        top.shapes(layers["pads"]).insert(db.Box(200 * U, 20 * U, 270 * U, 90 * U))
        top.shapes(layers["texts"]).insert(db.Text("orphan", db.Trans(db.Vector(235 * U, 55 * U))))
    paths = build_world(tmp_path, mutate_die=("U1", mutate))
    refused(paths, registry_env, "every die pad must carry a terminal")


def test_pad_outside_outline_is_refused(tmp_path, registry_env):
    def mutate(ly, top, layers):
        top.shapes(layers["outline"]).clear()
        top.shapes(layers["outline"]).insert(db.Box(0, 0, 300 * U, 150 * U))
    paths = build_world(tmp_path, mutate_die=("U1", mutate))
    refused(paths, registry_env, "not strictly inside the outline")


def test_two_terminals_in_one_pad_is_refused(tmp_path, registry_env):
    def mutate(doc):
        _terminal(doc, "U1.b.pad0")["position_um"] = [60, 50]   # inside pad a as well
        return doc

    def proxy(ly, top, layer):
        region = db.Region(top.shapes(layer))
        top.shapes(layer).clear()
        for p in region.each():
            top.shapes(layer).insert(p.moved(10 * U, 0) if p.bbox().center().x == 150 * U else p)
    paths = build_world(tmp_path, mutate_doc=mutate)
    refused(paths, registry_env, "T -> P|already hosts")


def test_proxy_bbox_must_equal_body_bbox(tmp_path, registry_env):
    def mutate(ly, top, layer):
        region = db.Region(top.shapes(layer))
        top.shapes(layer).clear()
        for p in region.each():
            c = p.bbox().center()
            top.shapes(layer).insert(ngon(c.x, c.y, 46000, 128) if c.x == 50 * U else p)
    paths = build_world(tmp_path, mutate_proxy=("U1", mutate))
    refused(paths, registry_env, "X -> B")


def test_proxy_with_hole_or_split_is_refused(tmp_path, registry_env):
    def mutate(ly, top, layer):
        region = db.Region(top.shapes(layer)) - db.Region(db.Box(49000, 49000, 51000, 51000))
        top.shapes(layer).clear()
        top.shapes(layer).insert(region)
    paths = build_world(tmp_path, mutate_proxy=("U1", mutate))
    refused(paths, registry_env, "proxy has holes")

    def split(ly, top, layer):
        region = db.Region(top.shapes(layer)) - db.Region(db.Box(0, 49000, 100000, 51000))
        top.shapes(layer).clear()
        top.shapes(layer).insert(region)
    paths2 = build_world(tmp_path / "b", mutate_proxy=("U1", split))
    refused(paths2, registry_env, "4 proxies for 3 terminals|not square|T -> X")


def test_terminal_off_any_body_is_refused(tmp_path, registry_env):
    def mutate(doc):
        _terminal(doc, "U1.a.pad0")["position_um"] = [51, 50]
        return doc
    paths = build_world(tmp_path, mutate_doc=mutate)
    refused(paths, registry_env, "no body centre")


def test_die_placed_on_another_dies_bodies_is_refused(tmp_path, registry_env):
    """Two dies sharing a body: the bijection over the carrier fails."""
    world = base_world()
    world["dies"]["U2"] = copy.deepcopy(world["dies"]["U1"])
    world["dies"]["U2"].update({"top_cell": "die_u2", "proxy_cell": "u2_proxy"})
    world["nets"] = [{"name": "n", "connections": [{"component": "U1", "pin": p} for p in "abc"]
                      + [{"component": "U2", "pin": p} for p in "abc"]
                      + [{"component": "J1", "pin": "1"}, {"component": "J2", "pin": "1"}]}]
    paths = build_world(tmp_path, world)
    refused(paths, registry_env, "already hosts terminal|centre set differs|share the centre")


# --------------------------------------------------------------------------- #
# z identity, .chiplet, expect_chiplet                                         #
# --------------------------------------------------------------------------- #
def test_z_identity_on_altered_stack(tmp_path, registry_env):
    def mutate(m):
        m["methods"]["cupillar_opt1"]["connection_stack"]["layers"][0].pop("height")
    paths = build_world(tmp_path, mutate_manifest=mutate)
    refused(paths, registry_env, "missing required keys")

    def mutate2(m):
        m["methods"]["cupillar_opt1"]["connection_stack"]["layers"] = []
    paths2 = build_world(tmp_path / "b", mutate_manifest=mutate2)
    refused(paths2, registry_env, "must not be empty")

    def mutate3(m):
        m["methods"]["cupillar_opt1"]["connection_stack"]["layers"][0]["height"] = -1
    paths3 = build_world(tmp_path / "c", mutate_manifest=mutate3)
    refused(paths3, registry_env, "greater than zero")


def test_expect_chiplet_round_trip(tmp_path, registry_env):
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    accepted = tmp_path / "accepted.chiplet"
    shutil.copy(result.outputs["chiplet"], accepted)
    shutil.rmtree(paths["out"])
    sidecar = json.loads(paths["sidecar"].read_text())
    sidecar["expect_chiplet"] = "../accepted.chiplet"
    paths["sidecar"].write_text(json.dumps(sidecar))
    result2 = entry.run_entry(paths["sidecar"], paths["out"])
    assert result2.manifest["expect_chiplet"]["equal"] is True
    # a changed die width, height or z in the accepted file is a refusal; a changed
    # carrier dimension or die thickness is recorded only
    data = entry.cfio.load(accepted)
    data["components"][1]["dimensions"]["width"] = 320.0
    entry.cfio.dump(data, accepted)
    shutil.rmtree(paths["out"])
    refused(paths, registry_env, "U1.dimensions.width: 320.0 vs 300")
    data["components"][1]["dimensions"]["width"] = 300.0
    data["components"][1]["dimensions"]["height"] = 220.0
    entry.cfio.dump(data, accepted)
    refused(paths, registry_env, "U1.dimensions.height: 220.0 vs 200")
    data["components"][1]["dimensions"]["height"] = 200.0
    data["components"][1]["dimensions"]["thickness"] = 999.0
    data["components"][0]["dimensions"]["width"] = 999.0
    entry.cfio.dump(data, accepted)
    result3 = entry.run_entry(paths["sidecar"], paths["out"])
    assert result3.manifest["expect_chiplet"]["dimensions"]["U1"]["expected"]["thickness"] == 999.0
    assert result3.manifest["expect_chiplet"]["dimensions"]["interposer"]["expected"]["width"] == 999.0
    data["components"][1]["position"]["z"] = 1.0
    entry.cfio.dump(data, accepted)
    shutil.rmtree(paths["out"])
    refused(paths, registry_env, "round trip differs")
    # the carrier top cell is the resolved layout's own name (a renamed variant keeps the
    # same view): recorded, not compared; a die top cell still is
    data["components"][1]["position"]["z"] = 57.83
    data["components"][0]["top_cell"] = "INTERPOSER"
    entry.cfio.dump(data, accepted)
    result4 = entry.run_entry(paths["sidecar"], paths["out"])
    assert result4.manifest["expect_chiplet"]["carrier_top_cell"] == {"expected": "INTERPOSER",
                                                                       "produced": "CARRIER"}
    data["components"][1]["top_cell"] = "other"
    entry.cfio.dump(data, accepted)
    shutil.rmtree(paths["out"])
    refused(paths, registry_env, "U1.top_cell")


EXPECT_CHIPLET_NON_NUMBERS = {
    "position_string": (lambda u1: u1["position"].update(x="abc"), "U1.position.x"),
    "position_null": (lambda u1: u1["position"].update(z=None), "U1.position.z"),
    "position_not_a_mapping": (lambda u1: u1.update(position=[0.0, 0.0, 0.0]), "U1.position.x"),
    "dimension_string": (lambda u1: u1["dimensions"].update(width="abc"), "U1.dimensions.width"),
    "dimension_list": (lambda u1: u1["dimensions"].update(height=[200.0]), "U1.dimensions.height"),
    "dimensions_not_a_mapping": (lambda u1: u1.update(dimensions=[300.0, 200.0]), "U1.dimensions.width"),
}


@pytest.mark.parametrize("name", sorted(EXPECT_CHIPLET_NON_NUMBERS))
def test_expect_chiplet_non_number_is_refused(tmp_path, registry_env, capsys, name):
    """A position or die size the vendored reader leaves unchecked is a typed refusal
    naming its path, exit 2, not a traceback from the decimal conversion."""
    mutate, where = EXPECT_CHIPLET_NON_NUMBERS[name]
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    data = entry.cfio.load(result.outputs["chiplet"])
    mutate(data["components"][1])
    entry.cfio.dump(data, tmp_path / "accepted.chiplet")
    shutil.rmtree(paths["out"])
    sidecar = json.loads(paths["sidecar"].read_text())
    sidecar["expect_chiplet"] = "../accepted.chiplet"
    paths["sidecar"].write_text(json.dumps(sidecar))
    refused(paths, registry_env, f"sidecar.expect_chiplet.{where}: expected a finite number")
    assert entry.main(["--sidecar", str(paths["sidecar"]), "--out", str(paths["out"])]) == 2
    assert f"sidecar.expect_chiplet.{where}: expected a finite number" in capsys.readouterr().err
    assert not paths["out"].exists()


def test_chiplet_consumers_read_the_emitted_file(tmp_path, registry_env):
    """pads_vs_pillars and chiplet2dbx accept the emitted .chiplet and pillar manifest."""
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    sys.path.insert(0, str(ADK_ROOT / "checks"))
    sys.path.insert(0, str(ADK_ROOT / "openroad"))
    import pads_vs_pillars as pvp  # noqa: E402
    import chiplet2dbx  # noqa: E402
    assembly = chiplet2dbx.load_chiplet(result.outputs["chiplet"])
    outdir = tmp_path / "dbx"
    outdir.mkdir()
    chiplet2dbx.export_3dblox(assembly, outdir)
    assert any(outdir.iterdir())
    rc = pvp.main(["--chiplet", str(result.outputs["chiplet"]), "--pillars", str(result.outputs["pillars"]),
                   "--gds-pads", "U1", "--gds-pads", "U2", "--strict"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# derive_attachment_sidecar                                                    #
# --------------------------------------------------------------------------- #
def test_derive_sidecar_round_trips(tmp_path, registry_env):
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    chiplet = accepted / "syn.chiplet"
    pillars = accepted / "syn_assembled.pillars.json"
    text = result.outputs["chiplet"].read_text().replace("layout: ../dies/", "layout: ../dies/")
    chiplet.write_text(text)
    shutil.copy(result.outputs["pillars"], pillars)
    shutil.rmtree(paths["out"])
    derived = tmp_path / "derived.adk_attachment.json"
    rc = derive.main(["--chiplet", str(chiplet), "--pillars", str(pillars),
                      "--document", str(paths["doc"]), "--dies-root", str(paths["dies"]),
                      "--methods-manifest", "manifest/interconnect_methods.json", "--out", str(derived)])
    assert rc == 0
    data = json.loads(derived.read_text())
    validate("resolved_attachment.schema.json", data)
    original = json.loads(paths["sidecar"].read_text())
    assert data["binds"] == original["binds"]
    assert data["components"]["U1"]["dimensions"] == original["components"]["U1"]["dimensions"]
    assert data["interposer"]["attachment_layers"] == original["interposer"]["attachment_layers"]
    assert data["interposer"]["attachment_surface_z"] == 13.83
    assert data["expect_chiplet"] == os.path.relpath(chiplet, derived.parent)
    again = entry.run_entry(derived, paths["out"])
    assert again.manifest["expect_chiplet"]["equal"] is True


def test_a_courtyard_sized_chiplet_is_refused_through_derive(tmp_path, registry_env):
    """A .chiplet sizing a die from its courtyard (outline + 20) derives, and the entry refuses it."""
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    data = entry.cfio.load(result.outputs["chiplet"])
    data["components"][1]["dimensions"].update(width=320.0, height=220.0)
    accepted = tmp_path / "accepted"  # layouts are ../dies/ from the .chiplet, as in the round trip
    accepted.mkdir()
    courtyard = accepted / "syn.chiplet"
    entry.cfio.dump(data, courtyard)
    pillars = accepted / "syn_assembled.pillars.json"
    shutil.copy(result.outputs["pillars"], pillars)
    shutil.rmtree(paths["out"])
    derived = tmp_path / "courtyard.adk_attachment.json"
    rc = derive.main(["--chiplet", str(courtyard), "--pillars", str(pillars),
                      "--document", str(paths["doc"]), "--dies-root", str(paths["dies"]),
                      "--methods-manifest", "manifest/interconnect_methods.json", "--out", str(derived)])
    assert rc == 0
    assert json.loads(derived.read_text())["components"]["U1"]["dimensions"] == {"width": 320.0, "height": 220.0}
    with pytest.raises(entry.Refusal, match="outline 300000x200000 DBU is not the declared 320000x220000 DBU"):
        entry.run_entry(derived, paths["out"])
    assert not paths["out"].exists()


def test_derive_refuses_a_z_that_disagrees(tmp_path, registry_env):
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    data = entry.cfio.load(result.outputs["chiplet"])
    data["components"][1]["position"]["z"] = 60.0
    altered = tmp_path / "altered.chiplet"
    entry.cfio.dump(data, altered)
    rc = derive.main(["--chiplet", str(altered), "--pillars", str(result.outputs["pillars"]),
                      "--document", str(paths["doc"]), "--dies-root", str(paths["dies"]),
                      "--methods-manifest", "manifest/interconnect_methods.json",
                      "--out", str(tmp_path / "d.json")])
    assert rc == 2 and not (tmp_path / "d.json").exists()


def test_derive_validates_before_publication(tmp_path, registry_env, monkeypatch):
    """LVS finding: the sidecar used to be written before its own re-load; now a
    validation refusal leaves neither the destination nor a candidate behind, and an
    existing destination is refused and left untouched."""
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    args = ["--chiplet", str(result.outputs["chiplet"]), "--pillars", str(result.outputs["pillars"]),
            "--document", str(paths["doc"]), "--dies-root", str(paths["dies"]),
            "--methods-manifest", "manifest/interconnect_methods.json"]
    # late validation refusal: nothing published, no candidate left
    original = entry.load_sidecar

    def refusing(path):
        original(path)
        raise entry.Refusal("sidecar", "simulated late validation refusal")
    monkeypatch.setattr(entry, "load_sidecar", refusing)
    monkeypatch.setattr(derive.entry, "load_sidecar", refusing)
    rc = derive.main(args + ["--out", str(dest_dir / "d.json")])
    assert rc == 2 and sorted(dest_dir.iterdir()) == []
    monkeypatch.setattr(entry, "load_sidecar", original)
    monkeypatch.setattr(derive.entry, "load_sidecar", original)
    # existing destination: refused, content preserved byte for byte
    existing = dest_dir / "existing.json"
    existing.write_text("unrelated sentinel\n")
    rc = derive.main(args + ["--out", str(existing)])
    assert rc == 2 and existing.read_text() == "unrelated sentinel\n"
    assert sorted(p.name for p in dest_dir.iterdir()) == ["existing.json"]
    # corrected retry at the path of the late refusal: published atomically, no candidate left
    rc = derive.main(args + ["--out", str(dest_dir / "d.json")])
    assert rc == 0 and sorted(p.name for p in dest_dir.iterdir()) == ["d.json", "existing.json"]


def test_derive_requires_the_pillar_method_inventory(tmp_path, registry_env):
    """A pillar manifest that lacks a placed die's method, or names one absent from
    the methods manifest, is refused; the derivation inputs are recorded by sha256."""
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    pillars = json.loads(result.outputs["pillars"].read_text())
    args = ["--chiplet", str(result.outputs["chiplet"]), "--document", str(paths["doc"]),
            "--dies-root", str(paths["dies"]), "--methods-manifest", "manifest/interconnect_methods.json"]
    lacking = tmp_path / "lacking.json"
    lacking.write_text(json.dumps({**pillars, "methods": {k: v for k, v in pillars["methods"].items() if k != "cupillar_opt1"}}))
    assert derive.main(args + ["--pillars", str(lacking), "--out", str(tmp_path / "a.json")]) == 2
    assert not (tmp_path / "a.json").exists()
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({**pillars, "methods": {**pillars["methods"], "cupillar_opt9": pillars["methods"]["cupillar_opt1"]}}))
    assert derive.main(args + ["--pillars", str(unknown), "--out", str(tmp_path / "b.json")]) == 2
    schema_only = tmp_path / "schema_only.json"
    schema_only.write_text(json.dumps({"schema": "adk-pillar-manifest"}))
    assert derive.main(args + ["--pillars", str(schema_only), "--out", str(tmp_path / "c.json")]) == 2


def test_derived_from_identity_is_bound_to_the_round_trip_target(tmp_path, registry_env):
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    chiplet = accepted / "syn.chiplet"
    pillars = accepted / "syn_assembled.pillars.json"
    shutil.copy(result.outputs["chiplet"], chiplet)
    shutil.copy(result.outputs["pillars"], pillars)
    shutil.rmtree(paths["out"])
    derived = tmp_path / "derived.adk_attachment.json"
    assert derive.main(["--chiplet", str(chiplet), "--pillars", str(pillars), "--document", str(paths["doc"]),
                        "--dies-root", str(paths["dies"]), "--methods-manifest", "manifest/interconnect_methods.json",
                        "--out", str(derived)]) == 0
    data = json.loads(derived.read_text())
    validate("resolved_attachment.schema.json", data)
    assert data["derived_from"] == {"chiplet": {"path": "accepted/syn.chiplet", "sha256": sha(chiplet)},
                                    "pillars": {"path": "accepted/syn_assembled.pillars.json", "sha256": sha(pillars)}}
    again = entry.run_entry(derived, paths["out"])
    assert again.manifest["derived_from"] == data["derived_from"]
    shutil.rmtree(paths["out"])
    # a recorded identity the file does not have: authenticated first, refused first
    data["derived_from"]["chiplet"]["sha256"] = "0" * 64
    derived.write_text(json.dumps(data))
    with pytest.raises(entry.Refusal, match="sidecar.derived_from.chiplet: sidecar binds 0000"):
        entry.run_entry(derived, paths["out"])
    assert not paths["out"].exists()
    # a genuine other file, correctly identified, that is not the round-trip target
    other = accepted / "other.chiplet"
    other.write_text(chiplet.read_text() + "\n")
    data["derived_from"]["chiplet"] = {"path": "accepted/other.chiplet", "sha256": sha(other)}
    derived.write_text(json.dumps(data))
    with pytest.raises(entry.Refusal, match="derivation input is not the round-trip target"):
        entry.run_entry(derived, paths["out"])
    assert not paths["out"].exists()


def test_strictly_inside_is_interior_containment():
    """(b) strict containment is interior containment, exact on integer coordinates, with
    no clearance implied: a slanted boundary at a positive sub-DBU distance is inside."""
    diamond = db.Region(db.Polygon([db.Point(0, 3), db.Point(3, 0), db.Point(0, -3), db.Point(-3, 0)]))
    assert entry.strictly_inside(db.Region(db.Box(-1, -1, 1, 1)), diamond)         # clearance 0.707 DBU
    assert not entry.strictly_inside(db.Region(db.Box(-1, -2, 1, 2)), diamond)     # touches at (0, +-2)? no: crosses
    assert not entry.strictly_inside(db.Region(db.Box(-3, -3, 3, 3)), diamond)     # crosses
    square = db.Region(db.Box(0, 0, 10, 10))
    assert entry.strictly_inside(db.Region(db.Box(1, 1, 9, 9)), square)
    assert not entry.strictly_inside(db.Region(db.Box(0, 1, 9, 9)), square)        # shared edge
    assert not entry.strictly_inside(db.Region(db.Box(1, 1, 10, 9)), square)       # shared edge
    assert entry.inside(db.Region(db.Box(0, 1, 9, 9)), square)


def test_grid_spelling_is_numeric_not_textual(tmp_path, registry_env):
    """0.0010 and 1E-3 are the same power of ten as 0.001; 0.002 is not."""
    for spelling in ("0.0010", "1E-3"):
        paths = build_world(tmp_path / spelling.replace("-", "m"))
        text = paths["doc"].read_text()
        assert '"grid_um": 0.001' in text
        paths["doc"].write_text(text.replace('"grid_um": 0.001', f'"grid_um": {spelling}', 1))
        sidecar = json.loads(paths["sidecar"].read_text())
        sidecar["binds"]["document"] = sha(paths["doc"])
        paths["sidecar"].write_text(json.dumps(sidecar))
        assert run(paths, registry_env).manifest["resolved"]["grid_um"] == "0.001"
    paths = build_world(tmp_path / "two", mutate_doc=lambda d: (d["technology"].update(grid_um=0.002), d)[1])
    refused(paths, registry_env, "not a power of ten")


def test_vertex_count_invariant_holds_across_dies_of_one_method(tmp_path, registry_env):
    """(c) the method-wide vertex-count invariant is enforced across dies, not per die:
    two dies on the same method, every U2 opening drawn as a 128-gon (U2 internally
    consistent, U1 different), so only the method-global guard can refuse."""
    world = base_world()
    world["dies"]["U2"]["method"] = "cupillar_opt1"
    u2 = list(body_centres(world)["U2"].values())

    def mutate(ly, top, L):
        _replace_shapes(top, L(9, 35), u2, lambda cx, cy: ngon(cx, cy, 35000, 128))
    paths = build_world(tmp_path / "global", world, mutate_carrier=mutate)
    refused(paths, registry_env, "across all dies have")
    # the predecessor's die-local guard is still there, for a single body within one die
    gx, gy = u2[0]

    def mutate_one(ly, top, L):
        _replace_shapes(top, L(9, 35), [(gx, gy)], lambda cx, cy: ngon(cx, cy, 35000, 128))
    paths = build_world(tmp_path / "local", world, mutate_carrier=mutate_one)
    refused(paths, registry_env, "differ from other bodies")


def test_point_strictly_inside_is_interior_semantics():
    """Successor delta 5: the label predicate is point-interior, the same semantics as the
    polygon predicate; the integer centre of a unit diamond is interior, its vertices and
    edge points are not, and no clearance is implied."""
    diamond = db.Polygon([db.Point(0, 1), db.Point(1, 0), db.Point(0, -1), db.Point(-1, 0)])
    assert entry.point_strictly_inside((0, 0), diamond)
    for p in ((0, 1), (1, 0), (0, -1), (-1, 0), (2, 0)):
        assert not entry.point_strictly_inside(p, diamond)
    square = db.Polygon(db.Box(0, 0, 10, 10))
    assert entry.point_strictly_inside((1, 1), square)
    assert not entry.point_strictly_inside((0, 5), square) and not entry.point_strictly_inside((5, 10), square)


def test_output_id_lengths_38_39_64_produce_every_output(tmp_path, registry_env):
    """Successor delta 2: the id rule (64 characters) and the output-name rule (255) are
    separate; ids of 38, 39 and 64 characters produce every output, 65 is refused."""
    for n in (38, 39, 64):
        aid = "i" * n
        paths = build_world(tmp_path / str(n), mutate_doc=lambda d, a=aid: (d["assembly"].update(id=a), d)[1])
        result = run(paths, registry_env)
        for name in (f"{aid}_assembled.gds", f"{aid}_assembled.boundaries.json", f"{aid}_assembled.pillars.json",
                     f"{aid}_assembled.ixn_methods.json", f"{aid}.chiplet", "entry_manifest.json"):
            assert (paths["out"] / name).is_file(), name
        assert result.manifest["resolved"]["assembly_id"] == aid
    paths = build_world(tmp_path / "65", mutate_doc=lambda d: (d["assembly"].update(id="i" * 65), d)[1])
    refused(paths, registry_env, "not usable as an output file stem")


def test_derived_pillars_and_expected_chiplet_are_authenticated(tmp_path, registry_env, monkeypatch):
    """Successor delta 3: the recorded derivation inputs are opened and authenticated, the
    expected chiplet is bound at load and re-checked when parsed, and all of them are in the
    closure; pillar, expected-chiplet and config mutation controls."""
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    chiplet = accepted / "syn.chiplet"
    pillars = accepted / "syn_assembled.pillars.json"
    shutil.copy(result.outputs["chiplet"], chiplet)
    shutil.copy(result.outputs["pillars"], pillars)
    shutil.rmtree(paths["out"])
    derived = tmp_path / "derived.adk_attachment.json"
    assert derive.main(["--chiplet", str(chiplet), "--pillars", str(pillars), "--document", str(paths["doc"]),
                        "--dies-root", str(paths["dies"]), "--methods-manifest", "manifest/interconnect_methods.json",
                        "--out", str(derived)]) == 0
    good = entry.run_entry(derived, paths["out"])
    closure = good.manifest["inputs_rechecked_after_outputs"]
    assert {"sidecar", "expect_chiplet", "derived_from.chiplet", "derived_from.pillars", "config.chiplet_pads"} <= set(closure)
    assert closure["derived_from.pillars"] == sha(pillars) and closure["expect_chiplet"] == sha(chiplet)
    shutil.rmtree(paths["out"])
    # pillar bytes changed after derivation: the recorded identity no longer matches
    pillars.write_text(pillars.read_text() + "\n")
    with pytest.raises(entry.Refusal, match="sidecar.derived_from.pillars"):
        entry.run_entry(derived, paths["out"])
    assert not paths["out"].exists()
    pillars.write_text(pillars.read_text()[:-1])
    # pillar file removed: a recorded identity without bytes is a refusal
    pillars.rename(accepted / "gone.json")
    with pytest.raises(entry.Refusal, match="sidecar.derived_from.pillars"):
        entry.run_entry(derived, paths["out"])
    (accepted / "gone.json").rename(pillars)
    # expected chiplet appended after the sidecar was loaded (mid-run): the parse re-checks
    # the bound bytes and refuses; no output remains
    original = entry.prove_preservation

    def mutate_expect(*args, **kwargs):
        with open(chiplet, "a", encoding="utf-8") as handle:
            handle.write("\n")
        return original(*args, **kwargs)
    monkeypatch.setattr(entry, "prove_preservation", mutate_expect)
    with pytest.raises(entry.Refusal, match="sidecar.expect_chiplet|changed during the run"):
        entry.run_entry(derived, paths["out"])
    assert not paths["out"].exists()
    monkeypatch.setattr(entry, "prove_preservation", original)
    chiplet.write_text(chiplet.read_text()[:-1])
    # config mutated after it was read and bound (the round-trip comparison runs after the
    # single config read): the closure refuses; the config is restored afterwards
    pads_path = entry.ADK_ROOT / "config" / "chiplet_pads.json"
    pads_bytes = pads_path.read_bytes()
    original_compare = entry.compare_expect_chiplet

    def mutate_config(*args, **kwargs):
        pads_path.write_bytes(pads_bytes + b"\n")
        return original_compare(*args, **kwargs)
    monkeypatch.setattr(entry, "compare_expect_chiplet", mutate_config)
    try:
        with pytest.raises(entry.Refusal, match="config.chiplet_pads.*changed during the run"):
            entry.run_entry(derived, paths["out"])
    finally:
        pads_path.write_bytes(pads_bytes)
    assert not paths["out"].exists()


def test_layout_changed_while_being_read_is_refused(tmp_path, registry_env, monkeypatch):
    """Successor delta 3: KLayout reads from a path, so the bytes read are bound by hashing
    immediately before and after the read; a change in between is a refusal."""
    paths = build_world(tmp_path)
    original_read = db.Layout.read
    target = {"path": None}

    def reading(self, path, *args, **kwargs):
        rc = original_read(self, path, *args, **kwargs)
        if target["path"] is None:
            target["path"] = Path(path)
            with open(path, "ab") as handle:
                handle.write(b"\0")
        return rc
    monkeypatch.setattr(db.Layout, "read", reading)
    refused(paths, registry_env, "changed while being read")
    assert target["path"] is not None


def test_derive_never_clobbers_a_destination_created_during_validation(tmp_path, registry_env, monkeypatch):
    """Successor delta 4: a destination that appears while the candidate is being validated
    is preserved byte for byte; the publication refuses, no candidate remains, and a retry
    on a fresh path succeeds."""
    paths = build_world(tmp_path)
    result = run(paths, registry_env)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    args = ["--chiplet", str(result.outputs["chiplet"]), "--pillars", str(result.outputs["pillars"]),
            "--document", str(paths["doc"]), "--dies-root", str(paths["dies"]),
            "--methods-manifest", "manifest/interconnect_methods.json"]
    dest = dest_dir / "d.json"
    original = entry.load_sidecar

    def racing(path):
        original(path)
        dest.write_text("sentinel written during validation\n")
    monkeypatch.setattr(entry, "load_sidecar", racing)
    monkeypatch.setattr(derive.entry, "load_sidecar", racing)
    rc = derive.main(args + ["--out", str(dest)])
    assert rc == 2 and dest.read_text() == "sentinel written during validation\n"
    assert sorted(p.name for p in dest_dir.iterdir()) == ["d.json"]
    monkeypatch.setattr(entry, "load_sidecar", original)
    monkeypatch.setattr(derive.entry, "load_sidecar", original)
    assert derive.main(args + ["--out", str(dest_dir / "retry.json")]) == 0
    assert sorted(p.name for p in dest_dir.iterdir()) == ["d.json", "retry.json"]


def test_input_mutated_during_the_run_is_refused(tmp_path, registry_env, monkeypatch):
    """LVS finding: binds were checked only before use. Every consumed input is hashed
    again after the outputs exist (closure); a document mutated mid-run is a refusal
    and no output is admitted."""
    paths = build_world(tmp_path)
    original = entry.prove_preservation

    def mutating(*args, **kwargs):
        with open(paths["doc"], "a", encoding="utf-8") as handle:
            handle.write("\n")
        return original(*args, **kwargs)
    monkeypatch.setattr(entry, "prove_preservation", mutating)
    refused(paths, registry_env, "changed during the run")


def test_closure_records_every_consumed_input(tmp_path, registry_env):
    result = run(build_world(tmp_path), registry_env)
    closure = result.manifest["inputs_rechecked_after_outputs"]
    assert {"document", "adapter_deck", "methods_manifest", "config.chiplet_pads",
            "die_layout.U1", "die_layout.U2", "layout.U1", "layout.U2", "layout.interposer"} <= set(closure)
    assert closure["config.chiplet_pads"] == sha(entry.ADK_ROOT / "config" / "chiplet_pads.json")


def test_holed_outline_is_refused(tmp_path, registry_env):
    """LVS finding: the boundary export writes the hull only, so a holed 206/0 outline
    is refused by name instead of being silently filled."""
    def mutate(ly, top, L):
        region = db.Region(top.shapes(L["outline"])) - db.Region(db.Box(500, 500, 1500, 1500))
        assert region.count() == 1 and list(region.each())[0].holes() == 1
        top.shapes(L["outline"]).clear()
        top.shapes(L["outline"]).insert(region)
    paths = build_world(tmp_path, mutate_die=("U1", mutate))
    refused(paths, registry_env, "carries 1 holes")


def test_yaml_leading_zero_scalar_is_refused(tmp_path, registry_env):
    """LVS finding: PyYAML reads 012 as octal 10 while a decimal reader gives 12; the
    entry refuses the ambiguous scalar (profile policy) instead of choosing."""
    for text_value in ("0700", "012.5"):
        paths = build_world(tmp_path / text_value)
        doc = json.loads(paths["doc"].read_text())
        text = yaml.safe_dump(doc, sort_keys=False).replace("- 900\n", f"- {text_value}\n", 1)
        assert f"- {text_value}\n" in text
        ypath = paths["case"] / "resolved.yaml"
        ypath.write_text(text)
        sidecar = json.loads(paths["sidecar"].read_text())
        sidecar["document"] = "resolved.yaml"
        sidecar["binds"]["document"] = sha(ypath)
        paths["sidecar"].write_text(json.dumps(sidecar))
        refused(paths, registry_env, "leading zero")


# --------------------------------------------------------------------------- #
# Gated: corpus run and frozen-parser differential                             #
# --------------------------------------------------------------------------- #
def _gate(var):
    value = os.environ.get(var)
    if not value:
        if os.environ.get("ADK_RESOLVED_V2_ACCEPTANCE") == "1":
            pytest.fail(f"acceptance mode: {var} is not set; an unreachable corpus is a failure")
        pytest.skip(f"{var} not set (gated control; a skip is not acceptance evidence)")
    path = Path(value)
    if not path.exists():
        if os.environ.get("ADK_RESOLVED_V2_ACCEPTANCE") == "1":
            pytest.fail(f"acceptance mode: {var}={path} does not exist")
        pytest.skip(f"{var}={path} does not exist")
    return path


# Documents the frozen parser accepts and the entry refuses: exactly the documented profile
# restrictions and entry-side policies, nothing else (the differential asserts the set of
# such documents is a subset of this list; the runner's verdict gate reads it through the
# identity probe). Not blanket parity: every name here is a documented difference.
PROFILE_ONLY_DIFFERENCES = frozenset({
    "interposer_rotated", "interposer_mirrored", "uncertainty_nonzero",
    "certificate_present", "two_terminals_one_port", "technology_layer_mapped_twice",
    "die_maps_two_contact_layers", "contact_roles_reversed_only", "duplicate_io_pad_tuple",
    # entry-side structural and output-safety refusals (recorded, not v2 semantics)
    "contact_second_carrier_conductor", "contact_second_carrier_conductor_reversed",
    "contact_pairing_repeated_reversed", "contacts_two_carrier_conductors",
    "assembly_id_traversal", "assembly_id_absolute", "assembly_id_nested", "assembly_id_dot",
    # YAML 1.1 octal versus decimal: the frozen parser chooses, the entry refuses
    "yaml_leading_zero_int",
    # explicit supported-subset difference: numeric tokens only, a numeric string is not
    # a number for the entry (the frozen _decimal accepts it)
    "string_as_number",
    # entry numeric-domain restriction, both modes: coordinates and sizes must lie in the
    # exact-safe range of +/-SAFE_GRID_COORD grid units; the frozen parser aligns Decimals
    # to the grid without a magnitude limit (kept on purpose, see docs/resolved_v2_entry.md)
    "outside_safe_range",
})

CORPUS_VARIANTS = {"reference": ("e3a34b3f", "4004c8c8", 0), "equivalent": ("0518d48c", "0049640b", 0),
                   "narrow_trace": ("e3a34b3f", "ec17989d", 900), "route_cut": ("e3a34b3f", "680b02fe", 40)}


@pytest.mark.parametrize("variant", sorted(CORPUS_VARIANTS))
def test_corpus_variant(tmp_path, monkeypatch, variant):
    corpus = _gate("ADK_RESOLVED_V2_CORPUS_ROOT")
    design = corpus / "design"
    monkeypatch.delenv("ADK_REGISTRY_DIR", raising=False)
    doc_prefix, carrier_prefix, removed_um2 = CORPUS_VARIANTS[variant]
    doc = corpus / variant / "resolved.json"
    assert sha(doc).startswith(doc_prefix)
    carrier = entry.decode_resolved_v2(doc).interposer
    assert sha(carrier.layout_resolved).startswith(carrier_prefix)
    sidecar = tmp_path / "design.adk_attachment.json"
    rc = derive.main(["--chiplet", str(design / "outputs" / "design.chiplet"),
                      "--pillars", str(design / "outputs" / "design.pillars.json"),
                      "--document", str(doc), "--dies-root", str(design / "chiplets"),
                      "--methods-manifest", "manifest/interconnect_methods.json", "--out", str(sidecar)])
    assert rc == 0
    result = entry.run_entry(sidecar, tmp_path / "out")
    m = result.manifest
    assert m["resolved"]["counts"] == {"dies": 2, "terminals": 84, "terminals_per_die": {"U1": 48, "U2": 36},
                                       "io_pads": 25, "logical_nets": 35, "endpoints": 109, "bodies": 84}
    assert m["expect_chiplet"]["equal"] is True
    assert m["outline_vs_declared"] == {"U1": {"outline_dbu": [2000000, 2000000], "declared_dbu": [2000000, 2000000],
                                               "difference_dbu": [0, 0]},
                                        "U2": {"outline_dbu": [1600000, 1600000], "declared_dbu": [1600000, 1600000],
                                               "difference_dbu": [0, 0]}}
    row = next(r for r in m["correspondence"]["U1"] if r["terminal"] == "U1.IOVDD_0.pad0")
    assert row["body_centre_c_dbu"] == [3800000, 670000] and row["body_centre_g_dbu"] == [103800000, -104330000]
    frame = next(f for f in m["frames"]["dies"] if f["die"] == "U1")
    assert frame["g_origin"] == [104800000, -104400000] and frame["legacy_rotation_deg"] == 0
    reference = entry.decode_resolved_v2(corpus / "reference" / "resolved.json").interposer
    assert sha(reference.layout_resolved).startswith(CORPUS_VARIANTS["reference"][1])
    ref = entry.open_layout(reference.layout_resolved, GRID, reference.top_cell, "ref")
    src = entry.open_layout(carrier.layout_resolved, GRID, carrier.top_cell, "src")
    xor = entry.region_of(ref, (134, 0)) ^ entry.region_of(src, (134, 0))
    assert xor.area() == removed_um2 * U * U
    (tmp_path / "tm2_xor_um2.txt").write_text(f"{variant} {xor.area() / (U * U)}\n")


def test_frozen_parser_differential(tmp_path):
    """Differential against the frozen assembly-lvs parser (gated)."""
    root = _gate("ADK_RESOLVED_V2_FROZEN_PARSER_ROOT")
    expected_sha = os.environ.get("ADK_RESOLVED_V2_FROZEN_PARSER_SHA256")
    parser_file = root / "src" / "assembly_lvs" / "parser.py"
    if expected_sha and sha(parser_file) != expected_sha:
        pytest.fail(f"frozen parser {parser_file} is {sha(parser_file)[:12]}, expected {expected_sha[:12]}")
    sys.path.insert(0, str(root / "src"))
    from assembly_lvs import parser as frozen  # noqa: E402
    from assembly_lvs.model import ConfigError  # noqa: E402

    profile_only = PROFILE_ONLY_DIFFERENCES
    verdicts = {}
    semantics = {}
    cases = dict(DOC_REFUSALS)
    cases["accepted"] = (lambda d: None, None)
    cases["contact_both_reversed"] = (lambda d: d["contracts"]["interface_contacts"][0].update(
        layers=["TM2", "DIE_CONTACT"], roles=["interposer", "die"]), None)
    for name, (mutate, _) in sorted(cases.items()):
        paths = build_world(tmp_path / name, mutate_doc=lambda d, m=mutate: (m(d), d)[1])
        try:
            frozen.load_resolved(paths["doc"])
            parser_ok = True
        except ConfigError:
            parser_ok = False
        try:
            entry.decode_resolved_v2(paths["doc"], enforce_profile=False)
            nominal_ok = True
        except entry.Refusal:
            nominal_ok = False
        try:
            entry.decode_resolved_v2(paths["doc"], enforce_profile=True)
            profile_ok = True
        except entry.Refusal:
            profile_ok = False
        verdicts[name] = (parser_ok, nominal_ok, profile_ok)
        if parser_ok and nominal_ok:
            # semantic comparison, not only accept/refuse: the normalized values both
            # readers hand on must agree (LVS finding on the acceptance-only differential)
            ours = _normalized_entry(entry.decode_resolved_v2(paths["doc"], enforce_profile=False))
            theirs = _normalized_frozen(frozen.load_resolved(paths["doc"]).document)
            semantics[name] = {"equal": ours == theirs, "entry": ours, "frozen": theirs}
    # YAML leading zero: the frozen parser reads 0700 as octal 448 and accepts, the entry
    # refuses; both verdicts and the frozen reading are recorded as evidence
    zpaths = build_world(tmp_path / "yaml_leading_zero_int")
    zdoc = json.loads(zpaths["doc"].read_text())
    plain = yaml.safe_dump(zdoc, sort_keys=False)
    ztext = plain.replace("- 900\n", "- 0700\n", 1)
    assert "- 0700\n" in ztext
    zpath = zpaths["case"] / "resolved.yaml"
    zpath.write_text(ztext)
    decimal_reading = _normalized_entry(entry.decode_resolved_v2(zpaths["doc"], enforce_profile=False))
    try:
        parsed = frozen.load_resolved(zpath)
        parser_ok = True
        octal_reading = _normalized_frozen(parsed.document)
        semantics["yaml_leading_zero_int"] = {"frozen": octal_reading,
                                              "frozen_equals_decimal_reading": octal_reading == decimal_reading}
    except ConfigError:
        parser_ok = False
    try:
        entry.decode_resolved_v2(zpath, enforce_profile=False)
        nominal_ok = True
    except entry.Refusal as exc:
        nominal_ok = False
        semantics.setdefault("yaml_leading_zero_int", {})["entry_refusal"] = str(exc)
    verdicts["yaml_leading_zero_int"] = (parser_ok, nominal_ok, nominal_ok)
    (tmp_path / "verdicts.json").write_text(json.dumps({"verdicts": verdicts, "semantics": semantics}, indent=1, default=str))
    accepted_here_refused_there = [n for n, (p, _, e) in verdicts.items() if e and not p]
    assert not accepted_here_refused_there, accepted_here_refused_there
    refused_there_accepted_nominal = [n for n, (p, nom, _) in verdicts.items() if not p and nom]
    assert not refused_there_accepted_nominal, refused_there_accepted_nominal
    extra = {n for n, (p, _, e) in verdicts.items() if p and not e}
    assert extra <= profile_only, extra - profile_only
    disagreeing = [n for n, s in semantics.items() if "equal" in s and not s["equal"]]
    assert not disagreeing, disagreeing
    assert "equal" in semantics["accepted"] and "equal" in semantics["contact_both_reversed"]


def _canon(value):
    """Canonical numeric text: equal Decimals render identically whatever their spelling
    (600, 600.000, 6E2 all give "600"); the differential compares values, not lexemes.
    Zero is one value: -0.0, 0E-3 and 0 all render "0" (Decimal.normalize keeps the sign
    of a negative zero, the entry's integer DBU zero has none)."""
    number = Decimal(value)
    if number.is_zero():
        return "0"
    return format(number.normalize(), "f")


# The projection both readers are compared on. Its denominator is explicit and limited:
# assembly identity and units, grid, conductors, every component's role, top cell,
# placement and layer map, every carrier io_pad, every terminal, every interface contact
# and every logical net with its connections. It does not cover vias, uncertainty or
# provenance, which the verdict comparison and the entry's own decoder tests carry.
PROJECTION_FIELDS = ("assembly_id", "units", "grid_um", "conductors", "components", "io_pads",
                     "terminals", "contacts", "logical_nets")


def _normalized_entry(doc):
    grid = doc.technology.grid_um
    um = lambda p: [_canon(Decimal(p[0]) * grid), _canon(Decimal(p[1]) * grid)]  # noqa: E731
    return {
        "assembly_id": doc.assembly_id, "units": doc.units, "grid_um": _canon(grid),
        "conductors": sorted(doc.technology.conductors),
        "components": {c.id: {"role": c.role, "top_cell": c.top_cell,
                              "position_um": um(c.placement.position), "anchor_um": um(c.placement.anchor),
                              "rotation_deg": c.placement.rotation_deg, "mirror_x": c.placement.mirror_x,
                              "layers": sorted((list(m.source), m.technology_layer) for m in c.layers)}
                       for c in doc.components},
        "io_pads": {c.id: sorted([p.id, p.pin, um(p.position), um(p.size), p.technology_layer, p.net]
                                 for p in c.io_pads) for c in doc.components},
        "terminals": {t.id: {"component": t.component, "pin": t.pin, "layer": t.technology_layer,
                             "position_um": um(t.position), "size_um": um(t.size)} for t in doc.terminals},
        "contacts": {c.id: sorted(list(p) for p in c.pairs) for c in doc.contacts},
        "logical_nets": {n.name: {"connections": sorted([e.component, e.pin, e.layer] for e in n.connections),
                                  "net_class": n.net_class, "external": n.external} for n in doc.nets},
    }


def _normalized_frozen(doc):
    um = lambda p: [_canon(p.x), _canon(p.y)]  # noqa: E731
    return {
        "assembly_id": doc.assembly.id, "units": doc.assembly.units, "grid_um": _canon(doc.technology.grid_um),
        "conductors": sorted(doc.technology.conductors),
        "components": {c.id: {"role": c.role, "top_cell": c.layout.top_cell,
                              "position_um": um(c.placement.position_um), "anchor_um": um(c.placement.anchor_um),
                              "rotation_deg": c.placement.rotation_deg, "mirror_x": c.placement.mirror_x,
                              "layers": sorted(([m.source.layer, m.source.datatype], m.technology_layer) for m in c.layers)}
                       for c in doc.assembly.components},
        "io_pads": {c.id: sorted([p.id, p.pin, um(p.position_um), um(p.size_um), p.technology_layer, p.declared_net]
                                 for p in c.io_pads) for c in doc.assembly.components},
        "terminals": {t.id: {"component": t.component, "pin": t.pin, "layer": t.technology_layer,
                             "position_um": um(t.position_um), "size_um": um(t.size_um)}
                      for t in doc.contracts.terminals},
        "contacts": {c.id: sorted([r, l] for r, l in zip(c.roles, c.layers)) for c in doc.contracts.interface_contacts},
        "logical_nets": {n.name: {"connections": sorted([e.component, e.pin, e.declared_layer] for e in n.connections),
                                  "net_class": n.net_class, "external": n.external} for n in doc.contracts.logical_nets},
    }


def test_normalized_projections_agree_by_value_not_spelling(tmp_path, registry_env):
    """I3 (successor delta 1): the two projections must agree on equal values whatever the
    Decimal spelling. A stub with the frozen model's attribute names carries the entry's
    values re-spelled (600 as 6E2, 0.001 as 0.0010, sizes with trailing zeros)."""
    from types import SimpleNamespace as NS
    paths = build_world(tmp_path)
    doc = entry.decode_resolved_v2(paths["doc"], enforce_profile=False)
    grid = doc.technology.grid_um
    def respell(dbu, i):
        value = Decimal(dbu) * grid
        if value.is_zero():   # the frozen model may carry a signed or exponent zero
            return Decimal("-0.0") if i == 0 else Decimal("0E-3")
        return value * (Decimal("1.000") if i % 2 else Decimal("1E0"))
    pt = lambda p: NS(x=respell(p[0], 0), y=respell(p[1], 1))  # noqa: E731
    stub = NS(
        assembly=NS(id=doc.assembly_id, units=doc.units, components=[
            NS(id=c.id, role=c.role, layout=NS(top_cell=c.top_cell),
               placement=NS(position_um=pt(c.placement.position), anchor_um=pt(c.placement.anchor),
                            rotation_deg=c.placement.rotation_deg, mirror_x=c.placement.mirror_x),
               layers=[NS(source=NS(layer=m.source[0], datatype=m.source[1]), technology_layer=m.technology_layer)
                       for m in c.layers],
               io_pads=[NS(id=p.id, pin=p.pin, position_um=pt(p.position), size_um=pt(p.size),
                           technology_layer=p.technology_layer, declared_net=p.net) for p in c.io_pads])
            for c in doc.components]),
        technology=NS(grid_um=Decimal("0.0010") if grid == Decimal("0.001") else grid, conductors=list(doc.technology.conductors)),
        contracts=NS(
            terminals=[NS(id=t.id, component=t.component, pin=t.pin, technology_layer=t.technology_layer,
                          position_um=pt(t.position), size_um=pt(t.size)) for t in doc.terminals],
            interface_contacts=[NS(id=c.id, roles=[r for r, _ in c.pairs], layers=[l for _, l in c.pairs]) for c in doc.contacts],
            logical_nets=[NS(name=n.name, net_class=n.net_class, external=n.external,
                             connections=[NS(component=e.component, pin=e.pin, declared_layer=e.layer) for e in n.connections])
                          for n in doc.nets]))
    ours, theirs = _normalized_entry(doc), _normalized_frozen(stub)
    assert set(ours) == set(PROJECTION_FIELDS) == set(theirs)
    assert ours == theirs
    assert ours["components"]["U1"]["position_um"] == ["600", "400"]
    assert _canon("6E2") == _canon("600.000") == "600" and _canon("0.0010") == "0.001"
    # the same-value zero control is not vacuous: the stub carried at least one -0.0 and
    # one 0E-3 where the entry has an integer DBU zero, and both sides rendered "0"
    carried = {v.as_tuple() for c in stub.assembly.components
               for p in (c.placement.anchor_um, c.placement.position_um) for v in (p.x, p.y)}
    assert {Decimal("-0.0").as_tuple(), Decimal("0E-3").as_tuple()} <= carried, carried
    assert any("0" in c["anchor_um"] + c["position_um"] for c in ours["components"].values())


@pytest.mark.parametrize("sign", [1, -1])
def test_exact_safe_bound_is_the_boundary_in_both_modes(tmp_path, registry_env, sign):
    """Named entry numeric-domain restriction (outside_safe_range): +/-SAFE_GRID_COORD grid
    units is accepted by the nominal decoder and by the profile, one DBU outside is refused
    by both, values written as Decimal um from the grid; the frozen parser has no such
    bound, so the differential lists the name as a documented entry-side difference."""
    bound = entry.SAFE_GRID_COORD
    assert bound == (1 << 30) - 1
    grid = Decimal("0.001")

    def at(units):
        um = Decimal(units) * grid          # e.g. 1073741.823 um, written as a numeric token
        assert Decimal(repr(float(um))) == um   # the serialized text is the exact value
        def mutate(d):
            _terminal(d, "U1.a.pad0").update(position_um=[float(um), 50])
            return d
        return mutate
    inside = build_world(tmp_path / "inside", mutate_doc=at(sign * bound))
    for enforce in (False, True):
        doc = entry.decode_resolved_v2(inside["doc"], enforce_profile=enforce)
        assert next(t for t in doc.terminals if t.id == "U1.a.pad0").position[0] == sign * bound
    outside = build_world(tmp_path / "outside", mutate_doc=at(sign * (bound + 1)))
    for enforce in (False, True):
        with pytest.raises(entry.Refusal, match="outside the exact-safe range"):
            entry.decode_resolved_v2(outside["doc"], enforce_profile=enforce)
    assert "outside_safe_range" in PROFILE_ONLY_DIFFERENCES and "outside_safe_range" in DOC_REFUSALS


def test_canonical_zero_is_unsigned_and_exponent_free():
    """Item 1 reopened (signed zero): the entry's integer DBU zero renders "0" while
    Decimal.normalize keeps the sign of -0.0 ("-0"); every spelling of zero is one value."""
    for spelling in ("-0.0", "0E-3", "0", "-0", "0.000", "-0E+2", "0E+5"):
        assert _canon(spelling) == "0", spelling
    assert _canon(Decimal("-0.0")) == "0"
    assert _canon(Decimal(0) * Decimal("0.001")) == "0"
    assert format(Decimal("-0.0").normalize(), "f") == "-0"   # the defect the rule closes


# --------------------------------------------------------------------------- #
# The methods registry version is a POLICY, not an equality test.              #
#                                                                             #
# ci/contract-registry.json declares interconnect_methods_registry as          #
# `additive_minor` with floor 1.0. Until 2026-09-17 this loader demanded an    #
# exact "1.0" and so refused a compatible 1.1 outright, reading a MINOR as a   #
# MAJOR. The table below is the policy; without the higher-minor row an        #
# equality test passes every other case identically.                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("declared,verdict", [
    ("1.0", "accept"),          # the floor, silent
    ("1.0.7", "accept"),        # PATCH is ignored throughout
    ("1.1", "report"),          # THE ROW THAT MATTERS: additive minor, usable
    ("1.9", "report"),
    ("0.9", "refuse"),          # a different major, in both directions
    ("2.0", "refuse"),
    ("1", "refuse"),            # malformed is refused, never guessed at
    ("x", "refuse"),
])
def test_the_methods_registry_version_follows_the_additive_minor_policy(declared, verdict):
    raw = json.dumps(methods_manifest(
        lambda d: d.update(schema_version=declared))).encode("utf-8")
    reported = []
    try:
        got = entry.load_methods_manifest(
            Path("unused"), ["cupillar_opt1"], "syn_ixn", raw=raw,
            on_warn=reported.append)
    except entry.Refusal as exc:
        assert verdict == "refuse", f"{declared!r} was refused: {exc.message}"
        assert "schema_version" in exc.path
        return
    assert verdict != "refuse", f"{declared!r} was accepted and should not be"
    assert got, "a usable manifest returned no methods"
    # Accepted-but-newer must never be silent: something has to carry the fact
    # that fields were ignored, or the run looks like it understood the file.
    assert bool(reported) == (verdict == "report"), (
        f"{declared!r} reported {reported!r}")


def test_a_methods_registry_with_no_version_is_refused_and_not_defaulted():
    """Absent is not 1.0. A missing version means nobody stated the contract,
    which is a different thing from stating the one we support."""
    data = methods_manifest()
    del data["schema_version"]
    with pytest.raises(entry.Refusal) as exc:
        entry.load_methods_manifest(Path("unused"), ["cupillar_opt1"], "syn_ixn",
                                    raw=json.dumps(data).encode("utf-8"))
    assert exc.value.path == "methods_manifest.schema_version"
