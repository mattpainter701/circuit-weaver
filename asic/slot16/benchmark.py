"""Run the same two-stream peripheral workload on Slot16 and pinned PicoRV32."""
import argparse
import hashlib
import json
import math
import struct
import subprocess
import tempfile
from pathlib import Path

from asm16 import assemble, write_image

HERE = Path(__file__).resolve().parent
PICO_SHA256 = "0836050971b3c6cdd28ac3b1e5719a67fb645161912bef1e472e63995ceb0622"
SAMPLES, PERIOD, DEADLINE = 64, 128, 64


def build(directory: Path, pico: Path) -> dict:
    banks = [assemble((HERE / "firmware" / f"{name}.s16").read_text()) for name in ("filter", "telemetry")]
    write_image([w for bank in banks for w in bank + [0] * (256 - len(bank))], directory / "slot.hex")
    if hashlib.sha256(pico.read_bytes()).hexdigest() != PICO_SHA256:
        raise ValueError("picorv32.v differs from the documented upstream revision")
    tools = "riscv64-unknown-elf-"
    subprocess.run([tools + "as", "-march=rv32i", "-mabi=ilp32", "-o", "rv.o", HERE / "firmware/controller.S"],
                   cwd=directory, check=True)
    subprocess.run([tools + "ld", "-m", "elf32lriscv", "--no-relax", "-Ttext=0", "-o", "rv.elf", "rv.o"],
                   cwd=directory, check=True)
    subprocess.run([tools + "objcopy", "-O", "binary", "-j", ".text", "rv.elf", "rv.bin"],
                   cwd=directory, check=True)
    program = (directory / "rv.bin").read_bytes()
    if len(program) > 1024:
        raise ValueError("RV32 program exceeds ROM")
    (directory / "rv.hex").write_text("".join(f"{w:08x}\n" for (w,) in struct.iter_unpack("<I", program.ljust(1024, b"\0"))))
    for name, sources in (("Slot16", [HERE / "slot16.sv"]), ("PicoRV32", ["-DPICORV32", pico])):
        subprocess.run(["iverilog", "-g2012", "-s", "controller_tb", "-o", name, *sources, HERE / "controller_tb.sv"],
                       cwd=directory, check=True)
    return {"Slot16_bank_bytes": [2 * len(bank) for bank in banks], "PicoRV32_bytes": len(program)}


def run_trial(directory: Path, core: str, burst: int, phase: int, align: int, stall: int) -> dict:
    trace = subprocess.check_output(["vvp", core, f"+burst={burst}", f"+phase={phase}", f"+align={align}",
                                     f"+stall={stall}"], cwd=directory, text=True)
    end = 64 + align + SAMPLES * PERIOD
    delays = []
    telemetry = in_window = 0
    done = False
    for line in trace.splitlines():
        fields = line.split()
        if fields[:1] == ["DONE"]:
            done = fields[1:] == [str(SAMPLES), str(burst * 16)]
        if fields[:1] != ["OUT"]:
            continue
        _, edge, lane, payload = fields
        cycle, actual = int(edge), int(payload, 16)
        if lane == "0":
            i = len(delays)
            previous = 0 if i == 0 else 0xE001 + (i - 1) * 997
            expected = (0xE001 + i * 997 + previous) & 65535
            delays.append(cycle - (64 + align + i * PERIOD))
        else:
            expected = ((0xB13D + telemetry * 37) & 65535) ^ 85
            telemetry += 1
            in_window += cycle < end
        if actual != expected:
            raise AssertionError(f"{core} edge {cycle} lane {lane}: {actual} != {expected}")
    if not done or len(delays) != SAMPLES or min(delays) < 0 or telemetry != 16 * burst:
        raise AssertionError(f"{core}: incomplete or misordered workload")
    if stall == 2 and in_window:
        raise AssertionError(f"{core}: transferred through a stalled telemetry sink")
    return dict(core=core, burst=burst, phase=phase, align=align, stall=stall, latency=delays,
                misses=sum(delay > DEADLINE for delay in delays), telemetry_in_window=in_window,
                telemetry_after_window=telemetry - in_window)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("picorv32", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    scenarios = [(0, 0, align, 0) for align in (0, 1)]
    scenarios += [(burst, phase, align, stall) for burst in (1, 8, 32) for phase in (0, 17, 63)
                  for align in (0, 1) for stall in (0, 1, 2)]
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        sizes = build(directory, args.picorv32.resolve())
        rows = [run_trial(directory, core, *case) for core in ("Slot16", "PicoRV32") for case in scenarios]
    idle = {row["align"]: row["latency"] for row in rows if row["core"] == "Slot16" and row["burst"] == 0}
    for row in rows:
        if row["core"] == "Slot16" and row["latency"] != idle[row["align"]]:
            raise AssertionError(f"telemetry changed the control timing in {row}")
    print(sizes)
    for core in ("Slot16", "PicoRV32"):
        for stall in (0, 1, 2):
            group = [row for row in rows if row["core"] == core and row["stall"] == stall]
            delays = sorted(v for row in group for v in row["latency"])
            print(f"{core} stall={stall} samples={len(delays)} min={delays[0]} "
                  f"p50={delays[math.ceil(len(delays)*0.50)-1]} p95={delays[math.ceil(len(delays)*0.95)-1]} "
                  f"max={delays[-1]} misses={sum(row['misses'] for row in group)} "
                  f"telemetry={sum(row['telemetry_in_window'] for row in group)} "
                  f"drain={sum(row['telemetry_after_window'] for row in group)}")
    if args.json:
        args.json.write_text(json.dumps(dict(deadline=DEADLINE, period=PERIOD, sizes=sizes, trials=rows), indent=2))
    if any(row["misses"] for row in rows if row["core"] == "Slot16"):
        raise SystemExit("Slot16 missed the declared control deadline")


if __name__ == "__main__":
    main()
