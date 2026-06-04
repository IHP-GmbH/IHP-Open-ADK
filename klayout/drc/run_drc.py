"""
ADK assembly DRC runner.

Standalone CLI for the assembly DRC wrapper. Requires an interposer adapter
(shortname or absolute path) so the wrapper can resolve abstract inputs.
See docs/adapter_contract.md for the contract.
"""

import argparse
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from subprocess import check_call, CalledProcessError
from typing import List, Optional, Set, Union

import klayout.db


# ================================================================
# -------------------- REPORT UTILITIES --------------------------
# ================================================================


def get_rules_with_violations(results_database: Union[str, Path]) -> Set[str]:
    """Parse a KLayout RDB file and return rule names that have violations."""
    results_database = Path(results_database)
    if not results_database.is_file():
        logging.error(f"Results database not found: {results_database}")
        raise FileNotFoundError(f"No such file: {results_database}")

    try:
        tree = ET.parse(results_database)
        root = tree.getroot()
    except ET.ParseError as e:
        logging.error(f"Failed to parse results database: {results_database}")
        raise e

    violating_rules = set()
    for rule in root[7]:  # root[7] : List rules with violations
        violating_rules.add(f"{rule[1].text}".replace("'", ""))

    return violating_rules


# ================================================================
# -------------------- LAYOUT UTILITIES --------------------------
# ================================================================


def get_top_cell_names(gds_path: str) -> List[str]:
    """Get top cell names from a GDS file."""
    layout = klayout.db.Layout()
    layout.read(gds_path)
    return [t.name for t in layout.top_cells()]


def check_klayout_version():
    """Check that KLayout >= 0.29.11 is available."""
    try:
        klayout_version_output = os.popen("klayout -b -v").read().strip()
    except Exception as e:
        logging.error(f"Error while checking KLayout version: {e}")
        exit(1)

    if not klayout_version_output:
        logging.error("KLayout not found. Make sure it is installed and in PATH.")
        exit(1)

    version_str = klayout_version_output.split()[-1]
    version_parts = version_str.split(".")

    try:
        major = int(version_parts[0])
        minor = int(version_parts[1]) if len(version_parts) > 1 else 0
        patch = int(version_parts[2]) if len(version_parts) > 2 else 0
    except ValueError:
        logging.error(f"Failed to parse KLayout version: '{klayout_version_output}'")
        exit(1)

    if (major, minor, patch) < (0, 29, 11):
        logging.error(f"Minimum KLayout version is 0.29.11. Found: {version_str}")
        exit(1)

    logging.info(f"KLayout version: {version_str}")


def check_layout_path(layout_path: str) -> str:
    """Validate layout file exists and is GDS/OAS format. Returns absolute path."""
    path = Path(layout_path)

    if not path.is_file():
        logging.error(f"Layout file '{layout_path}' does not exist.")
        exit(1)

    if not layout_path.lower().endswith((".gds", ".gds.gz", ".gds2", ".gds2.gz", ".oas")):
        logging.error(f"Layout '{layout_path}' is not GDS or OAS format.")
        exit(1)

    return str(path.resolve())


def get_run_top_cell_name(topcell_arg: str, layout_path: str) -> str:
    """Resolve top cell name: use provided value or auto-detect from layout."""
    if topcell_arg:
        return topcell_arg

    top_cells = get_top_cell_names(layout_path)
    if len(top_cells) > 1:
        logging.error("Layout has multiple top cells. Specify one with --topcell.")
        exit(1)
    elif not top_cells:
        logging.error("No top cell found in layout.")
        exit(1)
    return top_cells[0]


# ================================================================
# -------------------- ADAPTER RESOLUTION ------------------------
# ================================================================


_ADK_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_DIR = _ADK_ROOT / "pdk_adapters" / "interposer"
_INTERCONNECT_ADAPTER_DIR = _ADK_ROOT / "pdk_adapters" / "interconnect"


def resolve_adapter(name_or_path: str) -> str:
    """Resolve a --interposer-adapter argument to an absolute .drc path.

    Accepts either a shortname (resolved against pdk_adapters/interposer/) or
    an explicit path to a .drc file.
    """
    candidate = Path(name_or_path)
    if candidate.suffix == ".drc" and candidate.is_file():
        return str(candidate.resolve())

    shortname = name_or_path[:-4] if name_or_path.endswith(".drc") else name_or_path
    adapter_path = _ADAPTER_DIR / f"{shortname}.drc"
    if adapter_path.is_file():
        return str(adapter_path.resolve())

    logging.error(
        f"Interposer adapter not found: '{name_or_path}'. "
        f"Looked for the literal path and for '{adapter_path}'."
    )
    exit(1)


