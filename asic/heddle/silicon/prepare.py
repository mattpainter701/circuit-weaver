"""Stage the complete design against the pinned Tiny Tapeout IHP tile."""
import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent
sys.path.insert(0, str(CORE))

from build_rtl import build  # noqa: E402
from heddle import Model  # noqa: E402

# Pinned Tiny Tapeout IHP tile templates: SHA-256 of the DEF with LF line endings, and the die area.
TILES = {
    "3x2": ("d8dba38597c27a8e0864b5fa14f4181193119f9b9295925a653d94298fc101d0", "0 0 636.96 313.74"),
    "4x2": ("c40206826251e6fb719ba7a3b0315eb4290832066c1da4dd5c29fa12c8fd1a8d", "0 0 854.40 313.74"),
    "6x2": ("be7c516f58e455421eee3ae477f2b8579a584a9477b1a87d8e360e5f8aa49346", "0 0 1289.28 313.74"),
}


def info_tiles() -> str:
    """Tile size declared in info.yaml, the single place a submission states it."""
    match = re.search(r'^\s*tiles:\s*"([0-9]+x[0-9]+)"', (HERE / "info.yaml").read_text(), re.MULTILINE)
    if not match:
        raise ValueError("info.yaml does not declare a tile size")
    return match.group(1)


def prepare(output: Path, templates: Path, tiles: str) -> None:
    digest, die_area = TILES[tiles]
    template_data = (templates / f"tt_block_{tiles}_pgvdd.def").read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(template_data).hexdigest() != digest:
        raise ValueError(f"unexpected {tiles} IHP tile template")
    output.mkdir(parents=True)
    build(Model.load(), output)
    (output / "tile.def").write_bytes(template_data)
    config = json.loads((HERE / "config.json").read_text())
    config["DIE_AREA"] = die_area
    (output / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    for source in [CORE / "heddle_core.v", CORE / "tt_um_heddle.v", HERE / "impl.sdc", HERE / "signoff.sdc"]:
        shutil.copyfile(source, output / source.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("templates", type=Path, help="tech/ihp-sg13g2/def of the pinned tt-support-tools")
    parser.add_argument("--tiles", choices=sorted(TILES), default=None, help="default: the size in info.yaml")
    args = parser.parse_args()
    prepare(args.output, args.templates, args.tiles or info_tiles())
