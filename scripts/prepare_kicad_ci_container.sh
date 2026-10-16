#!/usr/bin/env bash
set -euo pipefail

expected_major="${1:?Usage: prepare_kicad_ci_container.sh <8|9|10>}"
case "$expected_major" in
  8|9|10) ;;
  *) echo "Unsupported KiCad major: $expected_major" >&2; exit 2 ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "KiCad CI container setup must run as root" >&2
  exit 1
fi

# Checkout's temporary HOME can hide its safe.directory entry from later
# container steps. Trust only this job's checked-out workspace, then require
# real Git metadata; do not substitute a revision or allow a wildcard.
if ! git -C "$GITHUB_WORKSPACE" rev-parse --verify HEAD; then
  git config --global --add safe.directory "$GITHUB_WORKSPACE"
  git -C "$GITHUB_WORKSPACE" rev-parse --verify HEAD
fi

python3 -m venv --system-site-packages "$RUNNER_TEMP/kicad-python"
printf '%s\n' "$RUNNER_TEMP/kicad-python/bin" >> "$GITHUB_PATH"

version_output="$(kicad-cli version)"
printf '%s\n' "$version_output"
if [[ ! "$version_output" =~ (^|[^0-9])([0-9]+)\. ]]; then
  echo "::error::Could not parse KiCad version output: $version_output"
  exit 1
fi
actual_major="${BASH_REMATCH[2]}"
if [[ "$actual_major" != "$expected_major" ]]; then
  echo "::error::Expected KiCad $expected_major but container has KiCad $actual_major"
  exit 1
fi

test -s /usr/share/kicad/template/fp-lib-table
test -s /usr/share/kicad/template/sym-lib-table
test -s /usr/share/kicad/symbols/Device.kicad_sym
test -d /usr/share/kicad/footprints/Resistor_SMD.pretty
install -D --mode=0644 \
  /usr/share/kicad/template/fp-lib-table \
  "$HOME/.config/kicad/${expected_major}.0/fp-lib-table"

python3 - "$expected_major" <<'PY'
import sys

import pcbnew

expected_major = sys.argv[1]
build_version = pcbnew.GetBuildVersion()
print(f"pcbnew {build_version} ({pcbnew.__file__})")
if not build_version.startswith(f"{expected_major}."):
    raise SystemExit(f"Expected pcbnew {expected_major}.x, got {build_version}")
pcbnew.BOARD()
PY
