# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for checks/assembly_admission.py.

Every refusal here is paired with a CONTROL that passes on the same fixture with
one thing changed back. That pairing is the point of the file rather than a
courtesy: a check that only ever refuses passes any test that only asks it to
refuse, so a refusal test alone cannot tell a working seam from a broken one that
always says no. The same discipline covers the third outcome, which is shown
reachable and shown NOT to be reachable from a healthy fixture.

Two of the fixtures are built from the demo's own shipped pin list
(``examples/two_die_interposer/chiplets/metal_test_chiplet.pins.json``) rather
than from invented pads, because the two properties under test there -- a pin
list with no ``die_bbox_dbu``, and 58 pads that repeat GND, NC and VCC -- are
properties the demo actually has today.

The contract, its schema and its rules belong to chiplet-spec. Nothing in this
file restates any of them; T7 asserts that nothing in the check does either.
"""

import ast
import json
import hashlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

yaml = pytest.importorskip("yaml", reason="the .chiplet reader needs PyYAML")

ADK_ROOT = Path(__file__).resolve().parents[1]
CHECKS_DIR = ADK_ROOT / "checks"
if str(CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(CHECKS_DIR))

import assembly_admission as adm  # noqa: E402

#: Where the shipped demo pin list is, relative to whichever base holds it.
DEMO_PINS_REL = Path("examples") / "two_die_interposer" / "chiplets" / \
    "metal_test_chiplet.pins.json"

#: Explicit override, the same seam ``tests/test_chiplet2dbx.py`` already uses
#: for this exact file. It wins over every guess below, because a caller that
#: knows where the example is should not have to match a layout.
DEMO_PINS_ENV = "ADK_DEMO_PINS"

#: The layouts this suite is actually run in. It was one guess,
#: ``ADK_ROOT.parents[1]``, which is the repository root when the ADK sits at
#: tools/adk in an adk-tools checkout and is WRONG everywhere else. In the
#: adk-tools image the ADK is installed at /opt/adk-tools/adk and the examples
#: ship at /opt/adk-tools/examples, so that guess resolved to /opt/examples and
#: the two demo fixtures below skipped. pytest does not call that a pass: a skip
#: is its own outcome and it is printed as one. What it does is exit 0, so a
#: caller that reads the exit status, which is what a build step does, cannot
#: tell the two apart. A skip caused by a layout the suite does not know about
#: then looks exactly like a checkout that genuinely has no example, which is
#: why the bases are listed rather than assumed.
DEMO_PINS_BASES = (
    ADK_ROOT.parents[1],            # tools/adk inside an adk-tools checkout
    ADK_ROOT.parent,                # /opt/adk-tools in the image
    ADK_ROOT.parent / "adk-tools",  # adk beside adk-tools, as test_chiplet2dbx resolves it
)


def _resolve_demo_pins(bases=DEMO_PINS_BASES, override=None):
    """The demo pin list, or the first base tried, for the skip reason."""
    override = override if override is not None else os.environ.get(DEMO_PINS_ENV)
    if override:
        return Path(override)
    for base in bases:
        candidate = base / DEMO_PINS_REL
        if candidate.is_file():
            return candidate
    return bases[0] / DEMO_PINS_REL


#: The shipped demo pin list, when one of the known layouts has it. The two
#: demo-derived fixtures skip without it rather than inventing a stand-in that
#: would be testing a property nothing ships.
DEMO_PINS = _resolve_demo_pins()


def _strict_unavailable_reason():
    """Why strict is unavailable here, or None.

    The reason travels rather than being flattened to a boolean: a fixed
    "not installed" message was wrong for the case that actually turned up,
    an installed reader at an incompatible strict API revision, and a skip
    that misattributes its own cause sends the reader to the wrong problem.
    """
    try:
        adm.load_admission()
    except adm.AdmissionUnavailable as exc:
        return str(exc)
    return None


_STRICT_GAP = _strict_unavailable_reason()

needs_strict = pytest.mark.skipif(
    _STRICT_GAP is not None,
    reason="strict admission is unavailable here: %s" % _STRICT_GAP)


class _CapabilityInt(int):
    pass


_REVISION = min(adm.SUPPORTED_REVISIONS)


@pytest.mark.parametrize("capability",
                         [None, 0, False, True, 1, 2, 3, str(_REVISION),
                          _REVISION + 1, float(_REVISION), _CapabilityInt(_REVISION)])
def test_incompatible_strict_api_is_tooling_unknown_before_schema(
        tmp_path, monkeypatch, capability):
    """Synthetic reader hooks: missing/wrong capability never reaches admission.

    The same reader admits once only its declaration is restored to an exact
    int the check supports. Earlier revisions are in the list because a stale
    installation still exports them, and the reason has to name the value
    found and its type, or a reader at the wrong revision reads as a reader
    that is missing.
    No installed strict reader, schema validation or geometry is assumed here.
    """
    reader = ModuleType("chiplet_format_io")
    reader.__file__ = str(tmp_path / "reader" / "__init__.py")
    reader.__path__ = [str(tmp_path / "reader")]
    contract = ModuleType("chiplet_format_io.die_contract")
    contract.__file__ = str(tmp_path / "reader" / "die_contract.py")
    if capability is not None:  # None represents an absent export.
        contract.STRICT_ADMISSION_API_VERSION = capability
    contract.DieContractError = type("SyntheticContractError", (Exception,), {})
    calls = []
    contract.die_contract_schema = lambda: calls.append("schema")
    contract.admit_component = lambda *args, **kwargs: calls.append("admit")
    reader.die_contract = contract
    monkeypatch.setitem(sys.modules, "chiplet_format_io", reader)
    monkeypatch.setitem(sys.modules, "chiplet_format_io.die_contract", contract)
    monkeypatch.setitem(sys.modules, "jsonschema", ModuleType("jsonschema"))

    assembly = {"components": [{"id": "U1", "type": "die"}]}
    report = adm.admit_assembly(assembly, tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_UNDECIDABLE
    assert report["cause"] == adm.CAUSE_TOOLING
    assert adm.exit_code(report) == report["exit_code"] == 2
    assert "STRICT_ADMISSION_API_VERSION" in report["reasons"][0]
    # The formatted fragment, not a bare repr: the message already carries a tmp
    # path full of digits, so "1 in reasons" is near-vacuous. Pinning "got %r (%s)"
    # makes every parameter load-bearing: a reader at the wrong revision must not
    # read as a reader that is missing.
    assert ("got %r (%s)" % (capability, type(capability).__name__)) in report["reasons"][0]
    assert calls == []

    # Legacy stays independent of compatibility and imports no strict API.
    assert adm.admit_assembly(assembly, tmp_path, profile="legacy")["exit_code"] == 0
    assert calls == []

    contract.STRICT_ADMISSION_API_VERSION = _REVISION
    report = adm.admit_assembly(assembly, tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_ADMITTED
    assert report["exit_code"] == 0
    assert calls == ["schema", "admit"]


# ---------------------------------------------------------------------------
# Builders. One bundle directory per die: layout, build record, inventory and
# contract all live together, which is what the contract's own path rule wants.
# ---------------------------------------------------------------------------

def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pads(count=4, *, unique=True, size=True):
    pads = []
    for i in range(count):
        name = "SIG%d" % i if unique else "GND"
        pad = {"name": name, "pad_index": i,
               "center_x_dbu": 1000 * (i + 1), "center_y_dbu": 2000}
        if size:
            pad["width_dbu"] = 40000
            pad["height_dbu"] = 40000
        pads.append(pad)
    return pads


def _box_for(pads):
    xs = [p["center_x_dbu"] for p in pads]
    ys = [p["center_y_dbu"] for p in pads]
    return {"x_min": min(xs) - 50000, "y_min": min(ys) - 50000,
            "x_max": max(xs) + 50000, "y_max": max(ys) + 50000}


def _terminal(pad, ports):
    """One terminal, optionally carrying the port identity the 1.1 minor adds."""
    term = {"name": pad["name"], "pad_index": pad["pad_index"], "role": "signal",
            "opening": {"purpose": "conductor", "gds_layer": 134,
                        "gds_datatype": 0, "shape": "rectangle"}}
    if ports:  # one lane per port: a signal port over several pads is a bus
        term["port"] = pad["name"]
        term["ordinal"] = 0
    return term


def make_bundle(root, name="die", *, pads=None, with_bbox=True, dbu=0.001,
                top_cell="DIE_TOP", version="1.0", ports=False, supply_domains=None):
    """Write one internally consistent die bundle under ``root/name``."""
    bundle = Path(root) / name
    bundle.mkdir(parents=True, exist_ok=True)
    pads = _pads() if pads is None else pads

    body = {"version": "1.0", "source_type": "hand_authored", "dbu_um": dbu,
            "pins": pads}
    if with_bbox:
        body["die_bbox_dbu"] = _box_for(pads)
    pins = bundle / "pins.json"
    pins.write_text(json.dumps(body, indent=2) + "\n")

    layout = bundle / "die.gds"
    layout.write_bytes(b"not a real gds, but real bytes")
    build = bundle / "build_record.json"
    build.write_text(json.dumps({"tool": "adk-tests", "version": "0"}) + "\n")

    box = _box_for(pads)
    contract = {
        "schema": "die-output-contract", "version": version,
        "die": {"name": name, "layout": layout.name, "layout_sha256": _sha(layout),
                "top_cell": top_cell, "dbu_um": dbu,
                "bbox_um": [box["x_min"] * dbu, box["y_min"] * dbu,
                            box["x_max"] * dbu, box["y_max"] * dbu],
                "frame": "die_local_gds_origin"},
        "process": {"pdk": "ihp-sg13g2", "pdk_version": "dev-adk-tests"},
        "build_record": {"path": build.name, "sha256": _sha(build)},
        "inventory": {"kind": "pins_json", "path": pins.name, "sha256": _sha(pins)},
        "terminals": [_terminal(p, ports) for p in pads],
    }
    if supply_domains is not None:  # the last pad becomes a ground in that domain
        domain = next(iter(supply_domains))
        ground = contract["terminals"][-1]
        ground["role"] = "ground"
        ground["supply_domain"] = domain
        if ports:
            ground["port"] = domain
        contract["supply_domains"] = supply_domains
    path = bundle / "die_contract.json"
    path.write_text(json.dumps(contract, indent=2) + "\n")
    return path


def make_component(root, contract_path, *, ident="U1", ctype="die",
                   top_cell="DIE_TOP", with_reference=True):
    root = Path(root)
    layout = Path(contract_path).parent / "die.gds"
    comp = {"id": ident, "type": ctype, "anchor": "gds_origin",
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "layout": str(layout.relative_to(root)), "top_cell": top_cell}
    if with_reference:
        comp["die_contract"] = {"path": str(Path(contract_path).relative_to(root)),
                                "sha256": _sha(contract_path)}
    return comp


def assembly_of(*components):
    return {"format_version": "1.0",
            "assembly": {"name": "test", "units": "um"},
            "components": list(components)}


# ---------------------------------------------------------------------------
# T1. Legacy is the absence of a call.
# ---------------------------------------------------------------------------

_PROBE = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {checks!r})
    import assembly_admission as adm
    report = adm.admit_assembly({assembly!r}, {base!r}, profile={profile!r})
    print(json.dumps({{
        "outcome": report["outcome"],
        "exit_code": report["exit_code"],
        "census": report["census"],
        "die_contract_imported": "chiplet_format_io.die_contract" in sys.modules,
    }}))
""")


