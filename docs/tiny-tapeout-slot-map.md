# Tiny Tapeout slot and demo-board lookup

Use the exact Verilog top module to look up a digital project on **TTSKY26d** and make a printable bring-up sheet for the **ETR v3.3 SKY130 demo board**. The selector address chooses the project on the chip. The index grid identifies its logical placement. The die image highlights the actual cell instance, including its rotation or reflection, in the full-chip layout.

Install Circuit Weaver with its `silicon` extra for die rendering. Live lookup uses the GitHub CLI with read access to public Actions artifacts. Check access with `gh auth status`. To try a published project, run this from a Circuit Weaver checkout.

```sh
python -m pip install '.[silicon]'
circuit-weaver silicon slot-map tt_um_factory_test --report factory-test.html
```

For a project awaiting a slot, supply its `info.yaml` to preview the board wiring. Supply the expected submitted repository URL to guard against looking up a different project with the same top name. Re-run the same command after the provider merges and builds the assignment.

```sh
circuit-weaver silicon slot-map tt_um_example --info ./info.yaml \
  --expected-repo https://github.com/your-org/your-project --report bringup.html
```

| Result | What the sheet contains |
| --- | --- |
| `unassigned` | The selected index has no matching macro. The sheet shows its shuttle revision and any pin functions provided locally. |
| `assigned` | The selector address, grid, tile size, project commit and repository. A live lookup reads the pin definitions at the shuttle commit. |
| `die.status = located` | Exactly one placement of the cell in the matched full-chip hierarchy, its bounding box in micrometres and an embedded PNG. |
| `issues` | A missing or ambiguous layout, unavailable published pinout, or local pinout discrepancy is reported alongside the evidence that could be verified. |

The CLI writes JSON. Exit 0 means the requested lookup completed, including pending assignments. Exit 3 means the result has issues. Exit 1 indicates a failed request, such as an unreadable index, a different expected project repository or a mismatched local layout digest. A successful report write replaces the entire HTML, so a later pending result clears an earlier die image. If a request fails before writing, the previous sheet still describes the revision printed in its footer.

Live lookup uses the latest successful **main push** of the shuttle GDS workflow. It verifies the index commit and archive digest. A die report also requires the full-chip `final_oas` artifact from a successful precheck for that same commit. The archive checks and unique instance lookup keep pull-request candidates, expired evidence and duplicates from producing apparent placements. Omitting `--report` skips the layout download. [Shuttle repository and workflows](https://github.com/TinyTapeout/tinytapeout-sky-26d/actions)

### Offline review

Keep the provider's `shuttle_index.json`, full-chip layout, project `info.yaml`, shuttle Git commit and independently recorded SHA-256 of the extracted layout file. Supply the digest of the OAS/GDS itself. GitHub's ZIP digest identifies a different file.

```sh
circuit-weaver silicon slot-map tt_um_example --index shuttle_index.json \
  --info info.yaml --layout die.oas --layout-commit "$SHUTTLE_COMMIT" \
  --layout-sha256 "$LAYOUT_SHA256" --report bringup.html
```

An offline lookup trusts the operator's source for those files. A local index alone supports address lookup. A local pinout is labeled in the sheet. Verify the board revision and pin 1 markings before wiring. [Demo-board pin references](tiny-tapeout-board.md)

### Python and MCP

Call `circuit_weaver.tt_slot.map_tt_slot(macro, info=..., expected_repo=..., report=...)` or use the MCP tool `silicon_slot_map`. Both return the same assignment, board, die and evidence fields as the CLI. Use absolute filesystem paths with MCP.
