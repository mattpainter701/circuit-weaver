"""Local KiCad footprint library lookup.

Circuit Weaver stores footprint bindings as KiCad library references such as
``Package_TO_SOT_SMD:SC-70-5``.  This module checks whether those references
exist in the user's local KiCad footprint libraries before a design is treated
as fabrication-ready.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from urllib.parse import quote

_KICAD_FOOTPRINT_PATHS = {
    "Windows": [
        Path("C:/Program Files/KiCad/10.0/share/kicad/footprints"),
        Path("C:/Program Files/KiCad/9.0/share/kicad/footprints"),
        Path("C:/Program Files/KiCad/8.0/share/kicad/footprints"),
    ],
    "Linux": [
        Path("/usr/share/kicad/footprints"),
        Path("/usr/local/share/kicad/footprints"),
    ],
    "Darwin": [
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
        Path("/usr/local/share/kicad/footprints"),
    ],
}


def _parse_kicad_sexp(text: str) -> list:
    """Parse table data only; never evaluate KiCad expressions."""
    tokens: list[object] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if char == ";":
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline + 1
            continue
        if char in "()":
            tokens.append(char)
            position += 1
            continue
        if char == '"':
            position += 1
            value: list[str] = []
            while position < len(text) and text[position] != '"':
                if text[position] == "\\":
                    if position + 1 >= len(text):
                        raise ValueError("unterminated escape in KiCad footprint library table")
                    if text[position + 1] in {'"', "\\"}:
                        value.append(text[position + 1])
                        position += 2
                    else:
                        # Preserve unknown escapes; this keeps unsafe path separators visible.
                        value.append("\\")
                        position += 1
                    continue
                value.append(text[position])
                position += 1
            if position >= len(text):
                raise ValueError("unterminated quoted string in KiCad footprint library table")
            position += 1
            tokens.append(("quoted", "".join(value)))
            continue
        atom_start = position
        while position < len(text) and not text[position].isspace() and text[position] not in '();"':
            position += 1
        if atom_start == position:
            raise ValueError("invalid token in KiCad footprint library table")
        tokens.append(text[atom_start:position])
    if not tokens:
        raise ValueError("empty KiCad footprint library table")

    stack: list[list] = []
    root: list | None = None
    for token in tokens:
        if token == "(":
            node: list = []
            if stack:
                stack[-1].append(node)
            elif root is None:
                root = node
            else:
                raise ValueError("multiple top-level expressions in KiCad footprint library table")
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("unbalanced KiCad footprint library table")
            stack.pop()
        else:
            if not stack:
                raise ValueError("atom outside KiCad footprint library table")
            stack[-1].append(token[1] if isinstance(token, tuple) else token)
    if stack or root is None:
        raise ValueError("unbalanced KiCad footprint library table")
    return root


def _direct_string(node: list, key: str) -> str | None:
    fields = [child for child in node if isinstance(child, list) and child and child[0] == key]
    if len(fields) > 1:
        raise ValueError(f"duplicate {key!r} field in KiCad footprint library table")
    if not fields:
        return None
    field = fields[0]
    if len(field) != 2 or not isinstance(field[1], str):
        raise ValueError(f"malformed {key!r} field in KiCad footprint library table")
    return field[1]


def _project_library_uri(project_root: Path, nickname: str) -> Path | None:
    table = project_root / "fp-lib-table"
    if not table.is_file():
        return None
    if table.stat().st_size > 1024 * 1024:
        raise ValueError("KiCad footprint library table exceeds 1 MiB")
    try:
        tree = _parse_kicad_sexp(table.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"could not read KiCad footprint library table: {exc}") from exc
    if not tree or tree[0] != "fp_lib_table":
        raise ValueError("invalid KiCad footprint library table root")
    matches = [x for x in tree[1:] if isinstance(x, list) and x and x[0] == "lib"
               and _direct_string(x, "name") == nickname]
    if len(matches) > 1:
        raise ValueError(f"duplicate footprint library nickname in project table: {nickname}")
    if not matches:
        return None
    entry = matches[0]
    kind, uri = _direct_string(entry, "type"), _direct_string(entry, "uri")
    if kind != "KiCad":
        raise ValueError(f"unsupported project footprint library type for {nickname}: {kind!r}")
    prefix = "${KIPRJMOD}/"
    if not uri or not uri.startswith(prefix):
        raise ValueError(f"unsupported project footprint library URI for {nickname}: {uri!r}")
    relative = uri[len(prefix):]
    parts = relative.split("/")
    if (not relative or "\\" in relative or any(x in {"", ".", ".."} for x in parts)
            or ":" in relative or "${" in relative):
        raise ValueError(f"unsafe project footprint library URI for {nickname}: {uri!r}")
    root = project_root.resolve(strict=True)
    library = root.joinpath(*parts).resolve(strict=False)
    if not library.is_relative_to(root):
        raise ValueError(f"project footprint library escapes project root: {uri!r}")
    return library


@dataclass(frozen=True)
class FootprintGeometry:
    """Real library geometry used by authoritative placement/handoff."""

    width_mm: float
    height_mm: float
    source: str
    content_hash: str
    evidence_kind: str = "footprint_lib"
    confidence: str = "verified"
    center_x_mm: float = 0.0
    center_y_mm: float = 0.0


class KiCadFootprintLibrary:
    """Resolve KiCad footprint references against local `.pretty` libraries."""

    def __init__(self, root: str | Path | None = None, *, project_root: str | Path | None = None) -> None:
        self._roots = self._default_roots(root)
        self._project_root = Path(project_root).resolve() if project_root is not None else None
        self._geometry_cache: dict[tuple[str, str, str], FootprintGeometry] = {}

    @staticmethod
    def _default_roots(root: str | Path | None) -> list[Path]:
        roots: list[Path] = []
        if root:
            roots.append(Path(root))
        for name in (
            "KICAD_FOOTPRINT_DIR",
            "KICAD10_FOOTPRINT_DIR",
            "KICAD9_FOOTPRINT_DIR",
            "KICAD8_FOOTPRINT_DIR",
        ):
            env = os.environ.get(name)
            if env:
                roots.append(Path(env))
        roots.extend(_KICAD_FOOTPRINT_PATHS.get(platform.system(), []))

        existing: list[Path] = []
        seen: set[str] = set()
        for candidate in roots:
            if not candidate.is_dir():
                continue
            key = os.path.normcase(os.path.abspath(candidate))
            if key not in seen:
                seen.add(key)
                existing.append(candidate)
        return existing

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def footprint_exists(self, footprint: str) -> bool:
        """Return True when ``Lib:Footprint`` exists locally."""
        if not footprint or ":" not in footprint:
            return False
        lib, name = footprint.split(":", 1)
        if not lib or not name:
            return False
        return self.resolve(footprint) is not None

    def resolve(self, footprint: str) -> Path | None:
        """Return the exact local `.kicad_mod` path for ``Lib:Footprint``."""

        if not footprint or ":" not in footprint:
            return None
        lib, name = footprint.split(":", 1)
        if not lib or not name or any(token in lib + name for token in ("/", "\\", "..")):
            return None
        if self._project_root is not None:
            project_library = _project_library_uri(self._project_root, lib)
            if project_library is not None:
                candidate = (project_library / f"{name}.kicad_mod").resolve(strict=False)
                if not candidate.is_relative_to(self._project_root):
                    raise ValueError("project footprint file escapes project root")
                return candidate if candidate.is_file() else None
        rel = Path(f"{lib}.pretty") / f"{name}.kicad_mod"
        for root in self._roots:
            candidate = root / rel
            if candidate.is_file():
                return candidate
        return None

    def read(self, footprint: str) -> str:
        """Read a resolved footprint or fail closed instead of using a placeholder."""

        path = self.resolve(footprint)
        if path is None:
            raise FileNotFoundError(f"unresolved KiCad footprint: {footprint}")
        return path.read_text(encoding="utf-8")

    def snapshot(self, footprints: list[str] | tuple[str, ...]) -> dict[str, str]:
        """Return deterministic content hashes without leaking machine-local paths."""

        return {
            footprint: hashlib.sha256(self.read(footprint).encode("utf-8")).hexdigest()
            for footprint in sorted(set(footprints))
        }

    def geometry(self, footprint: str) -> FootprintGeometry:
        """Measure geometry cached by resolution state, resolved path, and content hash."""

        path = self.resolve(footprint)
        if path is None:
            geometry = _heuristic_geometry(footprint)
            cache_key = (footprint, "<unresolved>", geometry.content_hash)
            cached = self._geometry_cache.get(cache_key)
            if cached is not None:
                return cached
            self._geometry_cache[cache_key] = geometry
            return geometry
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cache_key = (footprint, str(path.resolve()), digest)
        cached = self._geometry_cache.get(cache_key)
        if cached is not None:
            return cached
        courtyard_points: list[tuple[float, float]] = []
        for block in _iter_blocks(text, ("fp_line", "fp_rect", "fp_arc", "fp_poly")):
            if "F.CrtYd" not in block and "B.CrtYd" not in block:
                continue
            courtyard_points.extend(_coordinate_pairs(block))
        if courtyard_points:
            width, height = _bounds(courtyard_points)
            xs, ys = zip(*courtyard_points)
            geometry = FootprintGeometry(width, height, "courtyard", digest,
                                         center_x_mm=(min(xs) + max(xs)) / 2,
                                         center_y_mm=(min(ys) + max(ys)) / 2)
            self._geometry_cache[cache_key] = geometry
            return geometry

        pad_points: list[tuple[float, float]] = []
        for block in _iter_blocks(text, ("pad",)):
            at = re.search(r"\(at\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", block)
            size = re.search(r"\(size\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)", block)
            if not at or not size:
                continue
            x, y = float(at.group(1)), float(at.group(2))
            half_w, half_h = float(size.group(1)) / 2.0, float(size.group(2)) / 2.0
            pad_points.extend(((x - half_w, y - half_h), (x + half_w, y + half_h)))
        if not pad_points:
            raise ValueError(f"footprint {footprint} has neither courtyard geometry nor measurable pads")
        width, height = _bounds(pad_points)
        xs, ys = zip(*pad_points)
        geometry = FootprintGeometry(width, height, "pads", digest,
                                     center_x_mm=(min(xs) + max(xs)) / 2,
                                     center_y_mm=(min(ys) + max(ys)) / 2)
        self._geometry_cache[cache_key] = geometry
        return geometry

    def find(self, query: str) -> list[str]:
        """Find local footprint refs whose name contains ``query``."""
        q = query.lower().strip()
        if not q:
            return []
        matches: list[str] = []
        for root in self._roots:
            for pretty in sorted(root.glob("*.pretty")):
                for mod in sorted(pretty.glob("*.kicad_mod")):
                    if q in mod.stem.lower():
                        matches.append(f"{pretty.stem}:{mod.stem}")
        return matches


def _iter_blocks(text: str, keywords: tuple[str, ...]):
    token = re.compile(r"\((?:" + "|".join(re.escape(item) for item in keywords) + r")(?=[\s(])")
    for match in token.finditer(text):
        depth = 0
        quoted = False
        escaped = False
        for index in range(match.start(), len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield text[match.start() : index + 1]
                    break


def _coordinate_pairs(block: str) -> list[tuple[float, float]]:
    return [
        (float(match.group(1)), float(match.group(2)))
        for match in re.finditer(
            r"\((?:start|end|center|mid|xy)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)",
            block,
        )
    ]


def _bounds(points: list[tuple[float, float]]) -> tuple[float, float]:
    xs = [item[0] for item in points]
    ys = [item[1] for item in points]
    return max(xs) - min(xs), max(ys) - min(ys)


def _heuristic_geometry(footprint: str) -> FootprintGeometry:
    """Return an explicitly low-confidence estimate when no library file exists."""

    normalized = str(footprint or "").upper().replace("_", "-")
    dimensions = [
        (float(match.group(1)), float(match.group(2)))
        for match in re.finditer(
            r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)MM\b",
            normalized,
        )
    ]
    if dimensions:
        width, height = max(dimensions, key=lambda item: item[0] * item[1])
    else:
        metric = re.search(r"(?<!\d)(\d{4})METRIC\b", normalized)
        if metric:
            code = metric.group(1)
            width = max(int(code[:2]) / 10.0 + 1.0, 2.0)
            height = max(int(code[2:]) / 10.0 + 1.0, 2.0)
        else:
            width, height = 5.0, 5.0
    digest = hashlib.sha256(f"heuristic:{footprint}:{width}:{height}".encode()).hexdigest()
    return FootprintGeometry(
        width,
        height,
        "heuristic",
        digest,
        evidence_kind="heuristic",
        confidence="heuristic",
    )


def official_kicad_footprint_url(footprint: str) -> str:
    """Return the official KiCad footprint-library browser URL for a ref."""
    if not footprint or ":" not in footprint:
        return "https://gitlab.com/kicad/libraries/kicad-footprints"
    lib, name = footprint.split(":", 1)
    lib_path = quote(f"{lib}.pretty", safe="")
    mod_path = quote(f"{name}.kicad_mod", safe="")
    return f"https://gitlab.com/kicad/libraries/kicad-footprints/-/blob/master/{lib_path}/{mod_path}"


def curated_footprint_alternatives(mpn: str, fp_lib: KiCadFootprintLibrary | None = None) -> list[dict[str, str]]:
    """Return curated alternates that use available KiCad-backed footprints."""
    key = (mpn or "").strip().upper()
    if not key:
        return []
    try:
        raw = resources.files("circuit_weaver.ic_data").joinpath("alternates.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, ModuleNotFoundError):
        return []

    candidates = data.get(key) or data.get(mpn) or []
    if not isinstance(candidates, list):
        return []

    out: list[dict[str, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        footprint = str(candidate.get("footprint", "")).strip()
        if fp_lib is not None and fp_lib.roots and not fp_lib.footprint_exists(footprint):
            continue
        out.append({
            "mpn": str(candidate.get("mpn", "")).strip(),
            "footprint": footprint,
            "reason": str(candidate.get("reason", "")).strip(),
        })
    return out


def custom_footprint_suggestion(mpn: str, footprint: str, fp_lib: KiCadFootprintLibrary) -> str:
    """Build an actionable suggestion for a missing/custom footprint."""
    alts = curated_footprint_alternatives(mpn, fp_lib)
    if alts:
        rendered = "; ".join(
            f"{alt['mpn']} ({alt['footprint']}) — {alt['reason']}"
            for alt in alts
            if alt.get("mpn") and alt.get("footprint")
        )
        if rendered:
            return (
                "Safer KiCad-footprint-backed alternatives available: "
                f"{rendered}. Keep {mpn} only in advanced/custom-footprint mode after importing and verifying "
                "a trusted vendor/project .pretty footprint."
            )
    return (
        f"No curated KiCad-footprint-backed alternate is known for {mpn or footprint}; "
        "treat this as advanced/custom-footprint mode and import/verify a trusted vendor or project .pretty "
        "footprint before fabrication."
    )