def resolve_interconnect_adapter(name_or_path: str) -> str:
    """Resolve a --interconnect-adapter argument to an absolute .drc path.

    Parallel to resolve_adapter() but for the optional interconnect axis.
    Accepts a shortname (resolved against pdk_adapters/interconnect/) or an
    explicit path to a .drc file.
    """
    candidate = Path(name_or_path)
    if candidate.suffix == ".drc" and candidate.is_file():
        return str(candidate.resolve())

    shortname = name_or_path[:-4] if name_or_path.endswith(".drc") else name_or_path
    adapter_path = _INTERCONNECT_ADAPTER_DIR / f"{shortname}.drc"
    if adapter_path.is_file():
        return str(adapter_path.resolve())

    logging.error(
        f"Interconnect adapter not found: '{name_or_path}'. "
        f"Looked for the literal path and for '{adapter_path}'."
    )
    exit(1)


def resolve_manifest_path(layout_path: str, manifest_arg: Optional[str],
                          legacy_exchange0: bool) -> Optional[Path]:
    """Resolve the boundary manifest path (fail-loud).

    In manifest mode (default) an explicit ``--manifest`` is used if given,
    otherwise ``<layout-stem>.boundaries.json`` next to the GDS is
    auto-discovered. A missing manifest is a hard error: the runner never
    checks an assembly with no boundary source, which would pass vacuously.
    In legacy mode the manifest is not used (boundaries come from the GDS
    exchange0 fab layer).
    """
    if legacy_exchange0:
        return None

    if manifest_arg:
        manifest_path = Path(manifest_arg).resolve()
    else:
        layout = Path(layout_path)
        manifest_path = layout.with_name(layout.stem + ".boundaries.json")

    if not manifest_path.is_file():
        logging.error(
            "Boundary manifest not found: %s\n"
            "The assembly DRC needs the chiplet boundaries. Either:\n"
            "  - provide the <gds-stem>.boundaries.json sidecar (emitted by "
            "hyp_to_gds / blackbox_chiplet), or\n"
            "  - pass --legacy-exchange0 to check a pre-migration GDS that "
            "carries boundaries on the exchange0 fab layer.",
            manifest_path,
        )
        exit(1)
    return manifest_path


# ================================================================
# -------------------- DRC EXECUTION -----------------------------
# ================================================================


def run_assembly_drc(layout_path: str, adapter_path: str, topcell: str,
                     run_dir: Path, threads: int = 4,
                     run_mode: str = "tiling",
                     report_path: Optional[Path] = None,
                     manifest_path: Optional[Path] = None,
                     legacy_exchange0: bool = False,
                     interconnect_adapter_path: Optional[str] = None,
                     interconnect_methods_path: Optional[Path] = None) -> Path:
    """Run the ADK assembly DRC wrapper via klayout -b.

    Chiplet boundaries come from the producer's boundary manifest
    (``manifest_path``) unless ``legacy_exchange0`` is set, in which case the
    deck reads the historical exchange0 fab layer from the GDS.

    Returns the path to the generated .lyrdb report.
    """
    drc_script = str(_ADK_ROOT / "klayout" / "drc" / "adk_assembly.drc")
    layout_stem = Path(layout_path).stem
    if report_path is None:
        report_path = run_dir / f"{layout_stem}_{topcell}_assembly.lyrdb"

    cmd = (
        f"klayout -b -r '{drc_script}'"
        f" -rd input='{layout_path}'"
        f" -rd adapter='{adapter_path}'"
        f" -rd report='{report_path}'"
        f" -rd topcell='{topcell}'"
        f" -rd threads={threads}"
        f" -rd run_mode='{run_mode}'"
        f" -rd legacy_exchange0={'true' if legacy_exchange0 else 'false'}"
    )
    if manifest_path is not None:
        cmd += f" -rd manifest='{manifest_path}'"
    if interconnect_adapter_path is not None:
        cmd += f" -rd interconnect_adapter='{interconnect_adapter_path}'"
    if interconnect_methods_path is not None:
        cmd += f" -rd interconnect_methods='{interconnect_methods_path}'"

    logging.info(
        f"Running assembly DRC on {Path(layout_path).name} "
        f"(topcell: {topcell}, adapter: {Path(adapter_path).name})"
    )
    logging.debug(f"Command: {cmd}")

    try:
        check_call(cmd, shell=True)
    except CalledProcessError as e:
        logging.error(f"Assembly DRC failed with exit code {e.returncode}")
        raise

    return Path(report_path)


def check_drc_results(report_path: Path) -> Set[str]:
    """Parse the report and log a pass/fail banner. Returns the rule set."""
    if not report_path.is_file():
        logging.error(f"Result database not generated: {report_path}")
        exit(1)

    violating_rules = get_rules_with_violations(report_path)

    if violating_rules:
        logging.warning("=" * 70)
        logging.warning("ADK DRC FAILED: Violations detected")
        logging.warning("=" * 70)
        logging.warning(f"Violated rules: {sorted(violating_rules)}")
    else:
        logging.info("=" * 70)
        logging.info("ADK DRC PASSED: No violations detected")
        logging.info("=" * 70)

    logging.info(f"Report: {report_path}")
    return violating_rules


