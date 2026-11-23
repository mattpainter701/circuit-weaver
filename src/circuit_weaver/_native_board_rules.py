"""Read board-level native constraints needed by geometry-sensitive helpers."""

from __future__ import annotations

import math
import re
from numbers import Integral
from pathlib import Path

_TOKEN = re.compile(r'\s+|;[^\r\n]*|#[^\r\n]*|"(?:\\.|[^"\\])*"|[()]|[^\s();#]+')


def _sexpr_tokens(text: str) -> list[str]:
    """Tokenize KiCad's rule s-expression while skipping comments/whitespace."""
    tokens = []
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token.isspace() or token.startswith((";", "#")):
            continue
        tokens.append(token)
    return tokens


def _parse_sexpr(tokens: list[str]) -> list[object]:
    roots: list[object] = []
    stack: list[list[object]] = []
    for token in tokens:
        if token == "(":
            node: list[object] = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("KiCad custom rules contain an unmatched closing parenthesis")
            stack.pop()
        elif stack:
            stack[-1].append(token)
        else:
            roots.append(token)
    if stack:
        raise ValueError("KiCad custom rules contain an unclosed expression")
    return roots


def _contains_custom_edge_clearance(path: Path) -> bool:
    """Detect actual edge_clearance constraints without matching comments/strings."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("KiCad custom rule file cannot be read safely") from exc
    if size > 2 * 1024 * 1024:
        raise ValueError("KiCad custom rule file exceeds the 2 MiB inspection size limit")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("KiCad custom rule file cannot be read safely") from exc
    tree = _parse_sexpr(_sexpr_tokens(text))
    pending = list(tree)
    while pending:
        node = pending.pop()
        if not isinstance(node, list):
            continue
        if len(node) >= 2 and node[0] == "constraint" and node[1] == "edge_clearance":
            return True
        pending.extend(node)
    return False


def native_edge_clearance(board, pcbnew) -> dict[str, int | float | str]:
    """Resolve KiCad's native board-level copper-to-edge minimum.

    The project-backed `m_CopperEdgeClearance` setting is available from
    `BOARD_DESIGN_SETTINGS` in supported KiCad versions. Custom `.kicad_dru`
    edge-clearance constraints can raise the effective limit conditionally;
    this helper currently rejects those rather than routing against a weaker
    approximation. Other custom-rule constraint types remain supported.
    """
    try:
        settings = board.GetDesignSettings()
        raw = getattr(settings, "m_CopperEdgeClearance")
    except (AttributeError, RuntimeError, TypeError) as exc:
        raise ValueError("native copper-to-edge clearance setting is unavailable") from exc
    if isinstance(raw, bool) or not isinstance(raw, Integral):
        raise ValueError("native copper-to-edge clearance must be a nonnegative native IU integer")
    try:
        iu = int(raw)
        raw_number = float(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("native copper-to-edge clearance must be a nonnegative native IU integer") from exc
    if not math.isfinite(raw_number) or raw_number != iu or iu < 0:
        raise ValueError("native copper-to-edge clearance must be a nonnegative native IU integer")
    try:
        mm = float(pcbnew.ToMM(iu))
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("native copper-to-edge clearance could not be converted to millimeters") from exc
    if not math.isfinite(mm) or mm < 0:
        raise ValueError("native copper-to-edge clearance converted to an invalid value")

    try:
        filename = str(board.GetFileName()).strip()
        if not filename:
            raise ValueError("empty native filename")
        board_path = Path(filename).resolve()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("native board path is unavailable for custom-rule inspection") from exc
    if not board_path.name:
        raise ValueError("native board path is unavailable for custom-rule inspection")
    rules_path = board_path.with_suffix(".kicad_dru")
    if rules_path.is_file() and _contains_custom_edge_clearance(rules_path):
        raise ValueError("custom KiCad edge_clearance rules are unsupported by native routing geometry")

    return {
        "value_iu": iu,
        "value_mm": mm,
        "value_um": mm * 1000.0,
        "source": "BOARD_DESIGN_SETTINGS.m_CopperEdgeClearance",
    }


def native_edge_clearance_metadata(board, pcbnew) -> dict[str, int | float | str]:
    """Best-effort inspection metadata; routing callers must use strict reader above."""
    try:
        return {"status": "ok", **native_edge_clearance(board, pcbnew)}
    except ValueError as exc:
        return {"status": "unsupported", "message": str(exc)}