def _probe(assembly, base, profile):
    """Run one admit_assembly in a FRESH process and report what it imported.

    In-process this could not be measured: by the time the suite reaches here
    another test has already imported the reader, so sys.modules would say yes
    whatever the profile did.
    """
    code = _PROBE.format(checks=str(CHECKS_DIR), assembly=assembly,
                         base=str(base), profile=profile)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_legacy_imports_no_contract_reader(tmp_path):
    contract = make_bundle(tmp_path)
    asm = assembly_of(make_component(tmp_path, contract))

    legacy = _probe(asm, tmp_path, "legacy")
    assert legacy["die_contract_imported"] is False
    assert legacy["outcome"] == adm.OUTCOME_NOT_REQUESTED
    assert legacy["exit_code"] == 0


@needs_strict
def test_strict_does_import_it_control(tmp_path):
    """The control for T1: without this, "not imported" could just mean the
    probe never reached the import on either path."""
    contract = make_bundle(tmp_path)
    asm = assembly_of(make_component(tmp_path, contract))

    strict = _probe(asm, tmp_path, "strict")
    assert strict["die_contract_imported"] is True
    assert strict["outcome"] == adm.OUTCOME_ADMITTED


def test_legacy_leaves_the_document_alone(tmp_path):
    contract = make_bundle(tmp_path)
    asm = assembly_of(make_component(tmp_path, contract))
    before = json.dumps(asm, sort_keys=True)

    report = adm.admit_assembly(asm, tmp_path, profile="legacy")

    assert json.dumps(asm, sort_keys=True) == before
    assert report["census"]["components_seen"] == 1
    assert report["census"]["matched"] == 0  # legacy matches nothing; it asks nothing


