from pathlib import Path

import pytest

from circuit_weaver.footprint_lib import KiCadFootprintLibrary


def _make_footprint(root: Path, library: str = "Package_TO_SOT_SMD", name: str = "SOT-23-6") -> Path:
    pretty = root / f"{library}.pretty"
    pretty.mkdir(parents=True, exist_ok=True)
    footprint = pretty / f"{name}.kicad_mod"
    footprint.write_text('(footprint "x")', encoding="utf-8")
    return footprint


def _disable_platform_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("circuit_weaver.footprint_lib.platform.system", lambda: "UnlistedOS")


def test_kicad10_environment_root_resolves_an_actual_custom_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_platform_defaults(monkeypatch)
    custom_root = tmp_path / "custom-kicad-footprints"
    mod = _make_footprint(custom_root)
    monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", str(custom_root))
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.delenv("KICAD9_FOOTPRINT_DIR", raising=False)
    monkeypatch.delenv("KICAD8_FOOTPRINT_DIR", raising=False)

    library = KiCadFootprintLibrary()

    assert library.roots == [custom_root]
    assert library.footprint_exists("Package_TO_SOT_SMD:SOT-23-6")
    assert library.resolve("Package_TO_SOT_SMD:SOT-23-6") == mod


def test_environment_roots_preserve_explicit_precedence_and_deduplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_platform_defaults(monkeypatch)
    explicit = tmp_path / "explicit"
    generic = tmp_path / "generic"
    k10 = tmp_path / "kicad10"
    k9 = tmp_path / "kicad9"
    k8 = tmp_path / "kicad8"
    for root in (explicit, generic, k10, k9, k8):
        root.mkdir()
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(generic))
    monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", str(k10))
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(k9))
    monkeypatch.setenv("KICAD8_FOOTPRINT_DIR", str(k8))

    library = KiCadFootprintLibrary(explicit)

    assert library.roots == [explicit, generic, k10, k9, k8]


def test_duplicate_roots_keep_first_occurrence_and_missing_roots_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_platform_defaults(monkeypatch)
    first = tmp_path / "first"
    later = tmp_path / "later"
    missing = tmp_path / "not-installed"
    first.mkdir()
    later.mkdir()
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(first))
    monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", str(missing))
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(first / "."))
    monkeypatch.setenv("KICAD8_FOOTPRINT_DIR", str(later))

    library = KiCadFootprintLibrary(first)

    assert library.roots == [first, later]


@pytest.mark.parametrize("name", ["KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR"])
def test_versioned_roots_are_independently_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _disable_platform_defaults(monkeypatch)
    custom = tmp_path / name.lower()
    custom.mkdir()
    for env_name in (
        "KICAD_FOOTPRINT_DIR",
        "KICAD10_FOOTPRINT_DIR",
        "KICAD9_FOOTPRINT_DIR",
        "KICAD8_FOOTPRINT_DIR",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv(name, str(custom))

    assert KiCadFootprintLibrary().roots == [custom]
