"""Power/flag annotation must be globally unique without changing libraries."""

import re

from circuit_weaver.generator import _annotate_power_symbols
from circuit_weaver.primitives import (
    sexpr_power_instance,
    sexpr_power_lib_entry,
    sexpr_pwr_flag_instance,
    sexpr_pwr_flag_lib_entry,
)


def test_generated_power_references_are_unique_stable_and_match_instance_records(tmp_path):
    paths = [tmp_path / "a.kicad_sch", tmp_path / "b.kicad_sch"]
    library = sexpr_power_lib_entry("GND") + sexpr_pwr_flag_lib_entry()
    for index, path in enumerate(paths):
        path.write_text(
            '(kicad_sch (lib_symbols\n' + library + ')\n'
            + sexpr_power_instance("GND", 10, 10 + index, 90)
            + sexpr_pwr_flag_instance(20, 20 + index) + ')', encoding="utf-8"
        )
    _annotate_power_symbols(reversed(paths))
    contents = [p.read_text(encoding="utf-8") for p in paths]
    properties = [ref for text in contents for ref in re.findall(r'\(property "Reference" "(#[^"]+\d+)"', text)]
    instances = [ref for text in contents for ref in re.findall(r'\(reference "(#[^"]+)"', text)]
    assert len(properties) == len(set(properties)) == 4
    assert properties == instances
    assert all(library in text for text in contents)
    _annotate_power_symbols(paths)
    assert [p.read_text(encoding="utf-8") for p in paths] == contents