# ---------------------------------------------------------------------------
# T2. A component with no die_contract reference.
# ---------------------------------------------------------------------------

@needs_strict
def test_strict_refuses_a_component_with_no_reference(tmp_path):
    contract = make_bundle(tmp_path)
    comp = make_component(tmp_path, contract, with_reference=False)

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")

    assert report["outcome"] == adm.OUTCOME_REFUSED
    assert report["exit_code"] == 1
    assert "requires a die_contract reference" in report["reasons"][0]


@needs_strict
def test_strict_admits_the_same_component_with_its_reference(tmp_path):
    contract = make_bundle(tmp_path)
    comp = make_component(tmp_path, contract, with_reference=True)

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")

    assert report["outcome"] == adm.OUTCOME_ADMITTED
    assert report["census"]["matched"] == 1
    assert report["census"]["admitted"] == 1


# ---------------------------------------------------------------------------
# T2b. Verdicts that move between revisions, pinned at the accepted one.
# ---------------------------------------------------------------------------

@needs_strict
def test_a_conforming_1_1_contract_is_admitted_and_1_0_carrying_a_port_is_not(tmp_path):
    """The verdict that moved at revision 3 (1.1 port identity), at both ends.

    The flow writes the die contract at 1.1, whose terminals may carry the
    (port, ordinal) identity that minor adds. A revision 1 reader holds only the
    1.0 schema and refuses those bytes as a wrong DOCUMENT, naming a terminal
    that is correct at the minor it declares; which revision this check accepts
    is what decides whether the gate reports that false refusal. The control is
    the same terminals stamped 1.0, where the identity IS a key its own minor
    does not define and the refusal is the correct answer rather than an
    artifact of the reader's age.
    """
    admitted = make_bundle(tmp_path / "at_1_1", version="1.1", ports=True)
    report = adm.admit_assembly(
        assembly_of(make_component(tmp_path / "at_1_1", admitted)),
        tmp_path / "at_1_1", profile="strict")
    assert report["outcome"] == adm.OUTCOME_ADMITTED, report["reasons"]
    assert adm.exit_code(report) == report["exit_code"] == 0
    assert report["census"]["admitted"] == 1
    # Port identity is READ from revision 3 on, not tolerated element by element, so a
    # conforming 1.1 contract admits with no warning; a non-empty list here would
    # mean the reader fell back to element-level tolerance instead.
    assert report["warnings"] == []

    refused = make_bundle(tmp_path / "at_1_0", version="1.0", ports=True)
    report = adm.admit_assembly(
        assembly_of(make_component(tmp_path / "at_1_0", refused)),
        tmp_path / "at_1_0", profile="strict")
    assert report["outcome"] == adm.OUTCOME_REFUSED, report["reasons"]
    assert adm.exit_code(report) == report["exit_code"] == 1
    # Pin the offending key only; the terminal index and the jsonschema phrasing
    # are the reader's to change.
    assert "port" in report["reasons"][0]


