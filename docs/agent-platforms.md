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

The server exposes five tools: `validate_design`, `generate_artifacts`,
`discover_projects`, `research_component`, and `manufacturing_readiness`.
These tools cover focused operations; they do not run the conversational
design wizard or provide the full CLI project lifecycle. Use the `circuit-weaver`
agent skill or CLI for end-to-end design, import, analysis, resume, and
manufacturing workflows. Installing skills does not configure an MCP client.

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
