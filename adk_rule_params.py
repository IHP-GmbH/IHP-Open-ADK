# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""adk_rule_params -- the ADK assembly rule values and adapter overrides.

Reads ``config/rule_params.json`` and the numeric overrides an interposer or
interconnect adapter declares (``drc_rules['NAME'] = NUMBER``,
``interconnect_rules['NAME'] = NUMBER``). The deck evaluates an adapter as
Ruby, so the parsers read one literal form, above any other code, and refuse
every other line that names a rule table rather than skip it
(:class:`UnreadOverrideError`).

Standard library only, and it imports nothing from the ADK, so a consumer can
load it by file from a pinned ADK root
(``importlib.util.spec_from_file_location``) and run that tree's code without
putting the ADK on ``sys.path``. Adapter ids resolve through
``adk_registry.py``, which loads the same way; this module takes a path.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict, List, Tuple, Union

__all__ = [
    "RULE_PARAMS_RELPATH",
    "UnreadOverrideError",
    "load_rule_params",
    "parse_adapter_overrides",
    "parse_interconnect_overrides",
    "read_rule_table",
]

RULE_PARAMS_RELPATH = Path("config") / "rule_params.json"


_TABLES = ("drc_rules", "interconnect_rules")


def _override_re(table: str) -> "re.Pattern[str]":
    # One line, `<table>['NAME'] = NUMBER`, with an optional trailing comment.
    # NAME follows the config schema (^[A-Z][A-Za-z0-9_]*$). NUMBER is an
    # ASCII decimal literal whose integer part has no leading zero, since Ruby
    # reads 0250 as octal 168 (0 and 0.5 are fine). Anything else on a line
    # naming a table (2.5e2, 1_000, a trailing `;`, a second statement,
    # store/merge!, a computed value) does not match and is refused, never
    # truncated to a partial number.
    # The key's quotes must match, and \s is ASCII whitespace only, as Ruby's.
    return re.compile(
        rf"^\s*{table}\[\s*(?P<q>['\"])(?P<name>[A-Z][A-Za-z0-9_]*)(?P=q)\s*\]\s*=\s*"
        r"(?P<value>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)\s*(?:#.*)?$",
        re.ASCII,
    )


_TABLE_PATTERNS = {table: _override_re(table) for table in _TABLES}

# A line led by one of these continues the previous expression in Ruby (`.`
# and `&.` always; `&&`, `||`, `and`, `or` in newer Ruby), so after a literal
# override it could change the value without naming the table. `and`/`or`
# bind looser than `=` and cannot; they are refused anyway, as cheap caution.
_CONTINUATION_RE = re.compile(r"^\s*(?:&?\.|&&|\|\||(?:and|or)\b)")

# Ruby's whitespace; Python's str.strip() also strips NBSP and similar.
_RUBY_SPACE = " \t\n\v\f\r"


class UnreadOverrideError(ValueError):
    """Adapter lines the parser cannot read, so the applied value is unknown.

    ``lines`` holds ``(line number, text)`` for each refused line and
    ``reasons`` the reason for each, in the same order; the message gives
    both.

    An override is read in one form only, a literal
    ``<table>['NAME'] = NUMBER`` alone on its line, optionally followed by a
    comment naming neither table, and above any other code: only blank
    lines, comment lines and other overrides come before it. Any other line
    that names a rule table is refused, comment lines and an override's
    trailing comment included. So no override the deck applies is missed,
    unless the table is reached without its name (an alias, metaprogramming
    such as a reopened ``Hash#[]``, a file the adapter loads); adapters are
    vetted code, selected by registry id only.

    With no code above an override, nothing in the file can turn it into
    text, a branch or part of a larger statement (``a,`` before it makes
    Ruby store nil), so a read override is the value Ruby stores. A name
    assigned twice and a line continuing the last override are refused, so
    it is also the value Ruby keeps. Once a line other than an override is
    seen, every override below it is refused, so one stray line can list
    several.

    All of this holds per file. The deck evals the interposer adapter, the
    interconnect adapter and its own parts as one source, so a construct
    left open at the end of one file (an unclosed ``=begin``) could hide the
    overrides of the next. No parser checks that across files; the only
    cross-file check is that :func:`parse_interconnect_overrides` refuses
    ``drc_rules`` lines in an interconnect adapter.
    """

    def __init__(self, path: Path, table: str,
                 refused: List[Tuple[int, str, str]]):
        self.path = path
        self.table = table
        self.lines = [(n, text) for n, text, _ in refused]
        self.reasons = [why for _, _, why in refused]
        shown = "".join(f"\n  line {n} ({why}): {text}" for n, text, why in refused)
        super().__init__(
            f"adapter {path}: the ADK reads {table} overrides only as one "
            f"literal `{table}['NAME'] = NUMBER` per name, above any other "
            f"code, so the value the deck applies is unknown on:{shown}"
        )


