# Agent Platform Support

Circuit Weaver ships Agent Skills for Claude Code, Codex, OpenCode, and Kilo.
The Python installer owns platform paths, provenance tracking, and conflict
handling; the shell scripts are thin wrappers around that installer.

## Support Matrix

| Platform | Global skill directory | Invocation / discovery |
|-|-|-|
| Claude Code | `$CLAUDE_CONFIG_DIR/skills` or `~/.claude/skills` | `/circuit-weaver` |
| Codex | `~/.agents/skills` | `$circuit-weaver` or `/skills` |
| OpenCode | `$OPENCODE_CONFIG_DIR/skills`, otherwise `~/.agents/skills` | Loaded through OpenCode's skill tool |
| Kilo | `$KILO_CONFIG_DIR/skills` or `~/.kilo/skills` | Loaded from its skills directory |

The old Codex `~/.codex` directory is detected only for migration. New skills
are installed into the current `~/.agents/skills` location.

## Canonical Sources

- `skills/` is the source of truth for the 11 global skills included in the wheel.
- `src/circuit_weaver/_bundled_skills/` is the packaged mirror and must remain byte-for-byte synchronized.
- `.agents/skills/` contains repo-local skill entrypoints used while developing this checkout.
- `project-skills/` contains optional downstream project templates. These are not silently copied by a global install.

Every canonical skill directory and frontmatter `name` is already kebab-case.
Examples include `design-wizard`, `kicad-gen`, `kicad-hierarchy`,
`kicad-pcb-place`, `kicad-pinmap`, and `kicad-validate`. Installers do not
rewrite skill metadata.

## Installer Commands

No platform argument installs all supported global skills:

```bash
./install.sh
./install.ps1
```

Select platforms or individual skills explicitly when needed:

```bash
./install.sh --platform codex,opencode --skill circuit-weaver
./install.ps1 -Platform codex,opencode -Skill circuit-weaver

# Equivalent direct CLI
circuit-weaver install-skills --platform codex opencode --skills circuit-weaver
```

Useful flags:

- `--dry-run` / `-DryRun` plans the full operation without writing.
- `--skills-only` / `-SkillsOnly` skips package installation in the repository wrappers.
- `--python-only` / `-PythonOnly` installs only the package.
- `--force` / `-Force` resolves managed-file conflicts in favor of the release.
- `--backup` / `-Backup` requires force and preserves each replaced file.

## Optional MCP Server

The package also provides a stdio MCP server. Install its optional runtime and
launch it with:

```bash
pip install "circuit-weaver[mcp]"
circuit-weaver-mcp
```

The 17-tool server retains the focused tools `validate_design`, `generate_artifacts`,
`discover_projects`, `research_component`, and `manufacturing_readiness`, plus
`runtime_info`, the read-only `library_access` audit, and the shared project lifecycle tools `start_project`,
`open_project`, `adopt_project`, `plan_workflow`, `run_workflow`, `workflow_status`,
`resume_workflow`, and `cancel_workflow`. `plan_library_bundle` and
`apply_library_bundle` expose the hash-bound local asset import workflow.
The lifecycle tools require explicit
absolute project paths; specs and options are JSON objects and stage selections
are JSON arrays. Calls are synchronous request/response operations: a run tool
returns after its selected stages finish or fail. `resume_workflow` executes the
verified saved workflow; `plan_workflow` inspects without executing.
`cancel_workflow` records a request that the runner observes at a stage
boundary. The MCP server does not run the conversational design wizard.
`library_access` requires an absolute `project_root` when one is supplied and
reports local KiCad asset availability separately from functional and SPICE
support; it does not qualify electrical behavior or RF performance.
Installing skills does not configure an MCP client. See
[`project-workflows.md`](project-workflows.md) for the lifecycle API and
checkpoint behavior.

`validate_design` defaults to `detail_level="summary"`. It preserves validity,
category counts, every finding and evidence ID, but omits the embedded evidence
ledger and its per-reference index from metadata. The response identifies those
omissions. Request `detail_level="full"` for the complete report; that larger
payload can exceed an agent client's tool-output limit. Neither view changes
validation or manufacturing approval.

