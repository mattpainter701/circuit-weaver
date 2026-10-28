from types import SimpleNamespace

from circuit_weaver import _kicad_worker as worker


class _Seq(list):
    def Seq(self):
        return self


class _Point:
    x = 100_000
    y = 200_000


class _Field:
    def GetTextPos(self):
        return _Point()

    def GetTextAngleDegrees(self):
        return 0

    def IsLocked(self):
        return False

    def SetTextAngleDegrees(self, _):
        pass

    def SetTextPos(self, _):
        pass


class _Pad:
    def GetLayerSet(self):
        return _Seq([0])

    def GetAttribute(self):
        return 1

    def GetNumber(self):
        return "1"

    def GetNetname(self):
        return "GND"

    def GetPosition(self):
        return _Point()

    def GetOrientationDegrees(self):
        return 0

    def GetShape(self):
        return 1

    def GetSize(self):
        return SimpleNamespace(x=100_000, y=100_000)

    def GetDrillSize(self):
        return SimpleNamespace(x=0, y=0)

    def GetDrillShape(self):
        return 0

    def GetOffset(self):
        return SimpleNamespace(x=0, y=0)


class _Footprint:
    def __init__(self):
        self.field = _Field()
        self.pad = _Pad()

    def GetPosition(self):
        return _Point()

    def GetOrientationDegrees(self):
        return 0

    def Reference(self):
        return self.field

    def Value(self):
        return self.field

    def GetFields(self):
        return []
    def SetOrientationDegrees(self, _):
        pass
    def IsLocked(self):
        return False

    def Pads(self):
        return [self.pad]

    def GetLayer(self):
        return 0

    def GetCourtyard(self, _):
        return object()

    def GetAttributes(self):
        return 1

    def GetReference(self):
        return "J1"

    def GetFPID(self):
        return SimpleNamespace(GetLibItemName=lambda: "Connector_Generic:Conn_01x01")

    def GetFPIDAsString(self):
        return "Connector_Generic:Conn_01x01"


class _Board:
    def __init__(self):
        self.footprint = _Footprint()

    def GetEnabledLayers(self):
        # Physical top-to-bottom order intentionally differs from numeric ID order.
        return SimpleNamespace(CuStack=lambda: _Seq([0, 4, 6, 31]))

    def GetCopperLayerCount(self):
        return 4

    def GetLayerName(self, layer):
        return {0: "F.Cu", 4: "In1.Cu", 6: "In2.Cu", 31: "B.Cu"}[layer]

    def GetFootprints(self):
        return [self.footprint]

    def GetTracks(self):
        return []

    def Zones(self):
        return []

    def GetDrawings(self):
        return []
    def GetBoardEdgesBoundingBox(self):
        return SimpleNamespace(GetX=lambda: 0, GetY=lambda: 0,
                               GetWidth=lambda: 1_000_000, GetHeight=lambda: 1_000_000)


class _PCBNew:
    F_Cu = 0
    In1_Cu = 4
    In2_Cu = 6
    B_Cu = 31
    F_CrtYd = 40
    B_CrtYd = 41
    Edge_Cuts = 44
    FP_SMD = 1
    PAD_SHAPE_CIRCLE = 0
    PAD_SHAPE_RECT = 1
    PAD_SHAPE_OVAL = 2

    class LSET:
        @staticmethod
        def AllCuMask():
            return _Seq([0, 4, 6, 31])

    @staticmethod
    def ToMM(value):
        return value / 1_000_000


def test_inspect_reports_board_stack_and_keeps_pad_layer_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_field_visible", lambda _: False)
    monkeypatch.setattr(worker, "_placement_envelope", lambda *_: (0, 0, 100_000, 100_000))
    monkeypatch.setattr(worker, "_opposite_courtyard_state", lambda *_: (True, False))
    monkeypatch.setattr(worker, "_native_is_roundrect", lambda *_: False)
    monkeypatch.setattr(worker, "_native_roundrect_radius_ratio", lambda *_: None)
    monkeypatch.setattr(worker, "_native_surface_shape_known", lambda *_: True)

    board = _Board()
    board_path = tmp_path / "inspection.kicad_pcb"
    board.GetFileName = lambda: str(board_path)
    board.GetDesignSettings = lambda: SimpleNamespace(m_CopperEdgeClearance=500_000)
    board_path.with_suffix(".kicad_dru").write_text(
        '(rule "custom edge" (constraint edge_clearance (min 0.8mm)))\n', encoding="utf-8"
    )
    result = worker._inspect(board, _PCBNew)

    assert result["copper_layer_inventory_complete"] is True
    assert result["copper_layer_count"] == 4
    assert result["copper_layer_names"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert result["copper_layers"] == [
        {"id": 0, "name": "F.Cu"}, {"id": 4, "name": "In1.Cu"},
        {"id": 6, "name": "In2.Cu"}, {"id": 31, "name": "B.Cu"},
    ]
    assert result["footprints"][0]["pads"][0]["copper_layers"] == [0]
    assert result["native_edge_clearance"]["status"] == "unsupported"
