# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""assembly_admission -- strict die-contract admission for a ``.chiplet`` assembly.

Assembly-stage gate: for every die component of a finalized ``.chiplet``, ask
``chiplet_format_io.die_contract.admit_component`` whether the die is admissible,
and turn the answer into an exit code. The contract, its schema and every rule
in it belong to chiplet-spec; nothing here restates any of them. This module is
a seam and a census, not a second validator.

Two behaviours hold together, and the construction is what makes them hold
rather than a promise that they do:

* **Legacy is the ABSENCE of a call.** ``profile="legacy"`` returns before
  importing ``die_contract`` at all, so a reader that never asks for admission
  gains no import, no dependency and no behaviour change. A legacy ``.chiplet``
  loads exactly as it did. The guarantee is asserted by the suite, not assumed:
  ``chiplet_format_io.die_contract`` must be absent from ``sys.modules`` after a
  legacy run.

* **Strict enforces, and never degrades to a warning.** Where strict is on, a
  question that cannot be answered is a red gate, not a pass with a note. That
  is what the third exit code is for.

Three outcomes, because two cannot hold them:

  0  ADMITTED      every matched component was admitted (or the profile is
                   legacy, which asked nothing).
  1  REFUSED       admission refused a component, with the reason it gave.
  2  UNDECIDABLE   strict was requested and the answer is unknown, for one of
                   two causes the report keeps apart: ``tooling_unavailable``
                   (the reader is not installed, is installed without
                   ``die_contract``, resolves to the ADK's vendored copy, ships
                   no schema, has an unsupported strict API revision, or has
                   no ``jsonschema``) and
                   ``coverage_incomplete`` (the assembly holds a component this
                   profile cannot classify, or nothing matched at all).

There is deliberately no path from 2 to 0, and a refusal never becomes a 2: "the
contract said no" is a finding about the document, while 2 says the answer is not
known, and collapsing them is how a red gate starts reading as an environment
problem. The vocabulary is the one the ADK checks already use
(``pads_vs_pillars``): 0 clean, 1 findings, 2 usage / validation / tooling.

**What the revision 4 reader changes here, and what this module does not yet do.**
A reader that cannot read part of a document above its accepted minor raises
``DieContractIncomplete``, a ``DieContractError`` subclass, so it lands in the
refusal branch and is reported REFUSED, exit 1, not UNDECIDABLE with cause
``coverage_incomplete``. That fails closed; mapping it to the third outcome is
a separate change. A document above the accepted minor (1.3 and up) is read
element by element, so an unknown key there is ADMITTED, exit 0, with the
reader's warning collected into ``report["warnings"]``: that warning is the
reader's answer for the minor, not a strict failure downgraded here.

**Why coverage is a census and not a count.** ``type`` is deliberately NOT an
enum in ``chiplet.schema.json``: the spec names four canonical values and leaves
the field free-form for a host that models something else. So a type spelling
this profile does not expect does not fail, it silently matches nothing. A count
of matches catches only the empty case; the one it misses is the MIXED assembly,
where a recognised die admits and an unknown type rides along beside it and the
gate goes green on the strength of the subset it understood. The profile
therefore classifies EVERY component: ``die`` is checked, ``interposer`` and
``substrate`` are recognised and excluded without penalty, ``die_array`` is
refused by name, and anything else leaves coverage incomplete and forces
UNDECIDABLE even when another die admitted cleanly.

**Why the installed distribution and not the vendored reader.** The die contract
schema ships INSIDE the ``chiplet-format-io`` package; a vendored copy of the
module carries no schema, and strict admission refuses rather than falling back
to whatever checks happen to be written in Python. The ADK's own vendored copy
has no ``die_contract`` at all, and even a verbatim vendored pair of modules
imports and then refuses with "die_contract.schema.json is not present in this
installation". So strict resolves the INSTALLED distribution and refuses to
proceed if what it got is the vendored copy on ``sys.path``. In a process that
already imported one of them, ``sys.modules`` wins over any path insertion, which is exactly why strict
checks the identity of what it received instead of trusting the import.