@needs_strict
def test_a_1_2_contract_with_supply_domains_is_admitted_and_1_1_carrying_them_is_not(tmp_path):
    """The verdict that moved at revision 4, at both ends.

    Revision 4 accepts the 1.2 minor, whose die declares supply_domains, and
    reads them; revision 3 read such a contract element by element and admitted
    it with a warning, the domains ignored. So the 1.2 contract admits with no
    warning here. The control is the same domains stamped 1.1, where
    supply_domains is a key its own minor does not define.
    """
    domains = {"vss": {"volts": 0}}
    admitted = make_bundle(tmp_path / "at_1_2", version="1.2", ports=True,
                           supply_domains=domains)
    report = adm.admit_assembly(
        assembly_of(make_component(tmp_path / "at_1_2", admitted)),
        tmp_path / "at_1_2", profile="strict")
    assert report["outcome"] == adm.OUTCOME_ADMITTED, report["reasons"]
    assert adm.exit_code(report) == report["exit_code"] == 0
    assert report["warnings"] == []

    refused = make_bundle(tmp_path / "at_1_1", version="1.1", ports=True,
                          supply_domains=domains)
    report = adm.admit_assembly(
        assembly_of(make_component(tmp_path / "at_1_1", refused)),
        tmp_path / "at_1_1", profile="strict")
    assert report["outcome"] == adm.OUTCOME_REFUSED, report["reasons"]
    assert adm.exit_code(report) == report["exit_code"] == 1
    assert "supply_domain" in report["reasons"][0]


