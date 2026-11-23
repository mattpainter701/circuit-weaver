"""Conservative ordering canonicalization for private KiCad Specctra DSNs.

This module changes only records whose ordering is semantic-set order. It is
used on temporary native-board routing inputs; callers retain the original
DSN and design name for SES correlation and diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any

_LITERAL_QUOTE = re.compile(r'(\(string_quote\s+)"(?=\s*\))(?!\s*\)\s*"\s*\))', re.IGNORECASE)
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[^\s()]+')


def _scan_text(text: str) -> str:
    # In Specctra, (string_quote ") declares the quote delimiter; it is not
    # an opening quoted string. Same-width replacement preserves offsets.
    return _LITERAL_QUOTE.sub(r"\1q", text)


def _head(block: str) -> tuple[str, int]:
    match = re.match(r"\(\s*([^\s()]+)", block)
    if match is None or not block.endswith(")"):
        raise ValueError("malformed Specctra block")
    return match.group(1), match.end()


def _direct_blocks(block: str, *, allow_bare_after_children: bool = False) -> list[tuple[int, int, str, str]]:
    """Return direct child spans, preserving original text and rejecting imbalance."""
    keyword, head_end = _head(block)
    del keyword
    scan = _scan_text(block)
    rows: list[tuple[int, int, str, str]] = []
    index = head_end
    # Skip header atoms (for example an image name or the pcb design name)
    # before the first nested record. Thereafter only direct child blocks are
    # accepted; unknown bare payload is ambiguous and fails closed.
    while index < len(scan) - 1:
        while index < len(scan) - 1 and scan[index].isspace():
            index += 1
        if scan[index] == "(":
            break
        if scan[index] == ")":
            return rows
        match = _TOKEN.match(scan, index)
        if match is None:
            raise ValueError("malformed Specctra header atom")
        index = match.end()
    while index < len(scan) - 1:
        char = scan[index]
        if char.isspace():
            index += 1
            continue
        if char == ")":
            if scan[index + 1 :].strip():
                raise ValueError("unexpected data after outer Specctra block")
            return rows
        if char != "(":
            if allow_bare_after_children:
                match = _TOKEN.match(scan, index)
                if match is None:
                    raise ValueError("malformed Specctra atom")
                index = match.end()
                continue
            raise ValueError("unsupported bare token in Specctra block")
        start = index
        depth, quoted, escaped = 0, False, False
        while index < len(scan):
            current = scan[index]
            if escaped:
                escaped = False
            elif quoted and current == "\\":
                escaped = True
            elif current == '"':
                quoted = not quoted
            elif not quoted and current == "(":
                depth += 1
            elif not quoted and current == ")":
                depth -= 1
                if depth == 0:
                    index += 1
                    child = block[start:index]
                    child_head, _ = _head(child)
                    rows.append((start, index, child_head, child))
                    break
            index += 1
        else:
            raise ValueError("unbalanced nested Specctra block")
    if scan[index:].strip() not in {"", ")"}:
        raise ValueError("unbalanced outer Specctra block")
    return rows


def _root(text: str) -> tuple[str, int, int]:
    scan = _scan_text(text)
    matches = []
    quoted = escaped = False
    index = 0
    while index < len(scan):
        char = scan[index]
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            match = re.match(r"\(\s*pcb(?=\s|\))", scan[index:], re.IGNORECASE)
            if match:
                matches.append(index)
        index += 1
    if len(matches) != 1:
        raise ValueError("expected exactly one DSN pcb root")
    start = matches[0]
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(scan)):
        char = scan[index]
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0:
                root = text[start : index + 1]
                return root, start, index + 1
    raise ValueError("unbalanced DSN pcb root")


def _token_value(token: str) -> str:
    if token.startswith('"'):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            # Some KiCad Windows paths contain literal backslashes that are
            # not JSON escapes. Preserve all other characters and decode only
            # Specctra's escaped quote/backslash spellings.
            value = token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if not isinstance(value, str):
            raise ValueError("quoted Specctra token is not a string")
        return value
    return token


def _payload(block: str) -> str:
    _keyword, end = _head(block)
    return block[end:-1].strip()


def _payload_tokens(payload: str) -> list[str]:
    """Split on unquoted whitespace while retaining quoted pin fragments."""
    tokens: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for char in payload:
        if escaped:
            current.append(char)
            escaped = False
        elif quoted and char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if quoted or escaped:
        raise ValueError("unterminated quoted Specctra token")
    if current:
        tokens.append("".join(current))
    return tokens


def _leading_tokens(block: str) -> list[str]:
    payload = _payload(block)
    quoted = escaped = False
    for index, char in enumerate(payload):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            payload = payload[:index]
            break
    return [_token_value(token) for token in _payload_tokens(payload)]


def _atoms(block: str) -> list[str]:
    payload = _payload(block)
    if "(" in payload or ")" in payload:
        raise ValueError("nested payload where atom-only record was expected")
    return [_token_value(token) for token in _payload_tokens(payload)]


def _canonical_cycle(points: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    if len(points) < 3:
        raise ValueError("polygon has fewer than three vertices")
    return min(
        tuple(sequence[offset:] + sequence[:offset])
        for sequence in (points, list(reversed(points)))
        for offset in range(len(sequence))
    )


def _canonical_geometry_record(block: str) -> tuple[tuple[Any, ...], str]:
    kind, _ = _head(block)
    values = _atoms(block)
    if kind not in {"polygon", "path"} or len(values) < 6 or (len(values) - 2) % 2:
        raise ValueError(f"unsupported image geometry record {kind!r}")
    if not _is_number(values[1]) or not all(_is_number(value) for value in values[2:]):
        raise ValueError("image geometry contains a non-finite or malformed numeric token")
    prefix = tuple(values[:2])
    coordinates = list(zip(values[2::2], values[3::2], strict=True))
    if kind == "polygon":
        points = _canonical_cycle(coordinates)
    else:
        if len(coordinates) < 2:
            raise ValueError("path has fewer than two points")
        points = min(tuple(coordinates), tuple(reversed(coordinates)))
    key = (kind, prefix, points)
    rendered = f"({kind} " + " ".join((*prefix, *(part for point in points for part in point))) + ")"
    return key, rendered


def _image_geometry(block: str) -> tuple[tuple[Any, ...], list[str]]:
    geometry: list[tuple[tuple[Any, ...], str]] = []
    pins: list[tuple[tuple[Any, ...], str]] = []
    keepouts: list[tuple[str, str]] = []
    for _start, _end, kind, child in _direct_blocks(block):
        if kind == "outline":
            outlines = _direct_blocks(child)
            if len(outlines) != 1:
                raise ValueError("image outline must contain one geometry record")
            key, rendered = _canonical_geometry_record(outlines[0][3])
            geometry.append((key, f"(outline {rendered})"))
        elif kind == "pin":
            pin_children = _direct_blocks(child, allow_bare_after_children=True)
            if not pin_children:
                tokens = _payload_tokens(_payload(child))
                if len(tokens) != 4:
                    raise ValueError("unsupported image pin record")
                if not all(_is_number(_token_value(value)) for value in tokens[-2:]):
                    raise ValueError("image pin contains a non-finite or malformed coordinate")
                signature = (tuple(tokens),)
            elif len(pin_children) == 1 and pin_children[0][2] == "rotate":
                _start, _end, _kind, rotate = pin_children[0]
                head_end = _head(child)[1]
                prefix = _payload_tokens(child[head_end:_start])
                suffix = _payload_tokens(child[_end:-1])
                rotation = _leading_tokens(rotate)
                if len(prefix) != 1 or len(suffix) != 3 or len(rotation) != 1:
                    raise ValueError("unsupported rotated image pin record")
                if not _is_number(_token_value(rotation[0])) or not all(
                    _is_number(_token_value(value)) for value in suffix[-2:]
                ):
                    raise ValueError("rotated image pin contains malformed numeric data")
                signature = (tuple(prefix), tuple(rotation), tuple(suffix))
            else:
                raise ValueError("unknown image pin child; refusing canonicalization")
            pins.append((signature, child))
        elif kind == "keepout":
            # Keep the whole nested keepout record byte-for-byte; it is part of
            # the image geometry key, so aliases collapse only when identical.
            keepouts.append((child, child))
        else:
            raise ValueError(f"unknown image record {kind!r}; refusing canonicalization")
    if not geometry:
        raise ValueError("image has no outline records")
    geometry.sort(key=lambda item: repr(item[0]))
    pins.sort(key=lambda item: repr(item[0]))
    keepouts.sort(key=lambda item: item[0])
    key = (
        tuple(item[0] for item in geometry),
        tuple(item[0] for item in pins),
        tuple(item[0] for item in keepouts),
    )
    return key, [item[1] for item in geometry] + [item[1] for item in keepouts] + [item[1] for item in pins]


def _replace_spans(block: str, replacements: dict[tuple[int, int], str]) -> str:
    for (start, end), replacement in sorted(replacements.items(), reverse=True):
        block = block[:start] + replacement + block[end:]
    return block


def _canonicalize(text: str) -> tuple[str, dict[str, Any]]:
    root, root_start, root_end = _root(text)
    root_tokens = _leading_tokens(root)
    if len(root_tokens) < 1 or not root_tokens[0]:
        raise ValueError("DSN root has no design name")
    sections: dict[str, tuple[int, int, str]] = {}
    for start, end, kind, block in _direct_blocks(root):
        if kind in sections:
            raise ValueError(f"duplicate DSN section {kind!r}")
        sections[kind] = (start, end, block)
    if any(name not in sections for name in ("placement", "library", "network")):
        raise ValueError("DSN lacks placement, library, or network section")

    placement_block = sections["placement"][2]
    library_block = sections["library"][2]
    network_block = sections["network"][2]
    images: dict[str, tuple[tuple[Any, ...], list[str]]] = {}
    padstacks: list[str] = []
    seen_padstack = False
    for _start, _end, kind, child in _direct_blocks(library_block):
        if kind == "image":
            if seen_padstack:
                raise ValueError("library image appears after padstacks; refusing to reorder records")
            name = _leading_tokens(child)[0]
            if not name or name in images:
                raise ValueError("missing or duplicate image name")
            images[name] = _image_geometry(child)
        elif kind == "padstack":
            seen_padstack = True
            padstacks.append(child)
        else:
            raise ValueError(f"unknown library record {kind!r}; refusing canonicalization")
    if not images or not padstacks:
        raise ValueError("library lacks supported images or padstacks")

    placements: dict[str, dict[str, Any]] = {}
    refs_by_image: dict[str, set[str]] = defaultdict(set)
    component_rows: list[tuple[str, str, list[str]]] = []
    for _start, _end, kind, component in _direct_blocks(placement_block):
        if kind != "component":
            raise ValueError(f"unknown placement record {kind!r}; refusing canonicalization")
        component_tokens = _leading_tokens(component)
        if len(component_tokens) != 1 or component_tokens[0] not in images:
            raise ValueError("component image reference is missing or ambiguous")
        image_name = component_tokens[0]
        place_texts: list[tuple[str, str]] = []
        for _ps, _pe, place_kind, place in _direct_blocks(component):
            if place_kind != "place":
                raise ValueError(f"unknown component record {place_kind!r}; refusing canonicalization")
            tokens = _leading_tokens(place)
            if len(tokens) != 5:
                raise ValueError("malformed placement pose")
            ref = tokens[0]
            if not ref or ref in placements:
                raise ValueError("duplicate or missing placement reference")
            if not all(_is_number(token) for token in tokens[1:3] + tokens[4:5]):
                raise ValueError(f"invalid placement numeric token for {ref}")
            pose = tuple(tokens[1:])
            geometry = images[image_name][0]
            placements[ref] = {"pose": pose, "geometry": geometry, "image_name": image_name}
            refs_by_image[image_name].add(ref)
            place_texts.append((ref, place))
        if not place_texts:
            raise ValueError("component has no placement records")
        component_rows.append((image_name, component, [place for _, place in sorted(place_texts)]))

    # Reconcile duplicate image aliases by base name and exact, token-preserved
    # geometry, choosing deterministic aliases from sorted placed references.
    grouped: dict[tuple[str, tuple[Any, ...]], set[str]] = defaultdict(set)
    for ref, row in placements.items():
        grouped[(_alias_base(row["image_name"]), row["geometry"])].add(ref)
    by_base: dict[str, list[tuple[tuple[Any, ...], set[str]]]] = defaultdict(list)
    for (base, geometry), refs in grouped.items():
        by_base[base].append((geometry, refs))
    canonical_names: dict[tuple[str, tuple[Any, ...]], str] = {}
    for base, variants in by_base.items():
        for index, (geometry, refs) in enumerate(sorted(variants, key=lambda row: (min(row[1]), repr(row[0])))):
            canonical_names[(base, geometry)] = base if index == 0 else f"{base}::{index}"
    alias_map: dict[str, str] = {}
    image_blocks: dict[str, str] = {}
    for old_name, (geometry, records) in images.items():
        refs = refs_by_image.get(old_name)
        if not refs:
            raise ValueError(f"unreferenced library image {old_name!r}")
        identities = {(_alias_base(old_name), placements[ref]["geometry"]) for ref in refs}
        if len(identities) != 1:
            raise ValueError(f"ambiguous image alias {old_name!r}")
        base, image_key = next(iter(identities))
        new_name = canonical_names[(base, image_key)]
        alias_map[old_name] = new_name
        block = (
            "(image " + _render_token(new_name) + "\n" + "\n".join("      " + record for record in records) + "\n    )"
        )
        if new_name in image_blocks and image_blocks[new_name] != block:
            raise ValueError("duplicate canonical image alias has conflicting geometry")
        image_blocks[new_name] = block

    places_by_name: dict[str, list[str]] = defaultdict(list)
    for image, _component, places in component_rows:
        places_by_name[alias_map[image]].extend(places)
    canonical_placement = (
        "(placement\n"
        + "\n".join(
            "    (component "
            + _render_token(name)
            + "\n"
            + "\n".join("      " + place for place in sorted(places, key=lambda item: _leading_tokens(item)[0]))
            + "\n    )"
            for name, places in sorted(places_by_name.items())
        )
        + "\n  )"
    )
    canonical_library = (
        "(library\n"
        + "\n".join([image_blocks[name] for name in sorted(image_blocks)] + ["    " + block for block in padstacks])
        + "\n  )"
    )

    net_membership: dict[str, tuple[str, ...]] = {}
    net_blocks: list[tuple[str, str]] = []
    class_blocks: list[str] = []
    seen_class = False
    for _start, _end, kind, block in _direct_blocks(network_block):
        if kind == "class":
            seen_class = True
            class_blocks.append(block)
        elif kind == "net":
            if seen_class:
                raise ValueError("network net appears after class declarations; refusing to reorder records")
            values = _leading_tokens(block)
            if len(values) != 1 or not values[0] or values[0] in net_membership:
                raise ValueError("missing, malformed, or duplicate net declaration")
            children = _direct_blocks(block)
            if len(children) != 1 or children[0][2] != "pins":
                raise ValueError("unsupported net payload; refusing canonicalization")
            # Pin references may contain quoted fragments ("Ref X"-"1").
            # Keep each source token verbatim when reordering this set.
            pins = _payload_tokens(_payload(children[0][3]))
            if not pins or len(pins) != len(set(pins)):
                raise ValueError("empty or duplicate pin membership")
            net_membership[values[0]] = tuple(sorted(pins))
            net_blocks.append(
                (
                    values[0],
                    "(net " + _render_token(values[0]) + "\n      (pins " + " ".join(sorted(pins)) + ")\n    )",
                )
            )
        else:
            raise ValueError(f"unknown network record {kind!r}; refusing canonicalization")
    if not net_membership:
        raise ValueError("network contains no nets")
    canonical_network = (
        "(network\n"
        + "\n".join(
            [*("    " + block for _name, block in sorted(net_blocks)), *("    " + block for block in class_blocks)]
        )
        + "\n  )"
    )

    replacements: dict[tuple[int, int], str] = {}
    for name, replacement in (
        ("placement", canonical_placement),
        ("library", canonical_library),
        ("network", canonical_network),
    ):
        start, end, _block = sections[name]
        replacements[(start, end)] = replacement
    canonical_root = _replace_spans(root, replacements)
    canonical_text = text[:root_start] + canonical_root + text[root_end:]

    semantic = {
        "design_name": root_tokens[0],
        "placements": {
            ref: {
                "pose": row["pose"],
                "image_geometry_sha256": hashlib.sha256(repr(row["geometry"]).encode()).hexdigest(),
            }
            for ref, row in placements.items()
        },
        "net_membership": net_membership,
        "network_classes": class_blocks,
        "padstacks": padstacks,
        "untouched_sections": {
            name: block for name, (_s, _e, block) in sections.items() if name not in {"placement", "library", "network"}
        },
    }
    return canonical_text, {
        "semantic": semantic,
        "image_alias_map": alias_map,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "canonical_sha256": hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
        "canonical_bytes_changed": canonical_text != text,
    }


def _is_number(value: str) -> bool:
    try:
        return bool(value) and math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _render_token(value: str) -> str:
    return value if re.fullmatch(r"[^\s()\"]+", value) else json.dumps(value, ensure_ascii=False)


def _alias_base(name: str) -> str:
    return re.sub(r"::\d+$", "", name)


def canonicalize_native_dsn(text: str) -> tuple[str, dict[str, Any]]:
    """Canonicalize supported unordered DSN records or fail closed.

    The root design name, all numeric geometry tokens, padstack payloads,
    classes, and untouched top-level sections remain verbatim. Semantic
    summaries before and after are checked internally before returning.
    """
    canonical, info = _canonicalize(text)
    _canonical_again, after = _canonicalize(canonical)
    if info["semantic"] != after["semantic"]:
        raise ValueError("canonicalization changed DSN placement, net, or preserved payload semantics")
    info["canonical_roundtrip_sha256"] = after["canonical_sha256"]
    info["idempotent"] = canonical == _canonical_again
    if not info["idempotent"]:
        raise ValueError("DSN canonicalization is not idempotent")
    return canonical, info
