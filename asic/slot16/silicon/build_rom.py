"""Generate the physical mask ROM and the four emulator bank images."""
import argparse
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
from asm16 import assemble, write_image
from weftline import scratch_test_banks


def build(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    source = CORE / "firmware"
    scratch0, scratch1 = scratch_test_banks()
    banks = {
        "encoder": assemble((source / "encoder.s16").read_text()),
        "diagnostics": assemble((source / "encoder_diagnostics.s16").read_text()),
        "scratch0": scratch0,
        "scratch1": scratch1,
    }
    cases = []
    for index, (name, words) in enumerate(banks.items()):
        write_image(words, output / f"{name}.hex")
        for pc, word in enumerate(words):
            if word:  # Default instruction is HALT.
                cases.append(f"            10'h{index * 256 + pc:03x}: word = 16'h{word:04x};")
    lines = ["module weftline_rom(input wire [9:0] addr, output reg [15:0] word);",
             "    always @* begin", "        case (addr)", *cases,
             "            default: word = 16'h0000;", "        endcase", "    end", "endmodule"]
    (output / "weftline_rom.v").write_text("\n".join(lines) + "\n", encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
