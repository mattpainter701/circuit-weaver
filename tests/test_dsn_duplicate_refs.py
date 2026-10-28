from pathlib import Path
from unittest import mock

import pytest

from circuit_weaver.autoroute import _iter_sexpr_blocks, _validate_dsn_semantics, export_dsn

BOARD = '''(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "") (net 1 "GND") (net 2 "VDD_3P3")
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 10 10)
    (property "Reference" "R1" (at 0 -1.2 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "VDD_3P3")))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 11 10)
    (property "Reference" "R2" (at 0 -1.2 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "VDD_3P3")))
  (footprint "Connector_PinHeader_1x02_P2.54mm_Vertical" (layer "F.Cu") (at 20 10)
    (property "Reference" "J1" (at 0 -2 0) (layer "F.SilkS"))
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask")
      (net 2 "VDD_3P3"))
    (pad "2" thru_hole circle (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask")
      (net 1 "GND")))
  (gr_rect (start 5 5) (end 25 20) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts")))
'''

EDGE_CLEARANCE = {
    "status": "ok",
    "value_iu": 500_000,
    "value_mm": 0.5,
    "value_um": 500.0,
    "source": "BOARD_DESIGN_SETTINGS.m_CopperEdgeClearance",
}


def dsn(placements: str, pin_refs: str = 'R1-1 J1-2') -> str:
    return f'''(pcb "dup-board"
  (parser (string_quote ")) (resolution mil 1000) (unit mil)
  (structure (layer "F.Cu" (type signal)) (layer "B.Cu" (type signal))
    (boundary (path pcb 0 0 0 20000 0 20000 15000 0 15000 0 0)) (rule (clearance 5)))
  (placement {placements})
  (library (image "Resistor_SMD:R_0402_1005Metric") (image "Connector_PinHeader_1x02_P2.54mm_Vertical"))
  (network (net GND (pins {pin_refs})) (net VDD_3P3 (pins R1-2 J1-1))))
'''

DUPLICATE_CROSS_GROUP = dsn('''
    (component "Resistor_SMD:R_0402_1005Metric" (place R1 10000 10000 front 0))
    (component "Resistor_SMD:R_0402_1005Metric" (place "R1" 11000 10000 front 0))
    (component "Connector_PinHeader_1x02_P2.54mm_Vertical" (place J1 20000 10000 front 0))''')
DUPLICATE_SAME_GROUP = dsn('''
    (component "Resistor_SMD:R_0402_1005Metric"
      (place R1 10000 10000 front 0) (place "R1" 11000 10000 front 0))
    (component "Connector_PinHeader_1x02_P2.54mm_Vertical" (place J1 20000 10000 front 0))''')
VALID_DISTINCT = dsn('''
    (component "Resistor_SMD:R_0402_1005Metric" (place R1 10000 10000 front 0))
    (component "Resistor_SMD:R_0402_1005Metric" (place R2 11000 10000 front 0))
    (component "Connector_PinHeader_1x02_P2.54mm_Vertical" (place J1 20000 10000 front 0))''',
    'R1-1 R2-1 J1-2')

@pytest.mark.parametrize('payload', [DUPLICATE_SAME_GROUP, DUPLICATE_CROSS_GROUP])
def test_dsn_semantics_reject_duplicate_placed_references(payload):
    root = next(_iter_sexpr_blocks(payload, 'pcb'))
    result, error = _validate_dsn_semantics(root)
    assert result is None
    assert error == "DSN placement contains duplicate footprint reference 'R1'"

def test_dsn_semantics_accepts_distinct_references():
    root = next(_iter_sexpr_blocks(VALID_DISTINCT, 'pcb'))
    result, error = _validate_dsn_semantics(root)
    assert error is None
    assert result is not None
    assert result['design_name'] == 'dup-board'

def test_public_export_refuses_duplicate_references_without_publishing(tmp_path):
    board = tmp_path / 'duplicate.kicad_pcb'
    board.write_text(BOARD, encoding='utf-8')
    output = tmp_path / 'duplicate.dsn'
    def fake_cli_export(command, **kwargs):
        staging = Path(command[command.index('-o') + 1])
        staging.write_text(DUPLICATE_CROSS_GROUP, encoding='utf-8')
        return mock.Mock(returncode=0, stdout='', stderr='')
    with mock.patch('circuit_weaver.autoroute._find_kicad_cli', return_value='kicad-cli'), \
         mock.patch('circuit_weaver.autoroute._kicad_cli_supports_specctra', return_value=True), \
         mock.patch(
             'circuit_weaver.autoroute._inspect_native_edge_clearance', return_value=(EDGE_CLEARANCE, None)
         ), \
         mock.patch('circuit_weaver.autoroute.subprocess.run', side_effect=fake_cli_export):
        result = export_dsn(board, output)
    assert result['status'] == 'error'
    assert 'duplicate footprint reference' in result['message']
    assert not output.exists()
    assert not list(tmp_path.glob('*.staging*'))

def test_public_export_publishes_valid_distinct_references(tmp_path):
    board = tmp_path / 'valid.kicad_pcb'
    board.write_text(BOARD, encoding='utf-8')
    output = tmp_path / 'valid.dsn'
    def fake_cli_export(command, **kwargs):
        staging = Path(command[command.index('-o') + 1])
        staging.write_text(VALID_DISTINCT, encoding='utf-8')
        return mock.Mock(returncode=0, stdout='', stderr='')
    with mock.patch('circuit_weaver.autoroute._find_kicad_cli', return_value='kicad-cli'), \
         mock.patch('circuit_weaver.autoroute._kicad_cli_supports_specctra', return_value=True), \
         mock.patch(
             'circuit_weaver.autoroute._inspect_native_edge_clearance', return_value=(EDGE_CLEARANCE, None)
         ), \
         mock.patch('circuit_weaver.autoroute.subprocess.run', side_effect=fake_cli_export):
        result = export_dsn(board, output)
    assert result['status'] == 'ok'
    assert output.is_file()
    assert result['artifact']['kind'] == 'dsn'