Importable entry points:

    AdmissionUnavailable                        (the third outcome, as an error)
    load_admission() -> module                  (the installed die_contract)
    admit_assembly(assembly, base_dir, *, profile=...) -> report dict
    exit_code(report) -> int
    main(argv=None) -> int

New options must be appended as trailing keyword arguments with defaults so
importers stay source-compatible (see docs/integration.md).
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ADK_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ADK_ROOT / "vendor"

#: The two profiles. ``legacy`` asks nothing and imports nothing.
PROFILES = ("legacy", "strict")

#: Strict admission revisions (STRICT_ADMISSION_API_VERSION) this check accepts.
#: Revision 4 accepts the die contract minor 1.2, which adds the die's
#: supply_domains, and reads the terminal port identity; revision 3 would read a
#: 1.2 contract element by element and ignore its supply_domains.
SUPPORTED_REVISIONS = frozenset({4})

#: Component types strict admission checks. A die output contract binds ONE
#: layout digest and ONE top cell, so it speaks for exactly one die.
ADMITTED_TYPES = ("die",)

#: Known types the contract does not describe, and does not need to. Excluded
#: without penalty because the profile RECOGNISES them, which is the whole
#: difference between this and the bucket below.
PERMITTED_TYPES = ("interposer", "substrate")

#: Component types strict admission REFUSES rather than skipping. A ``die_array``
#: places several instances while the contract binds a single layout, so
#: admitting it would state something about one die and imply it about all of
#: them. Excluding it instead would be worse: in a mixed assembly the real dies
#: would be admitted, the array would ride along unchecked, and the gate would
#: be green. Refusing says which.
REFUSED_TYPES = ("die_array",)

#: Why an UNDECIDABLE is undecidable. Both exit 2, and they are kept apart in
#: the report because they are different facts: a document this profile cannot
#: fully classify is not an environment problem, and letting the two collapse is
#: how a red gate starts reading as a broken machine.
CAUSE_TOOLING = "tooling_unavailable"
CAUSE_COVERAGE = "coverage_incomplete"

OUTCOME_ADMITTED = "ADMITTED"
OUTCOME_REFUSED = "REFUSED"
OUTCOME_UNDECIDABLE = "UNDECIDABLE"
OUTCOME_NOT_REQUESTED = "NOT_REQUESTED"

#: The only mapping from an outcome to a process exit status.
EXIT_CODES = {
    OUTCOME_ADMITTED: 0,
    OUTCOME_NOT_REQUESTED: 0,
    OUTCOME_REFUSED: 1,
    OUTCOME_UNDECIDABLE: 2,
}


class AdmissionUnavailable(RuntimeError):
    """Strict admission was requested and the tooling cannot answer.

    Deliberately not a subclass of the reader's error types: "the check said no"
    and "we cannot tell what the check would say" are different facts, and the
    exit codes keep them apart.
    """


