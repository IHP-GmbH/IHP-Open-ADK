# SPDX-License-Identifier: Apache-2.0
"""Schema-parity test for the adapter-id contract.

The ``*.adapter`` fields of ``chiplet.schema.json`` (``interposer.adapter`` and
``interconnect.adapter``) are the id pattern PLUS a ``.drc`` negative
(``"not": {"pattern": "\\.drc$"}``). Every implementation of that contract runs
ONE shared oracle so the proposition being checked is "rejects everything the
schema rejects", exercised on the same set -- not "two regexes look alike"
(META-3). The oracle is chiplet-spec ``conformance/fixtures/adapter_id_cases.json``,
vendored byte-identical beside this file (see ``adapter_id_cases.PROVENANCE.txt``);
chiplet-spec's own conformance test proves that file against the schema itself.

This test is RED if ``validate_adapter_id`` ever degrades to ``validate_id``:
the reject list carries valid-id-shaped names ending in ``.drc``
(``intm4tm2.drc``, ``evil.drc``, ...) that the bare id rule accepts and only the
adapter field's ``.drc`` negative refuses (``test_oracle_covers_the_drc_negative``
guards that this discriminating property stays in the oracle).
"""
import json
import re
from pathlib import Path

import pytest

import adk_registry

_ORACLE = Path(__file__).resolve().parent / "fixtures" / "adapter_id_cases.json"
_CASES = json.loads(_ORACLE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", _CASES["accept"])
def test_schema_accept_cases_pass(value):
    """Every id the schema accepts, validate_adapter_id returns unchanged."""
    assert adk_registry.validate_adapter_id(value, "interposer adapter") == value


@pytest.mark.parametrize("value", _CASES["reject"])
def test_schema_reject_cases_raise(value):
    """Every value the schema rejects, validate_adapter_id refuses (exit 1)."""
    with pytest.raises(adk_registry.IdLookupError):
        adk_registry.validate_adapter_id(value, "interposer adapter")


def test_oracle_covers_the_drc_negative():
    """Guard the discriminating property: at least one reject case is a valid id
    SHAPE that only the ``.drc`` negative refuses. Without such a case the parity
    test could no longer tell validate_adapter_id from validate_id, and the
    parity would be vacuous."""
    negative_only = [c for c in _CASES["reject"]
                     if re.match(adk_registry.ID_PATTERN, c) and c.endswith(".drc")]
    assert negative_only, (
        "oracle has no valid-id-shaped .drc case; the .drc negative is no longer "
        "under test")


def test_both_axes_share_one_contract():
    """The negative is the adapter FIELD contract, identical on both axes; a
    .drc-suffixed value is refused whether it is named interposer or
    interconnect (the schema puts the same pair on both fields)."""
    for namespace in ("interposer adapter", "interconnect adapter"):
        with pytest.raises(adk_registry.IdLookupError):
            adk_registry.validate_adapter_id("evil.drc", namespace)
