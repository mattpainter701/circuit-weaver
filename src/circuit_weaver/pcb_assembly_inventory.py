"""Inspect PCB footprints for visible 3D models used in assembly previews.

This deliberately small S-expression reader only extracts top-level footprint
children. It does not resolve model paths or assert that a model depicts the
correct package; the native KiCad renderer remains responsible for loading
each referenced model.
"""

from __future__ import annotations

from typing import TypeAlias

_Node: TypeAlias = str | list["_Node"]


def _tokens(source: str) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
        elif char == ";":
            newline = source.find("\n", index + 1)
            index = len(source) if newline < 0 else newline + 1
        elif char in "()":
            result.append(char)
            index += 1
        elif char == '"':
            index += 1
            value: list[str] = []
            while index < len(source) and source[index] != '"':
                if source[index] == "\\":
                    index += 1
                    if index >= len(source):
                        raise ValueError("unterminated escape in quoted string")
                value.append(source[index])
                index += 1
            if index >= len(source):
                raise ValueError("unterminated quoted string")
            index += 1
            result.append("".join(value))
        else:
            start = index
            while index < len(source) and not source[index].isspace() and source[index] not in "();":
                index += 1
            result.append(source[start:index])
    return result


def _parse(source: str) -> _Node:
    tokens = _tokens(source)
    cursor = 0

    def expression() -> _Node:
        nonlocal cursor
        if cursor >= len(tokens):
            raise ValueError("unexpected end of KiCad board")
        token = tokens[cursor]
        cursor += 1
        if token == "(":
            node: list[_Node] = []
            while True:
                if cursor >= len(tokens):
                    raise ValueError("unbalanced KiCad board expression")
                if tokens[cursor] == ")":
                    cursor += 1
                    return node
                node.append(expression())
        if token == ")":
            raise ValueError("unexpected closing parenthesis in KiCad board")
        return token

    root = expression()
    if cursor != len(tokens):
        raise ValueError("unexpected content after KiCad board expression")
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("input is not a KiCad PCB S-expression")
    return root


def _children(node: list[_Node], name: str) -> list[list[_Node]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == name]


def _footprint_reference(footprint: list[_Node]) -> str | None:
    reference = None
    for prop in _children(footprint, "property"):
        if len(prop) > 2 and prop[1] == "Reference" and isinstance(prop[2], str):
            reference = prop[2]
            break
    if reference is not None:
        return reference
    for text in _children(footprint, "fp_text"):
        if len(text) > 2 and text[1] == "reference" and isinstance(text[2], str):
            return text[2]
    return None


def _has_electrical_pad(footprint: list[_Node]) -> bool:
    for pad in _children(footprint, "pad"):
        if len(pad) <= 2:
            continue
        if not isinstance(pad[2], str):
            raise ValueError("malformed KiCad pad type")
        if pad[2] in {"smd", "thru_hole"}:
            return True
    return False


def _is_dnp_or_board_only(footprint: list[_Node]) -> bool:
    attrs = _children(footprint, "attr")
    if any(value in {"dnp", "board_only"} for attr in attrs for value in attr[1:] if isinstance(value, str)):
        return True
    return any(
        len(prop) > 2 and prop[1] == "dnp" and str(prop[2]).casefold() in {"yes", "true", "1"}
        for prop in _children(footprint, "property")
    )


def _model_is_hidden(model: list[_Node]) -> bool:
    if "hide" in model[1:]:
        return True
    return any(
        not hide[1:] or hide[1] not in {"no", "false", "0"}
        for hide in _children(model, "hide")
    )


def assembly_component_coverage(board_text: str) -> dict:
    """Return required assembly footprint coverage by visible 3D models.

    A required footprint has at least one SMD or plated through-hole pad and
    is not explicitly marked DNP or board-only. The result counts model
    declarations separately, so multiple visible models on one footprint are
    preserved in the reported count.
    """
    root = _parse(board_text)
    components: list[dict] = []
    references: dict[str, bool] = {}
    for footprint in _children(root, "footprint"):
        reference = _footprint_reference(footprint)
        required = _has_electrical_pad(footprint) and not _is_dnp_or_board_only(footprint)
        if required:
            if not reference or reference.startswith("REF**"):
                raise ValueError("required assembly footprint is missing an annotated reference")
            if reference in references:
                raise ValueError(f"duplicate footprint reference: {reference}")
            references[reference] = True

        models = _children(footprint, "model")
        visible_count = sum(not _model_is_hidden(model) for model in models)
        hidden_count = len(models) - visible_count
        component = {
            "reference": reference,
            "required": required,
            "visible_model_count": visible_count,
            "hidden_model_count": hidden_count,
        }
        if required and visible_count == 0:
            component["reason"] = "only_hidden_models" if hidden_count else "no_model_declared"
        components.append(component)

    required_components = [component for component in components if component["required"]]
    missing_references = [
        component["reference"] for component in required_components if component["visible_model_count"] == 0
    ]
    represented = len(required_components) - len(missing_references)
    return {
        "expected": len(required_components),
        "represented": represented,
        "missing": len(missing_references),
        "missing_references": missing_references,
        "components": components,
        "model_declaration_total": sum(
            component["visible_model_count"] + component["hidden_model_count"]
            for component in components
        ),
    }
