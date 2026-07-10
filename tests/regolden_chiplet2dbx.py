# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Regenerate the chiplet2dbx golden files from the unit fixture.

Run after an intentional output change, review the diff, commit together:

    python tests/regolden_chiplet2dbx.py
"""
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent / "openroad"))

import chiplet2dbx  # noqa: E402
from test_chiplet2dbx import GOLDEN_DIR, base_assembly  # noqa: E402


def main() -> int:
    GOLDEN_DIR.mkdir(exist_ok=True)
    assembly = base_assembly()
    targets = {
        "chiplet2dbx_unit_demo.3dbv.golden": chiplet2dbx.render_3dbv(assembly),
        "chiplet2dbx_unit_demo.3dbx.golden":
            chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv"),
        "chiplet2dbx_tech.lef.golden": chiplet2dbx.render_tech_lef(1000),
    }
    for name, text in targets.items():
        (GOLDEN_DIR / name).write_text(text, encoding="utf-8")
        print("wrote %s" % (GOLDEN_DIR / name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
