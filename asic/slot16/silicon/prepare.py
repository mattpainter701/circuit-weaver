"""Stage the complete design against the pinned Tiny Tapeout IHP tile."""
import argparse
import hashlib
import shutil
from pathlib import Path

from build_rom import CORE, build

HERE = Path(__file__).resolve().parent


def prepare(output: Path, template: Path) -> None:
    template_data = template.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(template_data).hexdigest() != "d8dba38597c27a8e0864b5fa14f4181193119f9b9295925a653d94298fc101d0":
        raise ValueError("unexpected 3x2 IHP tile template")
    build(output)
    (output / "tile.def").write_bytes(template_data)
    for source in [CORE / "slot16.sv", HERE / "tt_um_weftline16.v", HERE / "config.json",
                   HERE / "pdn.tcl", HERE / "ihp_sram.vh", HERE / "impl.sdc", HERE / "signoff.sdc"]:
        shutil.copyfile(source, output / source.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("template", type=Path)
    args = parser.parse_args()
    prepare(args.output, args.template)
