"""Time one standard repair proposal inside KiCad Python. Invoked by replay_routing_repair.py."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch


def probe(request):
    started = time.perf_counter()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "circuit_weaver"))
    import _kicad_worker as worker
    import _native_routing_repair as repair
    import pcbnew

    if sys.platform == "win32":
        import wx

        worker._native_wx_app = wx.GetApp() or wx.App(False)
        worker._native_wx_log = wx.LogStderr()
        wx.Log.SetActiveTarget(worker._native_wx_log)

    metrics = {"init_s": time.perf_counter() - started, "load_s": 0.0, "setup_s": None,
               "search_s": None, "segment_s": 0.0, "via_s": 0.0, "segment_calls": 0, "via_calls": 0}
    load_board, repair_one, search = pcbnew.LoadBoard, repair.repair_one, repair._search_with_fallback
    repair_start = None

    def timed_load(*args, **kwargs):
        before = time.perf_counter()
        try:
            return load_board(*args, **kwargs)
        finally:
            metrics["load_s"] += time.perf_counter() - before

    def timed_repair(*args, **kwargs):
        nonlocal repair_start
        repair_start = time.perf_counter()
        return repair_one(*args, **kwargs)

    def timed_check(operation, kind):
        def check(*args):
            before = time.perf_counter()
            try:
                return operation(*args)
            finally:
                metrics[kind + "_s"] += time.perf_counter() - before
                metrics[kind + "_calls"] += 1
        return check

    def timed_search(start, goal, layer_count, layers, origins, bounds, step, budget, clear, via):
        before = time.perf_counter()
        metrics["setup_s"] = before - repair_start
        try:
            result = search(start, goal, layer_count, layers, origins, bounds, step, budget,
                            timed_check(clear, "segment"), timed_check(via, "via"))
            metrics["expanded_states"] = result[2]
            return result
        finally:
            metrics["search_s"] = time.perf_counter() - before

    with patch.object(pcbnew, "LoadBoard", timed_load), patch.object(repair, "repair_one", timed_repair), \
            patch.object(repair, "_search_with_fallback", timed_search):
        try:
            response = worker.execute(request)
            status, detail = "candidate", response["route_repair"]
        except (ValueError, RuntimeError) as exc:
            status, detail = "rejected", str(exc)
    metrics["process_body_s"] = time.perf_counter() - started
    return {"status": status, "detail": detail, "timings": metrics}


if __name__ == "__main__":
    response = probe(json.loads(sys.stdin.read()))
    Path(sys.argv[1]).write_text(json.dumps(response), encoding="utf-8")
