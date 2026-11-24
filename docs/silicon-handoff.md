# Silicon layout and handoff

Use `circuit-weaver silicon preview` to inspect the geometry in a core, integrated shuttle wrapper or full die. Select the intended top cell explicitly when a library has multiple roots. A reference padframe can show the bond-pad locations and the space reserved for the design. Only an integrated revision can establish that the design is wired to those pads.

Use `circuit-weaver silicon handoff` to review the files for **one provider, run and revision of the layout**. The provider defines the submission boundary. A shared shuttle may accept a project wrapper and assemble the full die itself. A standalone die typically also needs a pad ring, power domains, ESD, a seal ring and a package/bond map. Select the provider's rules, PDK and integration contract for that run. [Caravel wrapper requirements](https://github.com/efabless/caravel_user_project/blob/f534826abc72af4f9d467446b8b82c9471df68da/docs/source/index.md) · [Tiny Tapeout submission procedure](https://tinytapeout.com/guides/advanced-workshop/submit-your-design/)

## Review manifest

For a process-specific rendering, pass the provider's native layer-properties file with `circuit-weaver silicon preview carrier.oas --top chip_top --layers process.lyp -o carrier.png`.

The schema-1 JSON file is the root of the input package. Use paths relative to its directory. Each file needs a role and the SHA-256 of its exact bytes. The following deliberately incomplete example names two roles. Replace the illustrative hashes with measured values.

```json
{
  "schema": 1,
  "provider": "example-provider",
  "run": "example-run",
  "declared_top": "chip_top",
  "roles": [
    {"role": "layout", "scope": "top_level", "path": "chip.gds", "sha256": "<layout sha256>"},
    {"role": "drc", "path": "reports/drc.txt", "sha256": "<report sha256>"}
  ],
  "required_roles": [],
  "verification": {
    "drc": {"status": "pass", "provider": "example-provider", "run": "example-run",
            "role": "drc", "layout_sha256": "<layout sha256>", "report_sha256": "<report sha256>"}
  }
}
```

| Evidence role | What goes in the file |
|---|---|
| `layout` | The declared GDS root. Set scope to `top_level` for a full die or `shuttle_wrapper` for a provider-owned padframe. A `core_macro` is an intermediate input. |
| `pin_power_map` | Physical terminals or harness signals, direction, voltage domains, supplies and grounds. |
| `integration_spec` or `package_bond_map` | Provider interface and tile allocation, or package pins, pad-to-pin bonds and package orientation. |
| `drc`, `lvs`, `timing`, `provider_precheck` | Actual reports for the declared layout. For each role, supply a corresponding verification record with the provider, run, report hash, layout hash and reviewer/tool-reported pass status. |
| `bringup_plan` | Reset and power-up procedure, known test vectors, expected observations and test equipment. |

Use `required_roles` for additional evidence mandated by the target run, such as antenna checks, density, additional corners, electrical-rule checks, package drawing or a provider-specific job manifest. Listing a role makes its file mandatory. Keep the report-producing tools, versions, run IDs, deck revisions, corners, constraints and waiver decisions with the evidence.

The result's `report_modes` records which results Circuit Weaver could read. It inspects KLayout DRC marker databases for the declared top, KLayout and Netgen LVS comparisons, OpenROAD finish-stage setup and hold metrics, LibreLane setup and hold results across reported corners, and Tiny Tapeout's precheck XML for failed or skipped cases. Other formats rely on the supplied attestation and are marked `attested`. A clean DRC result applies to the deck that produced it. For Tiny Tapeout, retain the official project's `tt_submission` artifact and the precheck reports from the same run; the provider action constructs the upload files. [Tiny Tapeout GDS and precheck actions](https://github.com/TinyTapeout/tt-gds-action)

A complete review returns `ready_for_provider_review`. This describes the supplied package. Circuit Weaver checks file integrity, evidence binding and the declared physical top. The provider or responsible reviewer determines whether the checks, coverage and waivers satisfy the run. The optional ZIP is a local review archive. The provider may require a different upload format or a Git revision. Track an actual submitted revision, provider receipt, wafer/packaging status and measured bring-up separately.

## Python and MCP

Use `circuit_weaver.silicon_preview.render_silicon_preview(layout, output, top=..., layers=...)` and `circuit_weaver.silicon_handoff.review_silicon_handoff(manifest, bundle=...)` from Python. The local MCP tools are `silicon_preview` and `silicon_handoff`. Pass absolute paths in MCP. An incomplete review returns its blockers for inspection.