def load_admission():
    """Return the INSTALLED ``chiplet_format_io.die_contract``, or refuse.

    Every failure here is the third outcome, never a refusal and never a
    warning. The two schema/jsonschema probes are done UP FRONT on purpose: both
    surface inside ``admit_component`` as a ``DieContractError``, which this
    module would otherwise classify as a component refusal (exit 1) when the
    truth is that the tooling could not answer (exit 2). Reporting "cannot tell"
    as "refused" is a quieter defect than the reverse and just as wrong.
    """
    try:
        import chiplet_format_io
    except ImportError as exc:
        raise AdmissionUnavailable(
            "chiplet_format_io is not installed, so strict admission cannot ask "
            "the contract anything; install chiplet-format-io[strict]"
        ) from exc

    resolved = Path(getattr(chiplet_format_io, "__file__", "") or "").resolve()
    if _under_vendor(resolved):
        raise AdmissionUnavailable(
            "the chiplet_format_io on sys.path is the ADK's vendored copy at %s; "
            "the die contract schema ships inside the chiplet-format-io package "
            "and a vendored module carries none, so strict admission needs the "
            "installed distribution" % resolved
        )
    # A package can resolve across SEVERAL directories, so "__file__ is not the
    # vendored one" is not the same statement as "nothing vendored is in this
    # package". A search path with vendor/ on it yields a mixed package, part
    # installed and part vendored, with nothing announcing it, so a mixture is
    # refused here rather than left to fail somewhere quieter.
    mixed = [entry for entry in getattr(chiplet_format_io, "__path__", None) or []
             if _under_vendor(Path(entry).resolve())]
    if mixed:
        raise AdmissionUnavailable(
            "the chiplet_format_io package resolves across the ADK's vendor "
            "directory as well as its installation (%s); a package that is part "
            "installed and part vendored cannot be reasoned about, so strict "
            "admission stops rather than guessing which half answered"
            % ", ".join(str(m) for m in mixed)
        )

    try:
        from chiplet_format_io import die_contract
    except ImportError as exc:
        raise AdmissionUnavailable(
            "the installed chiplet_format_io at %s has no die_contract module, so "
            "it predates the die output contract; install a release that ships it"
            % resolved
        ) from exc

    submodule = Path(getattr(die_contract, "__file__", "") or "").resolve()
    if submodule.parent != resolved.parent:
        raise AdmissionUnavailable(
            "chiplet_format_io was imported from %s and its die_contract from %s; "
            "the reader and the admission module are not the same installation"
            % (resolved.parent, submodule.parent)
        )

    # Package versions alone span older, weaker admission implementations, so
    # the check selects the strict submodule's revision instead. It is a SET,
    # not a floor: a verdict moves between revisions, so accepting two is
    # accepting two answers for the same bytes. This is compatibility
    # selection, not independent reader qualification. The type guard is not
    # redundant with the membership test: an int subclass compares equal while
    # being another type, and a bool must not inherit acceptance from any
    # revision it happens to equal.
    capability = getattr(die_contract, "STRICT_ADMISSION_API_VERSION", None)
    if type(capability) is not int or capability not in SUPPORTED_REVISIONS:
        raise AdmissionUnavailable(
            "chiplet_format_io.die_contract at %s must export "
            "STRICT_ADMISSION_API_VERSION as an exact integer in %s; got %r (%s). "
            "Select a compatible strict reader; this tooling cannot decide admission"
            % (submodule, sorted(SUPPORTED_REVISIONS), capability,
               type(capability).__name__)
        )

    try:
        import jsonschema  # noqa: F401  (availability probe, nothing is validated here)
    except ImportError as exc:
        raise AdmissionUnavailable(
            "jsonschema is not installed and strict admission validates against the "
            "shipped schema; install chiplet-format-io[strict]"
        ) from exc

    try:
        die_contract.die_contract_schema()
    except die_contract.DieContractError as exc:
        raise AdmissionUnavailable(str(exc)) from exc

    return die_contract


def _under_vendor(path: Path) -> bool:
    """Is ``path`` at or below the ADK's vendor directory?"""
    vendor = VENDOR_DIR.resolve()
    return path == vendor or vendor in path.parents


def _type_of(comp: Dict[str, Any]) -> str:
    value = comp.get("type")
    return value if isinstance(value, str) and value else "(unstated)"


