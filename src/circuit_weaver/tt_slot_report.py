"""A printable, self-contained Tiny Tapeout assignment and wiring sheet."""

from __future__ import annotations

import base64
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _text(value: Any) -> str:
    return escape(str(value), quote=True) if value is not None else "—"


def _link(label: str, url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        return f'<a href="{_text(url)}">{_text(label)}</a>'
    return _text(label)


def _pin_table(pins: list[dict[str, Any]]) -> str:
    rows = ['<div class="scroll"><table><thead><tr><th>Signal</th><th>Project function</th>'
            '<th>Connector</th><th>Pin</th><th>Direction</th><th>Bring-up notes</th></tr>'
            '</thead><tbody>']
    for row in pins:
        cells = (_text(row.get(key)) for key in ("signal", "function", "connector", "pin", "direction", "notes"))
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return "".join(rows) + "</tbody></table></div>"


def write_slot_report(path: Path, result: dict[str, Any], image: bytes | None) -> None:
    assignment = result["assignment"]
    provider = result["provider"]
    macro = _text(result["macro"])
    page = [f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{macro} · Tiny Tapeout slot map</title>
<style>
:root {{color-scheme:light;font:16px/1.5 "Source Sans 3",Arial,sans-serif;color:#192d39;background:#f1f4f5}}
* {{box-sizing:border-box}} body {{max-width:70rem;margin:2rem auto;padding:2.5rem;background:white}}
h1,h2 {{line-height:1.15}} h1 {{overflow-wrap:anywhere;margin:.25rem 0 1rem}}
h2 {{margin:1.8rem 0 .6rem}} table {{width:100%;border-collapse:collapse;font-size:.94rem}}
th,td {{text-align:left;vertical-align:top;padding:.35rem .55rem;border-bottom:1px solid #d7dfe3}}
th {{background:#eaf1f4}} code {{overflow-wrap:anywhere}}
img {{display:block;max-width:100%;width:42rem;height:auto}} .notice {{border-left:4px solid #b67618;padding-left:1rem}}
.meta {{font-size:.9rem;overflow-wrap:anywhere}} .scroll {{overflow:auto}}
@media(max-width:600px){{body{{margin:0;padding:1rem}}}}
@media print{{body{{margin:0;padding:0}} img{{width:11cm}}tr{{break-inside:avoid}}thead{{display:table-header-group}}}}
</style><main><header>{_text(provider["name"])}<h1>{macro}</h1></header>"""]
    if assignment is None:
        page.append("<h2>Awaiting assignment</h2><p>This shuttle index has no entry for the project. "
                    "A selector address and die position become available when the shuttle lists it.</p>")
    else:
        page.append(f'<h2>Project selector address <strong>{_text(assignment["address"])}</strong></h2>'
                    f'<p>Shuttle grid ({_text(assignment["x"])}, {_text(assignment["y"])}) · '
                    f'{_text(assignment["tiles"])} tiles</p>'
                    f'<p>Project revision <code>{_text(assignment["project_commit"])}</code> '
                    f'at {_link(assignment["repo"], assignment["repo"])}</p>'
                    f'<p>On the matching shuttle, select this project in Commander or run '
                    f'<code>tt.shuttle.{macro}.enable()</code> in the demo-board REPL.</p>')
    for issue in result["issues"]:
        page.append(f'<p class="notice">{_text(issue)}</p>')
    if image:
        encoded = base64.b64encode(image).decode("ascii")
        page.append(f'<h2>Position on the die</h2><img alt="Selected project outlined on its shuttle die" '
                    f'src="data:image/png;base64,{encoded}">')
        die = result["die"]
        if "bbox_um" in die:
            coords = ", ".join(f"{coord:.3f}" for coord in die["bbox_um"])
            page.append(f'<p>Highlighted cell <code>{_text(die.get("layout_cell"))}</code>. '
                        f'Die coordinates in µm (left, bottom, right, top) <code>{coords}</code>.</p>')
    board = result["board"]
    if board:
        page.append(f'<h2>Demo-board wiring</h2><p>{_text(board["profile_id"])} · '
                    f'{_text(result["pinout_source"])}</p>')
        clock = board.get("project_config", {}).get("clock_hz")
        if type(clock) is int and clock > 0:
            page.append(f'<p>Declared project clock <strong>{clock:,} Hz</strong>.</p>')
        if board.get("orientation"):
            page.append(f'<p>{_text(board["orientation"])}</p>')
        for note in board.get("notes", []):
            page.append(f'<p>{_text(note)}</p>')
        extra, primary = [], []
        for row in board["pins"]:
            connector = row.get("connector") or ""
            (extra if connector.startswith(("J5", "J7")) else primary).append(row)
        page.append(_pin_table(primary))
        if extra:
            page.append("<details><summary>Carrier and management contacts</summary>"
                        + _pin_table(extra) + "</details>")
    page.append(f'<footer class="meta"><h2>Evidence</h2><p>Shuttle revision '
                f'<code>{_text(provider["commit"])}</code><br>Index SHA-256 '
                f'<code>{_text(result["index_sha256"])}</code></p>')
    if url := result["source"].get("url"):
        page.append(f'<p>{_link("Shuttle index build", url)}</p>')
    if image:
        page.append(f'<p>Layout SHA-256 <code>{_text(result["die"].get("input_sha256"))}</code></p>')
        if url := result["die"]["evidence"].get("url"):
            page.append(f'<p>{_link("Full-chip layout build", url)}</p>')
    if board:
        for url in board.get("provenance", []):
            page.append(f'<p>{_link(url, url)}</p>')
    if digest := result.get("published_info_sha256"):
        page.append(f'<p>Published pinout SHA-256 <code>{_text(digest)}</code></p>')
    page.append("</footer></main></html>")
    path.write_text("".join(page), encoding="utf-8")
