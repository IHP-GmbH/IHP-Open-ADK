# SPDX-License-Identifier: Apache-2.0
"""Repo guard: no test may hand a PATH to an adapter flag or resolver.

The id-only closure is only as good as the suite that exercises it. A migration
that quietly keeps a path form alive -- one test still passing an absolute .drc,
one helper still building `search_dir / name` -- would leave the capability in
place while every other test looks green. This module scans the suite's own
source for that shape.

Scanned: every ``tests/*.py`` except the security acceptance module (whose whole
job is to feed paths and watch them bounce) and this file (whose patterns
contain the very literals it hunts for). Within a scanned file, a resolver call
sitting under an open ``pytest.raises`` is an assertion that the path is
refused, not a use of the capability, so it is allowed.
"""

import re
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
SELF = Path(__file__).name
ACCEPTANCE = "test_adapter_id_only.py"
EXCLUDED = {SELF, ACCEPTANCE}

_ADAPTER_FLAG = r'--(?:interposer|interconnect)-adapter'

# An adapter flag followed by its value in an argv list. The value must be a
# plain string literal; a str(...) call there is a path being stringified.
_FLAG_LITERAL = re.compile(_ADAPTER_FLAG + r'"\s*,\s*(?P<q>["\'])(?P<val>.*?)(?P=q)')
_FLAG_STR_CALL = re.compile(_ADAPTER_FLAG + r'"\s*,\s*str\s*\(')

# A resolver called with a literal first argument.
_RESOLVER_LITERAL = re.compile(
    r'\bresolve_(?:interconnect_)?adapter(?:_path)?\s*\(\s*(?P<q>["\'])(?P<val>.*?)(?P=q)')


#: A match sitting under an open ``pytest.raises`` is an assertion that the
#: path is REFUSED, which is the opposite of using the capability. Allowed, and
#: narrow: the window is the match's own line plus the two above it, which is
#: as far as a ``with pytest.raises(...)`` header can be from its body.
_RAISES_WINDOW = 2


def _looks_like_a_path(value: str) -> bool:
    """The id shape forbids '/' outright, and a '.drc' suffix is a filename."""
    return "/" in value or "\\" in value or value.endswith(".drc")


def _is_a_refusal_assertion(source: str, offset: int) -> bool:
    lines = source[:offset].splitlines()
    return any("pytest.raises" in line for line in lines[-(_RAISES_WINDOW + 1):])


def _offenders_in(source: str):
    offenders = []

    for m in _FLAG_LITERAL.finditer(source):
        if _looks_like_a_path(m.group("val")):
            offenders.append(f"adapter flag given path literal {m.group('val')!r}")
    for m in _FLAG_STR_CALL.finditer(source):
        offenders.append("adapter flag given a str(...) value (a stringified path)")
    for m in _RESOLVER_LITERAL.finditer(source):
        if (_looks_like_a_path(m.group("val"))
                and not _is_a_refusal_assertion(source, m.start())):
            offenders.append(f"resolver called with path literal {m.group('val')!r}")

    return offenders


def _scanned_files():
    return sorted(p for p in TEST_DIR.glob("*.py") if p.name not in EXCLUDED)


def test_the_guard_actually_scans_something():
    """A glob that matched nothing would make every check below vacuous."""
    names = {p.name for p in _scanned_files()}
    assert len(names) >= 5, names
    assert "test_drc_regression.py" in names
    assert "test_dru_generator.py" in names
    assert not (names & EXCLUDED)


def test_the_guard_detects_the_shapes_it_claims_to(tmp_path):
    """The detector is exercised against a planted offender, so a regex that
    stopped matching cannot leave the whole guard silently green."""
    planted = tmp_path / "test_planted_offender.py"
    planted.write_text(
        'cmd = ["--interposer-adapter", "/abs/evil.drc"]\n'
        'cmd2 = ["--interconnect-adapter", str(some_path)]\n'
        'resolve_adapter_path("../interconnect/ihp_cupillar")\n'
        'resolve_adapter("intm4tm2")          # legitimate: an id\n'
    )
    assert len(_offenders_in(planted.read_text())) == 3


@pytest.mark.parametrize("path", _scanned_files(), ids=lambda p: p.name)
def test_no_path_valued_adapter_argument(path):
    offenders = _offenders_in(path.read_text())
    assert not offenders, (
        f"{path.name} routes a PATH into adapter selection, which is id-only:\n  "
        + "\n  ".join(offenders)
        + "\nRegister the deck as an id in a registry layer and name the id. "
        f"Refusal cases belong in {ACCEPTANCE}."
    )
