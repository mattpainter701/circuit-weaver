#!/bin/sh
# Run in the pinned LibreLane image. Arguments are a new build directory and TT support-tools checkout.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out=$(realpath -m "${1:?supply a new build directory}")
tt=$(realpath "${2:?supply the pinned tt-support-tools checkout}")
mkdir "$out"
tech=ihp-sg13g2
version=22f43352dd8219f9007eb659e422e0d5fe28c5fb
export PDK_ROOT=${PDK_ROOT:-/pdk} QT_QPA_PLATFORM=offscreen
ciel enable --pdk-root "$PDK_ROOT" --pdk "$tech" "$version"
python3 "$here/prepare.py" "$out/src" "$tt/tech/$tech/def/tt_block_3x2_pgvdd.def"
models="$PDK_ROOT/$tech/libs.ref/sg13g2_sram/verilog"
python3 "$here/../verify_isa.py" --ihp-models "$models"
cd "$out/src"
python3 "$here/pin_vectors.py" .
set -- "$models/RM_IHPSG13_1P_core_behavioral_bm_bist.v" "$models/RM_IHPSG13_1P_256x16_c2_bm_bist.v"
iverilog -g2012 -DFUNCTIONAL -DWEFTLINE_IHP_SRAM -s pin_tb -o pins "$@" slot16.sv weftline_rom.v tt_um_weftline16.v "$here/pin_tb.sv"
vvp pins
# Magic also reports on geometry internal to the delivered SRAM. Replace its
# count-only checker with a coordinate- and rule-exact vendor control below.
# The full integrated Magic and KLayout decks both run.
librelane --manual-pdk --pdk-root "$PDK_ROOT" --pdk "$tech" --jobs 4 --hide-progress-bar \
    --skip Checker.MagicDRC --run-tag weftline config.json
final="$out/src/runs/weftline/final"
python3 - "$final/metrics.json" <<'PY'
import json
import sys
from pathlib import Path

metrics = json.loads(Path(sys.argv[1]).read_text())
if metrics["design__max_fanout_violation__count"]:
    raise SystemExit("post-route fanout violations remain")
PY
python3 "$here/macro_drc.py" "$out/src/runs/weftline" "$PDK_ROOT/$tech"
top=tt_um_weftline16
iverilog -g2012 -DFUNCTIONAL -s pin_tb -o pins "$@" "$PDK_ROOT/$tech/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v" "$final/nl/$top.nl.v" "$here/pin_tb.sv"
vvp pins
python3 "$here/export.py" "$out/src/runs/weftline" "$out/tt_submission"
