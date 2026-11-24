#!/bin/sh
set -eu
cd "$(dirname "$0")"
simulation=$(mktemp)
trap 'rm -f "$simulation"' EXIT
iverilog -g2012 -Wall -s test_slot16 -o "$simulation" slot16.sv test_slot16.sv
vvp "$simulation"
yosys -Q -T -p 'read_verilog -sv slot16.sv; synth -top slot16; check -assert; stat'
