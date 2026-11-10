# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Regenerate the chiplet2dbx golden files from the unit fixture.

Run after an intentional output change, review the diff, commit together:

    python tests/regolden_chiplet2dbx.py
"""
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent / "openroad"))

import chiplet2dbx  # noqa: E402
from test_chiplet2dbx import (  # noqa: E402
    GOLDEN_DIR, base_assembly, bump_assembly, d1_pins, export_views_fixture)


def main() -> int:
    GOLDEN_DIR.mkdir(exist_ok=True)
    assembly = base_assembly()
    pins = {"D1": d1_pins()}
    targets = {
        "chiplet2dbx_unit_demo.3dbv.golden": chiplet2dbx.render_3dbv(assembly),
        "chiplet2dbx_unit_demo.3dbx.golden":
            chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv"),
        "chiplet2dbx_tech.lef.golden": chiplet2dbx.render_tech_lef(1000),
        "chiplet2dbx_unit_demo_bumps.3dbv.golden":
            chiplet2dbx.render_3dbv(bump_assembly(), die_pins=pins),
        "chiplet2dbx_unit_demo.bmap.golden":
            chiplet2dbx.render_bmap(bump_assembly(), "DIE_A__bump25",
                                    die_pins=pins),
        "chiplet2dbx_tech_route.lef.golden":
            chiplet2dbx.render_tech_lef(1000, "BUMP_ATTACH"),
    }
    # The record fixture exports through files; its outputs name the record
    # files relative to the out dir, so the temporary root does not show.
    with tempfile.TemporaryDirectory() as tmp:
        export_views_fixture(Path(tmp))
        for name in ("views_demo.3dbv", "views_demo.3dbx", "DIE_F.bmap"):
            targets["chiplet2dbx_%s.golden" % name] = (
                Path(tmp) / "out" / name).read_text(encoding="utf-8")
    for name, text in targets.items():
        (GOLDEN_DIR / name).write_text(text, encoding="utf-8")
        print("wrote %s" % (GOLDEN_DIR / name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
