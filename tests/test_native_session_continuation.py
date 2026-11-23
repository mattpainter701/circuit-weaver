"""Real Freerouting regression for importing a generated SES as copper."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from circuit_weaver._specctra_continuation import continuation_dsn
from circuit_weaver.autoroute import (
    _find_freerouting_command,
    _probe_freerouting_capabilities,
    _route_specctra,
)

_DSN = '''(pcb "two-net"
  (resolution um 10) (unit um)
  (structure (layer F.Cu (type signal)) (layer B.Cu (type signal))
    (boundary (path pcb 0 0 0 10000 0 10000 8000 0 8000 0 0))
    (via V (shape (circle F.Cu 600) (circle B.Cu 600)))
    (rule (width 250) (clearance 200)))
  (placement
    (component P (place J1 2000 4000 front 0) (place J2 8000 4000 front 0)))
  (library
    (image P (pin Pad 1 0 0) (pin Pad 2 0 2000))
    (padstack Pad (shape (circle F.Cu 900) (circle B.Cu 900)))
    (padstack V (shape (circle F.Cu 600) (circle B.Cu 600))))
  (network (net ROUTED (pins J1-1 J2-1)) (net REMAINING (pins J1-2 J2-2)))
  (wiring))
'''


@pytest.mark.skip_category("optional-tool")
def test_freerouting_imports_generated_wiring_then_routes_remaining_net(tmp_path):
    command = _find_freerouting_command()
    if not command:
        reason = "Native continuation regression requires pinned Freerouting 2.5.0"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    capability = _probe_freerouting_capabilities(command, timeout_seconds=20)
    if capability.get("version") != "2.5.0":
        reason = f"Native continuation regression requires Freerouting 2.5.0: {capability}"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    dsn = tmp_path / "two-net.dsn"
    dsn.write_text(_DSN, encoding="utf-8", newline="\n")
    session = '''(session "two-net"
      (base_design "two-net.dsn")
      (placement (component P (place J1 2000 4000 front 0) (place J2 8000 4000 front 0)))
      (was_is)
      (routes (resolution um 10) (library_out (padstack V))
        (network_out (net ROUTED
          (wire (path F.Cu 2500 20000 40000 80000 40000)))))
    )
'''
    converted, evidence = continuation_dsn(_DSN, session)
    assert evidence["net_count"] == 1 and evidence["wire_count"] == 1
    dsn.write_text(converted, encoding="utf-8", newline="\n")

    result = _route_specctra(
        dsn, tmp_path / "completed.ses", command, passes=10, timeout_seconds=45,
        overwrite=False, headless=True, optimization_threads=0,
        optimizer_strategy=None, optimizer_hybrid_ratio=None,
        optimizer_item_selection=None, optimizer_improvement_threshold=None,
        seed=17, capability_probe=capability,
    )
    assert result["status"] == "ok", result.get("message", result)
    stdout = Path(result["router_logs"]["stdout"]["path"]).read_text(encoding="utf-8", errors="replace")
    assert re.search(r"Auto-routing stage started[^\r\n]*for 1 unrouted items?", stdout, re.I), stdout
    assert result["stats"]["incomplete"] == 0, result["stats"]
    assert result["artifact"]["valid"] is True, result["artifact"]