@needs_strict
def test_a_1_1_contract_with_an_unknown_top_level_key_is_refused(tmp_path):
    """The largest verdict-move class: at the accepted minor an unknown key turns
    a green admission red. Revision 2 admitted it with a warning; from revision 3
    on the 1.1 document is validated against the shipped 1.1 schema and refused.
    """
    contract = make_bundle(tmp_path, version="1.1", ports=True)
    doc = json.loads(contract.read_text())
    doc["unknown_top_level"] = "x"
    contract.write_text(json.dumps(doc, indent=2) + "\n")
    # make_component hashes the edited file, so the digest matches and the
    # refusal is the unknown key, not a stale-digest mismatch.
    comp = make_component(tmp_path, contract)

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_REFUSED, report["reasons"]
    assert adm.exit_code(report) == report["exit_code"] == 1


# ---------------------------------------------------------------------------
# T3. A reference whose digest does not match the sidecar on disk.
# ---------------------------------------------------------------------------

@needs_strict
def test_strict_refuses_a_stale_digest(tmp_path):
    contract = make_bundle(tmp_path)
    comp = make_component(tmp_path, contract)
    original = contract.read_text()
    contract.write_text(original.replace('"pdk_version": "dev-adk-tests"',
                                         '"pdk_version": "dev-adk-tests2"'))

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_REFUSED
    assert "hashes to" in report["reasons"][0]

    contract.write_text(original)  # the control: one thing changed back
    again = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert again["outcome"] == adm.OUTCOME_ADMITTED


# ---------------------------------------------------------------------------
# T4 / T5. The demo's own pin list, which has both properties today.
# ---------------------------------------------------------------------------

def _demo_pins_skip_reason(resolved=None, override=None):
    """Why the demo fixtures are not running, including where they ended up.

    Said "%s is unset" unconditionally, which is a statement about the
    environment made without looking at it: an ADK_DEMO_PINS pointing at a path
    that does not exist produced a reason claiming the variable was never set,
    and the one person who had set it was the one it misled.
    """
    resolved = DEMO_PINS if resolved is None else resolved
    override = os.environ.get(DEMO_PINS_ENV) if override is None else override
    if override:
        return ("%s=%s names no file (resolved to %s)"
                % (DEMO_PINS_ENV, override, resolved))
    return ("no two_die_interposer example under %s, %s is unset, resolved to %s"
            % (", ".join(str(b) for b in DEMO_PINS_BASES), DEMO_PINS_ENV, resolved))


needs_demo = pytest.mark.skipif(not DEMO_PINS.is_file(), reason=_demo_pins_skip_reason())


# The locator itself, because the two fixtures below are the ones that went
# quiet when it was a single guess. The skips were visible in the report and
# invisible in the exit status, so a step that reads the status could not
# distinguish "this checkout has no example" from "the example is here and this
# suite looked in the wrong place".

@pytest.fixture
def no_demo_override(monkeypatch):
    """Take the environment out of the question for the base-order tests.

    Earned by a failure rather than added for tidiness: with ADK_DEMO_PINS
    exported, which is how the image and any explicit caller will run this
    suite, the override outranks the bases and these three tests were reading
    that instead of what they meant to exercise.
    """
    monkeypatch.delenv(DEMO_PINS_ENV, raising=False)


def test_the_demo_locator_finds_the_example_under_each_known_layout(
        tmp_path, no_demo_override):
    for name in ("checkout", "image", "sibling"):
        base = tmp_path / name
        pins = base / DEMO_PINS_REL
        pins.parent.mkdir(parents=True)
        pins.write_text("{}")
        assert _resolve_demo_pins(bases=(tmp_path / "absent", base)) == pins


def test_the_demo_locator_covers_the_layout_the_image_installs():
    # /opt/adk-tools/adk with the examples at /opt/adk-tools/examples: the
    # layout that resolved to /opt/examples and silently skipped both fixtures.
    assert ADK_ROOT.parent in DEMO_PINS_BASES


def test_an_explicit_demo_pins_path_beats_every_guess(tmp_path, no_demo_override):
    guessed = tmp_path / "guessed"
    (guessed / DEMO_PINS_REL).parent.mkdir(parents=True)
    (guessed / DEMO_PINS_REL).write_text("{}")
    named = tmp_path / "named.pins.json"
    named.write_text("{}")
    assert _resolve_demo_pins(bases=(guessed,), override=str(named)) == named


