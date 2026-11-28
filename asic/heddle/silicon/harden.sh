#!/bin/sh
# Run in the pinned LibreLane image. Arguments are a new build directory and TT support-tools checkout.
# A PDK already present under PDK_ROOT is used as is; otherwise the pinned revision is fetched.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out=$(realpath -m "${1:?supply a new build directory}")
tt=$(realpath "${2:?supply the pinned tt-support-tools checkout}")
mkdir "$out"
tech=ihp-sg13g2
version=22f43352dd8219f9007eb659e422e0d5fe28c5fb
export PDK_ROOT=${PDK_ROOT:-/pdk} QT_QPA_PLATFORM=offscreen
if [ ! -d "$PDK_ROOT/$tech" ]; then
    ciel enable --pdk-root "$PDK_ROOT" --pdk "$tech" "$version"
fi
# TILES overrides the size declared in info.yaml, for floorplan experiments only.
python3 "$here/prepare.py" "$out/src" "$tt/tech/$tech/def" ${TILES:+--tiles "$TILES"}
cd "$out/src"
python3 "$here/../vectors.py" .
iverilog -g2012 -I. -s pin_tb -o pins heddle_model.v heddle_core.v tt_um_heddle.v "$here/../pin_tb.sv"
vvp -n pins
librelane --manual-pdk --pdk-root "$PDK_ROOT" --pdk "$tech" --jobs "${JOBS:-4}" --hide-progress-bar \
    --run-tag heddle config.json
final="$out/src/runs/heddle/final"
python3 - "$final/metrics.json" <<'PY'
import json
import sys
from pathlib import Path

metrics = json.loads(Path(sys.argv[1]).read_text())
if metrics.get("design__max_fanout_violation__count"):
    raise SystemExit("post-route fanout violations remain")
PY
# Replay the full host scenario on the routed gate netlist with the PDK cell models.
top=tt_um_heddle
cells="$PDK_ROOT/$tech/libs.ref/sg13g2_stdcell/verilog"
iverilog -g2012 -DFUNCTIONAL -I. -s pin_tb -o pins "$cells/sg13g2_udp.v" "$cells/sg13g2_stdcell.v" \
    "$final/nl/$top.nl.v" "$here/../pin_tb.sv"
vvp -n pins
python3 "$here/export.py" "$out/src/runs/heddle" "$out/tt_submission"
