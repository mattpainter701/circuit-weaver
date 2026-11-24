#!/bin/sh
set -eu
cd "$(dirname "$0")"
simulation=$(mktemp)
trap 'rm -f "$simulation"' EXIT
iverilog -g2012 -Wall -s test_slot16 -o "$simulation" slot16.sv test_slot16.sv
vvp "$simulation"
python3 verify_isa.py
python3 test_weftline.py
# Newer frontends emit $check cells, which the SAT backend consumes after
# chformal -lower. Older frontends emit $assert directly.
lower_checks=''
if yosys -Q -T -p 'help chformal' | grep -q -- '-lower'; then
    lower_checks='chformal -lower;'
fi
yosys -Q -T -p "read_verilog -formal -sv -DSLOT16_FORMAL slot16.sv; prep -top slot16; $lower_checks memory_map; opt; select -assert-any t:\$assert; sat -verify -seq 2 -prove-skip 1 -prove-asserts"
yosys -Q -T -p 'read_verilog -sv slot16.sv; synth -top slot16; check -assert; stat'