def test_the_locator_reports_a_path_rather_than_none_when_nothing_is_found(
        tmp_path, no_demo_override):
    # The skip reason has to name something a reader can go and look at.
    missing = _resolve_demo_pins(bases=(tmp_path,))
    assert missing == tmp_path / DEMO_PINS_REL
    assert not missing.is_file()


def test_an_explicit_path_that_names_no_file_is_taken_and_reported_as_such(tmp_path):
    # The control that was missing: the override is honoured even when it is
    # wrong, rather than falling back to a guess that might accidentally work,
    # and the reason says so instead of blaming an unset variable.
    named = tmp_path / "not-here.pins.json"
    resolved = _resolve_demo_pins(bases=(tmp_path,), override=str(named))
    assert resolved == named and not resolved.is_file()
    reason = _demo_pins_skip_reason(resolved=resolved, override=str(named))
    assert str(named) in reason
    assert "is unset" not in reason


@needs_strict
@needs_demo
def test_strict_refuses_the_demo_repeated_pad_names(tmp_path):
    pads = json.loads(DEMO_PINS.read_text())["pins"]
    assert len({p["name"] for p in pads}) < len(pads), "the demo stopped repeating names"

    contract = make_bundle(tmp_path, pads=pads, top_cell="Metal_Test")
    comp = make_component(tmp_path, contract, top_cell="Metal_Test")

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_REFUSED
    assert "repeat the names" in report["reasons"][0]


@needs_strict
@needs_demo
def test_strict_admits_the_same_pads_with_distinct_names_control(tmp_path):
    pads = json.loads(DEMO_PINS.read_text())["pins"]
    for i, pad in enumerate(pads):
        pad["name"] = "%s_%d" % (pad["name"], i)

    contract = make_bundle(tmp_path, pads=pads, top_cell="Metal_Test")
    comp = make_component(tmp_path, contract, top_cell="Metal_Test")

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_ADMITTED
    assert report["census"]["admitted"] == 1


@needs_strict
def test_strict_refuses_an_inventory_with_no_die_box(tmp_path):
    contract = make_bundle(tmp_path, with_bbox=False)
    comp = make_component(tmp_path, contract)

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_REFUSED
    assert "die_bbox_dbu" in report["reasons"][0]


@needs_strict
def test_strict_admits_it_once_the_die_box_is_there_control(tmp_path):
    contract = make_bundle(tmp_path, with_bbox=True)
    comp = make_component(tmp_path, contract)

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")
    assert report["outcome"] == adm.OUTCOME_ADMITTED


# ---------------------------------------------------------------------------
# T6. The third outcome, and that it is not a warning.
# ---------------------------------------------------------------------------

_BLOCK_JSONSCHEMA = textwrap.dedent("""
    import sys
    class Blocker:
        def find_module(self, name, path=None):
            return self if name == "jsonschema" or name.startswith("jsonschema.") else None
        def load_module(self, name):
            raise ImportError("blocked for this process: " + name)
        def find_spec(self, name, path=None, target=None):
            if name == "jsonschema" or name.startswith("jsonschema."):
                raise ImportError("blocked for this process: " + name)
            return None
    sys.meta_path.insert(0, Blocker())
    sys.modules.pop("jsonschema", None)
""")


def _run_cli(args, *, prelude=""):
    code = prelude + textwrap.dedent("""
        import sys
        sys.path.insert(0, {checks!r})
        import assembly_admission as adm
        raise SystemExit(adm.main({args!r}))
    """).format(checks=str(CHECKS_DIR), args=list(args))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def _write_chiplet(path, assembly):
    Path(path).write_text(yaml.safe_dump(assembly, sort_keys=False))
    return Path(path)


@needs_strict
def test_missing_jsonschema_is_undecidable_and_never_a_warning(tmp_path):
    contract = make_bundle(tmp_path)
    doc = _write_chiplet(tmp_path / "a.chiplet",
                         assembly_of(make_component(tmp_path, contract)))

    blocked = _run_cli(["--chiplet", str(doc), "--profile", "strict"],
                       prelude=_BLOCK_JSONSCHEMA)
    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    assert "jsonschema" in blocked.stderr
    # The failure mode this test exists for: a profile that degrades to a note.
    assert "WARNING" not in blocked.stdout and "WARNING" not in blocked.stderr

    healthy = _run_cli(["--chiplet", str(doc), "--profile", "strict"])
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr


