"""Review graphics compare native board coordinates without inferring connectivity."""

from xml.etree import ElementTree as ET

from benchmarks.compare_net_layouts import comparison


def test_shared_viewport_exposes_motion_layers_and_native_length():
    a, b, c = (4, 2), (7, 2), (11, 9)
    initial = dict(bodies=[(a, b)], context=[a, c], pads=[("U<1>.4", a), ("J2.1", c)],
                   tracks=[], vias=[], extents=[a, c])
    routed = dict(initial, pads=[("U<1>.4", b), ("J2.1", c)], extents=[b, c], vias=[c],
                  tracks=[("B.Cu", b, c, (5, 1, 0), 18.125)])
    svg = ET.fromstring(ET.tostring(comparison("CLK<&>", [initial, routed], ["Placed", "Routed"])))
    ns = {"s": "http://www.w3.org/2000/svg"}
    captions = [node.text for node in svg.findall("s:text", ns)]
    assert {"Placed", "Routed", "CLK<&>", "B.Cu", "Pad motion",
            "18.12 mm   ·   1 track piece   ·   1 via"} <= set(captions)
    motion = svg.find("s:line", ns)
    assert float(motion.get("x1")) < float(motion.get("x2"))
    assert motion.get("y1") == motion.get("y2")
    assert any(" A " in path.get("d") for path in svg.findall("s:path", ns))
    fixed = [label for label in svg.findall("s:text", ns) if label.text == "J2.1"]
    assert len(fixed) == 2 and fixed[0].get("x") == fixed[1].get("x")
