"""Write the carrier handoff from completed and verified physical views."""
import argparse
import json
import os
import shutil
from pathlib import Path

import klayout.db as db


def export(run, output):
    final = run / "final"
    config = json.loads((run / "resolved.json").read_text())
    top = config["DESIGN_NAME"]
    pdk = config["PDK"]
    vendor, revision = (Path(config["PDK_ROOT"]) / pdk / "SOURCES").read_text().split()
    output.mkdir()
    stats = output / "stats"
    stats.mkdir()
    gds = final / "gds" / f"{top}.gds"
    layout = db.Layout()
    layout.read(str(gds))
    layout.set_property("TT_PDK", pdk)
    layout.write(str(output / f"{top}.oas"))
    for source in (gds, final / "lef" / f"{top}.lef", run / "resolved.json",
                   Path(__file__).with_name("info.yaml"), *sorted((final / "spef").glob("*/*.spef"))):
        shutil.copyfile(source, output / source.name)
    shutil.copyfile(final / "nl" / f"{top}.nl.v", output / f"{top}.v")
    synth, = run.glob("*-yosys-synthesis/reports/stat.rpt")
    shutil.copyfile(synth, stats / "synthesis-stats.txt")
    shutil.copyfile(final / "metrics.csv", stats / "metrics.csv")
    metadata = {"FLOW_NAME": "LibreLane", "FLOW_VERSION": config["meta"]["librelane_version"],
                "PDK": pdk, "PDK_SOURCE": vendor, "PDK_VERSION": revision}
    commit = {"app": "Circuit Weaver / Weftline 16", "repo": os.environ.get("SOURCE_REPO"),
              "commit": os.environ.get("SOURCE_COMMIT"), "workflow_url": os.environ.get("WORKFLOW_URL")}
    for filename, data in [("pdk.json", metadata), ("commit_id.json", commit)]:
        (output / filename).write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    export(args.run, args.output)