@needs_strict
def test_a_vendored_reader_on_sys_path_is_undecidable_not_admitted(tmp_path):
    """The ADK's own consumers put vendor/ first, so this is reachable in a real
    process rather than hypothetical. It must be code 2, not a quiet pass."""
    contract = make_bundle(tmp_path)
    doc = _write_chiplet(tmp_path / "a.chiplet",
                         assembly_of(make_component(tmp_path, contract)))
    prelude = "import sys; sys.path.insert(0, %r)\n" % str(ADK_ROOT / "vendor")

    shadowed = _run_cli(["--chiplet", str(doc), "--profile", "strict"], prelude=prelude)
    assert shadowed.returncode == 2, shadowed.stdout + shadowed.stderr
    assert "vendored copy" in shadowed.stderr


# ---------------------------------------------------------------------------
# T7. No second copy of the contract. This one never skips.
# ---------------------------------------------------------------------------

def test_the_adk_ships_no_die_contract_schema():
    strays = [p for p in ADK_ROOT.rglob("die_contract*.json")
              if ".git" not in p.parts]
    assert strays == [], (
        "adk-tools must not carry a copy of the die contract schema; chiplet-spec "
        "owns it and it ships inside chiplet-format-io: %s" % strays)


def test_the_check_restates_no_contract_rule():
    """No vocabulary from the contract is redefined here, and nothing validates.

    A small convenience copy is exactly how two sources of truth start, so the
    names the reference owns are asserted absent rather than trusted absent.
    """
    source = (CHECKS_DIR / "assembly_admission.py").read_text()
    tree = ast.parse(source)

    owned = {"TERMINAL_ROLES", "OPENING_PURPOSES", "OPENING_SHAPES",
             "DIE_CONTRACT_SCHEMA", "SUPPORTED_DIE_CONTRACT_VERSION",
             "ACCEPTED_DIE_CONTRACT_VERSIONS"}
    assigned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
    assert not (assigned & owned), (
        "these names belong to chiplet_format_io.die_contract and must not be "
        "restated here: %s" % sorted(assigned & owned))

    validating = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "jsonschema"]
    assert validating == [], (
        "the check must not validate anything itself; the shipped schema is "
        "applied by the reader")


# ---------------------------------------------------------------------------
# T8 / T9. The census, and an empty subject that must not pass.
# ---------------------------------------------------------------------------

@needs_strict
def test_an_assembly_with_no_dies_is_undecidable_not_admitted(tmp_path):
    interposer = {"id": "interposer", "type": "interposer",
                  "layout": "layout/i.gds", "top_cell": "INTERPOSER"}

    report = adm.admit_assembly(assembly_of(interposer), tmp_path, profile="strict")

    assert report["outcome"] == adm.OUTCOME_UNDECIDABLE
    assert report["exit_code"] == 2
    assert report["cause"] == adm.CAUSE_COVERAGE
    assert report["census"] == {"components_seen": 1, "matched": 0, "admitted": 0,
                                "refused": 0, "excluded": 1, "unclassified": 0,
                                "types_seen": {"interposer": 1}}
    assert "matched no component" in report["reasons"][0]


@needs_strict
def test_an_unexpected_type_spelling_is_undecidable_not_admitted(tmp_path):
    """`type` is free-form in the schema, so this is the realistic way coverage
    goes incomplete: not an assembly without dies, a spelling nobody knows."""
    contract = make_bundle(tmp_path)
    comp = make_component(tmp_path, contract, ctype="Die")

    report = adm.admit_assembly(assembly_of(comp), tmp_path, profile="strict")

    assert report["outcome"] == adm.OUTCOME_UNDECIDABLE
    assert report["cause"] == adm.CAUSE_COVERAGE
    assert report["census"]["types_seen"] == {"Die": 1}
    assert report["census"]["unclassified"] == 1
    assert "could not classify" in report["reasons"][0]