# ================================================================
# -------------------- CLI & MAIN --------------------------------
# ================================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run ADK assembly DRC checks via KLayout",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --path design.gds --interposer-adapter intm4tm2
  %(prog)s --path design.gds --interposer-adapter /abs/path/to/custom.drc
  %(prog)s --path design.gds --interposer-adapter intm4tm2 --interconnect-adapter ihp_cupillar
""",
    )

    parser.add_argument(
        "--path", type=str, required=True,
        help="Path to the input GDS/OAS file.",
    )
    parser.add_argument(
        "--interposer-adapter", type=str, required=True,
        help="Interposer adapter: a shortname (resolved against "
             "pdk_adapters/interposer/<name>.drc) or an absolute path to a "
             ".drc file.",
    )
    parser.add_argument(
        "--interconnect-adapter", type=str, default=None,
        help="Optional interconnect adapter: a shortname (resolved against "
             "pdk_adapters/interconnect/<name>.drc) or an absolute path. Adds "
             "the bump-to-bump pitch/spacing axis (IXN rules). Omit for "
             "interposer-only checking (identical to before this axis existed).",
    )
    parser.add_argument(
        "--interconnect-methods", type=str, default=None,
        help="Optional per-method interconnect file "
             "(<gds-stem>.ixn_methods.json, emitted by the exporter from the "
             ".chiplet's per-die connections + the interconnect PDK manifest). "
             "Scopes the IXN checks per method: each method's pitch/spacing "
             "runs on the attachment pads under ITS dies' boundaries, plus a "
             "conservative cross-method spacing check. Requires the boundary "
             "manifest. Without it the IXN axis stays assembly-global.",
    )
    parser.add_argument(
        "--topcell", type=str, default=None,
        help="Top-level cell name (auto-detected if omitted).",
    )
    parser.add_argument(
        "--run_dir", type=str, default=None,
        help="Output directory for reports (default: timestamped subdir in cwd).",
    )
    parser.add_argument(
        "--threads", type=int, default=4,
        help="Threads per KLayout invocation (default: 4).",
    )
    parser.add_argument(
        "--run_mode", type=str, choices=["tiling", "deep", "flat"],
        default="tiling",
        help="KLayout execution mode (default: tiling).",
    )
    parser.add_argument(
        "--report", type=str, default=None,
        help="Explicit report path (overrides default naming).",
    )
    parser.add_argument(
        "--manifest", type=str, default=None,
        help="Boundary manifest sidecar (<gds-stem>.boundaries.json). "
             "Auto-discovered next to --path if omitted. Holds the chiplet "
             "boundaries the assembly DRC checks (PDK-agnostic; not a fab layer).",
    )
    parser.add_argument(
        "--legacy-exchange0", action="store_true",
        help="Compat mode: read chiplet boundaries from the historical "
             "exchange0 fab layer in the GDS instead of a manifest "
             "(for pre-migration assemblies).",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    now_str = datetime.now(timezone.utc).strftime("adk_drc_run_%Y_%m_%d_%H_%M_%S")
    if args.run_dir in ["pwd", "", None]:
        run_dir = Path.cwd().resolve() / now_str
    else:
        run_dir = Path(args.run_dir).resolve()
    os.makedirs(run_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(run_dir / f"{now_str}.log"),
            logging.StreamHandler(),
        ],
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%d-%b-%Y %H:%M:%S",
    )

    time_start = time.time()

    check_klayout_version()
    layout_path = check_layout_path(args.path)
    topcell = get_run_top_cell_name(args.topcell, layout_path)
    adapter_path = resolve_adapter(args.interposer_adapter)
    interconnect_adapter_path = (
        resolve_interconnect_adapter(args.interconnect_adapter)
        if args.interconnect_adapter else None
    )
    interconnect_methods_path = None
    if args.interconnect_methods:
        interconnect_methods_path = Path(args.interconnect_methods).resolve()
        if not interconnect_methods_path.is_file():
            logging.error(
                "Interconnect methods file not found: %s",
                interconnect_methods_path,
            )
            exit(1)
    manifest_path = resolve_manifest_path(
        layout_path, args.manifest, args.legacy_exchange0
    )

    report_path = Path(args.report).resolve() if args.report else None
    report = run_assembly_drc(
        layout_path, adapter_path, topcell, run_dir,
        threads=args.threads, run_mode=args.run_mode,
        report_path=report_path,
        manifest_path=manifest_path,
        legacy_exchange0=args.legacy_exchange0,
        interconnect_adapter_path=interconnect_adapter_path,
        interconnect_methods_path=interconnect_methods_path,
    )
    violations = check_drc_results(report)

    elapsed = time.time() - time_start
    logging.info(f"Total DRC time: {elapsed:.2f}s")

    return 1 if violations else 0


if __name__ == "__main__":
    exit(main())
