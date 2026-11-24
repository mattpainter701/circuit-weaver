"""Compare every Magic marker with a control containing only the original IHP SRAM."""
import argparse
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

import klayout.db as db
import librelane

MACRO = "RM_IHPSG13_1P_256x16_c2_bm_bist"
TOP = "tt_um_weftline16"
BOX = re.compile(r"\s*(-?[\d.]+)um (-?[\d.]+)um (-?[\d.]+)um (-?[\d.]+)um")


def markers(path):
    counts = Counter()
    category = None
    declared = None
    for line in path.read_text().splitlines()[1:]:
        box = BOX.fullmatch(line)
        if box and category:
            counts[(category, *box.groups())] += 1
        elif line.startswith("[INFO] COUNT: "):
            declared = int(line.split()[-1])
        elif line and not line.startswith(("-", "[INFO]")):
            category = line
    if declared is None or counts.total() != declared:
        raise ValueError(f"incomplete Magic DRC report: {path}")
    return counts


def compare(run, pdk):
    step, = run.glob("*-magic-drc")
    dest = run / "sram-control"
    dest.mkdir(exist_ok=True)
    (dest / "reports").mkdir(exist_ok=True)
    tile = db.Layout()
    tile.read(str(run / "final/gds" / f"{TOP}.gds"))
    top = tile.top_cell()
    if top.name != TOP:
        raise ValueError("unexpected top cell")
    instance, = [inst for inst in top.each_inst() if inst.cell.name == MACRO]
    if instance.is_regular_array() or instance.dcplx_trans.mag != 1:
        raise ValueError("unexpected SRAM array or magnification")

    # Check the byte-original vendor GDS in its native coordinates. Transform
    # its marker boxes to chip coordinates only after DRC.
    config = dest / "control.tcl"
    config.write_text('source $::env(INTEGRATED_ENV)\n'
                      'set ::env(CURRENT_GDS) $::env(VENDOR_GDS)\n'
                      'set ::env(DESIGN_NAME) $::env(VENDOR_CELL)\n')
    scripts = Path(librelane.__file__).parent / "scripts/magic"
    env = dict(os.environ, STEP_DIR=str(dest), INTEGRATED_ENV=str(step / "_env.tcl"),
               VENDOR_CELL=MACRO, VENDOR_GDS=str(pdk / "libs.ref/sg13g2_sram/gds" / f"{MACRO}.gds"),
               _TCL_ENV_IN=str(config), _MAGIC_SCRIPT=str(scripts / "drc.tcl"))
    with (dest / "magic.log").open("w") as log:
        subprocess.run(["magic", "-dnull", "-noconsole", "-rcfile",
                        pdk / "libs.tech/magic/ihp-sg13g2.magicrc", scripts / "wrapper.tcl"],
                       cwd=dest, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    actual = markers(step / "reports/drc.magic.rpt")
    baseline = Counter()
    for (rule, *coords), count in markers(dest / "reports/drc.magic.rpt").items():
        box = instance.dcplx_trans * db.DBox(*map(float, coords))
        placed = (box.left, box.bottom, box.right, box.top)
        baseline[(rule, *(f"{coordinate:.3f}" for coordinate in placed))] += count
    extra = actual - baseline
    summary = {"integrated": actual.total(), "vendor_control": baseline.total(),
               "unmatched": extra.total(), "first_unmatched": list(extra)[:10]}
    (dest / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(summary)
    if extra:
        raise SystemExit("Magic found geometry errors absent from the vendor control")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("pdk", type=Path)
    args = parser.parse_args()
    compare(args.run.resolve(), args.pdk.resolve())