@needs_strict
def test_a_clean_die_does_not_carry_an_unknown_type_to_a_pass(tmp_path):
    """The case a match COUNT misses: the subject is not empty, it is partial.

    One die admits perfectly and one component cannot be classified at all. A
    check that reported on what it understood would exit 0 here and would never
    have looked at half the assembly.
    """
    good = make_bundle(tmp_path, name="good")
    stray = make_bundle(tmp_path, name="stray")
    report = adm.admit_assembly(
        assembly_of(make_component(tmp_path, good, ident="U1"),
                    make_component(tmp_path, stray, ident="X1", ctype="Die")),
        tmp_path, profile="strict")

    assert report["census"]["admitted"] == 1, "the good die really did admit"
    assert report["outcome"] == adm.OUTCOME_UNDECIDABLE
    assert report["exit_code"] == 2
    assert report["cause"] == adm.CAUSE_COVERAGE


@needs_strict
def test_recognised_non_die_types_do_not_make_coverage_incomplete_control(tmp_path):
    """The control for the two above: interposer and substrate are RECOGNISED,
    so a normal assembly is not dragged to 2 by the same rule."""
    contract = make_bundle(tmp_path)
    report = adm.admit_assembly(
        assembly_of(make_component(tmp_path, contract, ident="U1"),
                    {"id": "interposer", "type": "interposer"},
                    {"id": "sub", "type": "substrate"}),
        tmp_path, profile="strict")

    assert report["outcome"] == adm.OUTCOME_ADMITTED
    assert report["census"]["excluded"] == 2
    assert report["census"]["unclassified"] == 0


@needs_strict
def test_a_mixed_package_is_undecidable(tmp_path):
    """__file__ not being the vendored module does not mean nothing vendored is
    in the package: a search path with vendor/ on it yields a mixed one, part
    installed and part vendored, and nothing announces it."""
    import chiplet_format_io

    original = list(chiplet_format_io.__path__)
    chiplet_format_io.__path__.append(str(ADK_ROOT / "vendor" / "chiplet_format_io"))
    try:
        with pytest.raises(adm.AdmissionUnavailable) as caught:
            adm.load_admission()
        assert "part installed and part vendored" in str(caught.value)
    finally:
        chiplet_format_io.__path__[:] = original

    adm.load_admission()  # the control: restored, it resolves again


@needs_strict
def test_a_die_array_is_refused_by_name_not_silently_excluded(tmp_path):
    contract = make_bundle(tmp_path)
    array = make_component(tmp_path, contract, ident="A1", ctype="die_array")
    die = make_component(tmp_path, contract, ident="U1")

    report = adm.admit_assembly(assembly_of(array, die), tmp_path, profile="strict")

    assert report["outcome"] == adm.OUTCOME_REFUSED
    assert report["census"]["refused"] == 1
    assert report["census"]["admitted"] == 1
    entry = next(c for c in report["components"] if c["id"] == "A1")
    assert entry["outcome"] == "refused"
    assert "die_array" in entry["reason"]


@needs_strict
def test_the_three_exit_codes_are_distinct_and_all_reachable(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    contract = make_bundle(good)
    admitted = _write_chiplet(good / "a.chiplet",
                              assembly_of(make_component(good, contract)))

    bad = tmp_path / "bad"
    bad.mkdir()
    contract_b = make_bundle(bad)
    refused = _write_chiplet(
        bad / "a.chiplet",
        assembly_of(make_component(bad, contract_b, with_reference=False)))

    empty = tmp_path / "empty"
    empty.mkdir()
    undecidable = _write_chiplet(
        empty / "a.chiplet",
        assembly_of({"id": "interposer", "type": "interposer",
                     "layout": "i.gds", "top_cell": "INTERPOSER"}))

    mixed = tmp_path / "mixed"
    mixed.mkdir()
    contract_m = make_bundle(mixed)
    with_array = _write_chiplet(
        mixed / "a.chiplet",
        assembly_of(make_component(mixed, contract_m, ident="U1"),
                    make_component(mixed, contract_m, ident="A1",
                                   ctype="die_array")))

    codes = [
        adm.main(["--chiplet", str(admitted), "--profile", "strict"]),
        adm.main(["--chiplet", str(refused), "--profile", "strict"]),
        adm.main(["--chiplet", str(undecidable), "--profile", "strict"]),
    ]
    assert codes == [0, 1, 2]
    assert len(set(codes)) == 3

    # One good die beside a die_array: not a pass on the strength of the subset
    # that admitted, and a refusal rather than an environment complaint.
    assert adm.main(["--chiplet", str(with_array), "--profile", "strict"]) == 1
    # And the same document under legacy is untouched by any of it.
    assert adm.main(["--chiplet", str(with_array), "--profile", "legacy"]) == 0