For reports that exceed a client's output limit, request `detail_level="page"`
with `collection="findings"` or `collection="evidence_ids"`. Retrieve **both**
collections. Start at `offset=0`, then follow `next_offset` until it is null;
pass the first page's `report_sha256` as `expected_report_sha256` on subsequent
requests. Each collection supplies its total count and SHA-256 digest, and the
page header preserves validity, category counts, metadata and the manifest
reference. Finding records retain every audit field and their evidence IDs.
The provenance collection also includes IDs not attached to any finding.

`limit` is an upper bound of 1–100 items. The server reduces the returned count
when necessary to keep serialized pages at or below 16 KB; `next_offset`
reflects the actual item count. It returns an explicit error if a single item
or header cannot fit, rather than cutting its text or provenance. A changed
report also fails with an error so the client can restart retrieval. Summary
and full-detail responses retain their existing behavior; paging covers the
summary's complete findings and provenance, while full detail includes the
embedded ledger and per-reference index.

## OpenCode with an isolated package installation

Use a fresh environment and bind both the shell and MCP server to its Python.
Activating a different shell or installing a package does not change an already
running agent's PATH. A global `circuit-weaver` executable can otherwise run an
older package even while the MCP server uses the new environment.

For example, in PowerShell:

```powershell
py -3.13 -m venv .venv
$cwPython = (Resolve-Path .venv/Scripts/python.exe).Path
& $cwPython -m pip install "circuit-weaver[mcp]"
$env:VIRTUAL_ENV = (Resolve-Path .venv).Path
$env:PATH = "$env:VIRTUAL_ENV\Scripts;$env:PATH"
& $cwPython -m circuit_weaver --version
```

Configure OpenCode's optional local MCP entry with that same absolute Python
path (replace the example path below). Provider credentials remain managed by
OpenCode, outside this configuration:

```json
{
  "mcp": {
    "circuit-weaver": {
      "type": "local",
      "command": ["C:/absolute/path/.venv/Scripts/python.exe", "-m", "circuit_weaver.mcp_server"],
      "environment": {"CIRCUIT_WEAVER_MCP_PORT": "0"},
      "enabled": true
    }
  }
}
```

For an isolated skill/config test, set `OPENCODE_CONFIG_DIR` to a new directory
before `install-skills --platform opencode --skills circuit-weaver` and before
starting OpenCode. Use `opencode --pure run --format json` to retain inspectable
tool events. Verify that OpenCode actually executes the commands and reads the
saved result; a successful agent exit alone does not establish workflow success.

Large inline circuit specifications also consume the model's tool-call output
budget. In the OpenCode 1.17.15 / DeepSeek Flash stress test, an 8192-token cap
produced malformed tool arguments for the 438-part fixture. Setting
`OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=32768` before starting the client allowed
the exact complete specification to reach `validate_design` directly. Verify
your provider supports the requested budget and check the actual tool events;
report paging bounds the response, but does not shorten the input specification.
Keep the same `OPENCODE_CONFIG_DIR` for continued sessions and confirm the server
is connected with `opencode --pure mcp list`.

The full native workflow additionally needs KiCad and its libraries, Java, and
Freerouting. From a matching source checkout, have the agent run the
[workflow validator](automation-validation.md) using the environment's absolute
Python, `--installed`, `--expected-wheel`, `--render-3d`, and `--mcp-smoke`.
The checkout supplies the benchmark scripts; the application runs from the
installed wheel. Save a new output directory for every attempt and require all
16 stages to pass, including native DRC and model coverage. For the ESP32 IoT
sample, retain the suite's explicit 0.20 mm minimum-drill assumption; the default
0.30 mm rule correctly rejects its smaller thermal holes. This prototype
assumption does not authorize manufacturing export.

## Upgrade and Conflict Policy

Each installed skill contains `.circuit-weaver-install.json`, recording the
package version and a SHA-256 hash for every managed file.

| Destination state | Default behavior | With force |
|-|-|-|
| Absent | Install and write provenance | Install |
| Matches recorded hashes | Upgrade changed release files automatically | Upgrade |
| User modified/deleted a managed file | Preserve it, report conflict, exit non-zero | Replace it |
| Extra user-created file | Leave untouched | Leave untouched |

The installer recognizes pristine skills from the final manifest-less release
so existing users can migrate. Unknown untracked installations are treated as
conflicts instead of being overwritten.

## Downstream Project Templates

Copy only the required kebab-case directories from `project-skills/` into the
downstream repository's local skills directory and commit them there. Keep
project-specific symbol libraries, footprint libraries, BOMs, pin maps,
generated KiCad artifacts, and wrappers in that downstream repository.