def _census(components: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    types: Dict[str, int] = {}
    for comp in components:
        key = _type_of(comp)
        types[key] = types.get(key, 0) + 1
    return {"components_seen": len(components), "matched": 0, "admitted": 0,
            "refused": 0, "excluded": 0, "unclassified": 0, "types_seen": types}


def admit_assembly(assembly: Dict[str, Any], base_dir, *, profile: str = "legacy",
                   admitted_types: Sequence[str] = ADMITTED_TYPES,
                   refused_types: Sequence[str] = REFUSED_TYPES,
                   permitted_types: Sequence[str] = PERMITTED_TYPES,
                   on_warn=None) -> Dict[str, Any]:
    """Admit an assembly's die components under ``profile``.

    ``base_dir`` is the directory the assembly was read from, because that is
    where a component's own references are written. Everything the contract
    itself names is resolved by the reader against the contract, which is where
    THOSE are written.
    """
    if profile not in PROFILES:
        raise ValueError("unknown admission profile %r; expected one of %s"
                         % (profile, ", ".join(PROFILES)))

    components = list(assembly.get("components") or [])
    report: Dict[str, Any] = {
        "check": "assembly_admission",
        "profile": profile,
        "census": _census(components),
        "components": [],
        "reasons": [],
        "warnings": [],
        "cause": None,
    }

    if profile == "legacy":
        # The whole legacy guarantee is this early return: no import happens, so
        # nothing this module does can change how a legacy .chiplet loads.
        report["outcome"] = OUTCOME_NOT_REQUESTED
        report["exit_code"] = EXIT_CODES[OUTCOME_NOT_REQUESTED]
        return report

    try:
        dc = load_admission()
    except AdmissionUnavailable as exc:
        report["reasons"].append(str(exc))
        report["outcome"] = OUTCOME_UNDECIDABLE
        report["cause"] = CAUSE_TOOLING
        report["exit_code"] = EXIT_CODES[OUTCOME_UNDECIDABLE]
        return report

    census = report["census"]
    warn = on_warn if on_warn is not None else report["warnings"].append

    for comp in components:
        ident = comp.get("id")
        ctype = comp.get("type")
        entry: Dict[str, Any] = {"id": ident, "type": ctype}
        if ctype in refused_types:
            census["refused"] += 1
            entry["outcome"] = "refused"
            entry["reason"] = (
                "component %r is a %s: the die contract binds one layout digest and "
                "one top cell, so it speaks for a single die and cannot state "
                "anything about the other instances an array places" % (ident, ctype)
            )
            report["reasons"].append(entry["reason"])
        elif ctype in admitted_types:
            census["matched"] += 1
            try:
                dc.admit_component(comp, base_dir=base_dir, on_warn=warn)
            # DieContractIncomplete (an incomplete read above the accepted minor)
            # is a DieContractError subclass, so it lands here as a refusal today,
            # not as UNDECIDABLE; failing closed until that mapping is its own change.
            except dc.DieContractError as exc:
                census["refused"] += 1
                entry["outcome"] = "refused"
                entry["reason"] = str(exc)
                report["reasons"].append(str(exc))
            else:
                census["admitted"] += 1
                entry["outcome"] = "admitted"
        elif ctype in permitted_types:
            census["excluded"] += 1
            entry["outcome"] = "excluded"
            entry["reason"] = ("type %r is recognised and outside what a die "
                               "output contract describes" % ctype)
        else:
            # NOT excluded. An unrecognised type is a component this profile
            # could not classify at all, and a gate that passes on the subset it
            # understood is the defect the census exists to catch.
            census["unclassified"] += 1
            entry["outcome"] = "unclassified"
            entry["reason"] = ("type %r is not one this profile recognises, so "
                               "whether it needed admission is unknown" % ctype)
        report["components"].append(entry)

    seen = ", ".join("%s x%d" % (k, v)
                     for k, v in sorted(census["types_seen"].items())) or "none"
    if census["unclassified"]:
        report["reasons"].append(
            "strict admission could not classify %d of %d component(s) (%s); the "
            "recognised types are %s, and a verdict drawn from the subset this "
            "profile understood would pass an assembly it never looked at"
            % (census["unclassified"], census["components_seen"], seen,
               ", ".join(tuple(admitted_types) + tuple(permitted_types)
                         + tuple(refused_types)))
        )
    elif census["matched"] == 0 and not census["refused"]:
        # An empty subject is not a pass either. Naming the types actually
        # present is the value of the reason: the usual cause is an assembly
        # this profile has nothing to say about, and the report has to show that
        # rather than imply a check that ran.
        report["reasons"].append(
            "strict admission matched no component to check: %d component(s) present "
            "(%s) and none of type %s, so nothing was exercised and a pass here would "
            "be indistinguishable from a real one"
            % (census["components_seen"], seen, "/".join(admitted_types))
        )

    if census["refused"]:
        # A refusal is a complete answer about the document and stays code 1,
        # even alongside an unclassified component: turning a finding into "the
        # answer is unknown" is what makes a red gate read as a broken machine.
        outcome = OUTCOME_REFUSED
    elif census["unclassified"] or census["matched"] == 0:
        outcome = OUTCOME_UNDECIDABLE
        report["cause"] = CAUSE_COVERAGE
    else:
        outcome = OUTCOME_ADMITTED

    report["outcome"] = outcome
    report["exit_code"] = EXIT_CODES[outcome]
    return report


def exit_code(report: Dict[str, Any]) -> int:
    """The report's exit status, from the one mapping that defines it."""
    return EXIT_CODES[report["outcome"]]


def _load_assembly(path: Path, profile: str) -> Dict[str, Any]:
    """Read the assembly with the reader the profile is entitled to.

    Legacy reads through the ADK's vendored reader, the same idiom and the same
    module ``chiplet2dbx`` and ``pads_vs_pillars`` already use, so a legacy load
    is byte-for-byte the load it was before this module existed. Strict reads
    through the installed distribution: it is the reference reader the contract
    belongs to, and mixing the two inside one process is what the identity check
    in :func:`load_admission` exists to catch.
    """
    if profile == "strict":
        load_admission()  # refuse before reading, so a tooling gap is never a parse error
        import chiplet_format_io
        return chiplet_format_io.load(str(path))
    if str(VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(VENDOR_DIR))
    import chiplet_format_io
    return chiplet_format_io.load(str(path))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="assembly_admission",
        description="strict die-contract admission for a .chiplet assembly")
    parser.add_argument("--chiplet", required=True, help="the .chiplet assembly")
    parser.add_argument("--profile", choices=PROFILES, default="legacy",
                        help="legacy asks nothing and imports nothing; strict "
                             "enforces (default: %(default)s)")
    parser.add_argument("--base-dir", default=None, metavar="PATH",
                        help="directory the assembly's own references resolve "
                             "against (default: the .chiplet's directory)")
    parser.add_argument("--json", default=None, metavar="PATH",
                        help="write the machine-readable report here")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # As in pads_vs_pillars: the whole pipeline, report emission included, runs
    # inside this try so exit 1 is structurally reachable ONLY through the
    # deliberate return below. Python's default handler exits 1, which would be
    # indistinguishable from a refusal, so every other failure funnels to 2.
    try:
        chiplet = Path(args.chiplet)
        base_dir = Path(args.base_dir) if args.base_dir else chiplet.resolve().parent
        assembly = _load_assembly(chiplet, args.profile)
        report = admit_assembly(assembly, base_dir, profile=args.profile)
        report["chiplet"] = str(args.chiplet)
        report["base_dir"] = str(base_dir)

        for warning in report["warnings"]:
            print("WARNING: %s" % warning, file=sys.stderr)
        for entry in report["components"]:
            if entry["outcome"] == "refused":
                print("REFUSED [%s]: %s" % (entry["id"], entry["reason"]))
        census = report["census"]
        print("assembly_admission: profile=%s outcome=%s%s; %d component(s) seen, "
              "%d matched, %d admitted, %d refused, %d excluded, %d unclassified"
              % (report["profile"], report["outcome"],
                 " (%s)" % report["cause"] if report["cause"] else "",
                 census["components_seen"], census["matched"], census["admitted"],
                 census["refused"], census["excluded"], census["unclassified"]))
        if report["outcome"] == OUTCOME_UNDECIDABLE:
            for reason in report["reasons"]:
                print("UNDECIDABLE: %s" % reason, file=sys.stderr)

        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2) + "\n",
                                       encoding="utf-8")
            print("Report: %s" % args.json)

        return exit_code(report)
    except AdmissionUnavailable as exc:
        print("assembly_admission: error: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print("assembly_admission: error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print("assembly_admission: error: unexpected failure: %s" % exc,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