def read_rule_table(path: Union[str, Path]) -> Dict[str, float]:
    """Return the ``rules`` table of a rule_params.json file."""
    source = Path(path)
    data = json.loads(source.read_text())
    if "rules" not in data:
        raise ValueError(f"{source}: missing required 'rules' key.")
    rules = {name: float(value) for name, value in data["rules"].items()}
    # json.loads accepts NaN, Infinity and 1e999 (inf): no rule value.
    bad = sorted(name for name, value in rules.items() if not math.isfinite(value))
    if bad:
        raise ValueError(f"{source}: rules that are not finite: {', '.join(bad)}.")
    return rules


def load_rule_params(adk_root: Union[str, Path]) -> Dict[str, float]:
    """Return the ``rules`` table of ``<adk_root>/config/rule_params.json``."""
    return read_rule_table(Path(adk_root) / RULE_PARAMS_RELPATH)


def _parse_overrides(adapter_path: Path, table: str) -> Dict[str, float]:
    """Return the ``<table>['NAME'] = NUMBER`` overrides of a .drc adapter.

    Raises :class:`UnreadOverrideError` on the lines it lists. Lines split on
    newlines only, as Ruby's do: the file is read without newline
    translation, so a lone ``\r`` stays inside its line.
    """
    pattern = _TABLE_PATTERNS[table]
    overrides: Dict[str, float] = {}
    refused: List[Tuple[int, str, str]] = []
    below_code = prev_is_override = False
    with adapter_path.open(encoding="utf-8", newline="") as f:
        text = f.read()
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip(_RUBY_SPACE)
        if not stripped:
            continue
        comment = stripped.startswith("#")
        m = None if comment else pattern.match(line)
        rest = line[m.end("value"):] if m else line
        if any(t in rest for t in _TABLES):
            why = ("comment names a rule table" if comment or m
                   else f"not a literal {table} override")
        elif m and below_code:
            why = "below other code"
        elif m and m.group("name") in overrides:
            why = "assigned twice"
        elif prev_is_override and _CONTINUATION_RE.match(line):
            why = "continues an override"
        else:
            why = None
        if why:
            refused.append((number, stripped, why))
        elif m:
            # -0 and -0.0 read as 0; Ruby keeps -0.0, equal in value.
            overrides[m.group("name")] = float(m.group("value")) + 0.0
        if not comment:
            prev_is_override = m is not None
            below_code = below_code or m is None
    if refused:
        raise UnreadOverrideError(adapter_path, table, refused)
    return overrides


def parse_adapter_overrides(adapter_path: Union[str, Path]) -> Dict[str, float]:
    """Return the drc_rules[] overrides of a .drc interposer adapter."""
    return _parse_overrides(Path(adapter_path), "drc_rules")


def parse_interconnect_overrides(adapter_path: Union[str, Path]) -> Dict[str, float]:
    """Return the interconnect_rules[] overrides of an interconnect adapter."""
    return _parse_overrides(Path(adapter_path), "interconnect_rules")
