#!/bin/sh
# Portable checks: reference model, weight logic, row-level and pin-level
# equivalence in RTL and on the synthesized gate netlist.
# Set HEDDLE_LIBERTY to a standard-cell Liberty file to also report mapped area.
set -eu
cd "$(dirname "$0")"
here=$(pwd)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
python3 test_heddle.py
python3 build_rtl.py "$build" >/dev/null
python3 vectors.py "$build"
cd "$build"
iverilog -g2012 -Wall -I. -s rom_tb -o rom heddle_model.v "$here/rom_tb.sv"
vvp -n rom > rom.txt
python3 "$here/vectors.py" . --rom rom.txt
iverilog -g2012 -Wall -I. -s core_tb -o core heddle_model.v "$here/heddle_core.v" "$here/core_tb.sv"
vvp -n core
iverilog -g2012 -Wall -I. -s pin_tb -o pins heddle_model.v "$here/heddle_core.v" "$here/tt_um_heddle.v" "$here/pin_tb.sv"
vvp -n pins
sources="read_verilog -I. heddle_model.v $here/heddle_core.v $here/tt_um_heddle.v"
yosys -Q -T -q -p "$sources; synth -top tt_um_heddle; flatten; opt_clean; check -assert; tee -o stat.txt stat; write_verilog -noattr gates.v"
grep -E 'Number of cells' stat.txt | tail -n 1
iverilog -g2012 -I. -s pin_tb -o gates gates.v "$here/pin_tb.sv"
echo "gate netlist:"
vvp -n gates
if [ -n "${HEDDLE_LIBERTY:-}" ]; then
    yosys -Q -T -q -p "$sources; synth -top tt_um_heddle; dfflibmap -liberty $HEDDLE_LIBERTY; abc -liberty $HEDDLE_LIBERTY; flatten; opt_clean; tee -o area.txt stat -liberty $HEDDLE_LIBERTY"
    grep -E 'Chip area for top module' area.txt
fi
