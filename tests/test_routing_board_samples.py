"""Check that the routing gallery ships the matched board snapshots and rules."""

import hashlib
from pathlib import Path


def test_routing_gallery_frozen_boards_and_settings():
    gallery = Path(__file__).resolve().parents[1] / "samples" / "routing"
    boards = {
        "gateway": ("placed", "routed"),
        "relay": ("placed", "routed"),
        "setpoint": ("placed", "routed", "last-open"),
    }
    actual = {}
    for case, snapshots in boards.items():
        directory = gallery / case
        rules = (directory / "placed.kicad_pro").read_bytes()
        for stem in snapshots:
            board = directory / f"{stem}.kicad_pcb"
            project = board.with_suffix(".kicad_pro")
            assert project.read_bytes() == rules, project
            for path in (board, project):
                actual[path.relative_to(gallery).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    expected = {}
    for line in (gallery / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, filename = line.split("  ")
        expected[filename] = digest
    assert actual == expected
