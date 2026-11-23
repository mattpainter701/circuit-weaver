# Dimensioned native package model

The TDFN-8 STEP in this directory was generated with the official KiCad library
generator at commit `a903a695bf4e69905e9e56548f44dd46cdc1130e`. The installed
KiCad footprint references this exact model name, but the standard model library
does not include the file. This is a parametric package envelope, with a 2 × 2 mm
body, 0.75 mm maximum height, eight leads at 0.5 mm pitch and a 0.8 × 1.2 mm exposed
pad. It is not a manufacturer-supplied CAD solid or a mechanical certification.

Upstream sources and parameters:

- [KiCad generator source at the pinned commit](https://gitlab.com/kicad/libraries/kicad-footprint-generator/-/tree/a903a695bf4e69905e9e56548f44dd46cdc1130e)
- `data/package/no_lead/dfn_vson_wson_uson-8.yaml`, entry
  `TDFN-8-1EP_2x2mm_P0.5mm_EP0.8x1.30mm`. The generator emits the model name
  `TDFN-8-1EP_2x2mm_P0.5mm_EP0.8x1.2mm` from its physical package dimensions.
- Manufacturer outline 21-0168, package T822+3, as cited by the footprint and
  [MAX17048/MAX17049 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/max17048-max17049.pdf).

The LFCSP-72 STEP fills the missing `Package_CSP.3dshapes` model referenced by
stock footprints. It is generated from the official KiCad footprint-family
block at the same pinned generator commit and represents a generic nominal
10 x 10 mm body, 72 leads at 0.5 mm pitch, a 5.3 x 5.3 mm exposed pad, and
1.00 mm maximum overall height. The height range comes from ADI's AD9528
CP-72-6 outline (0.80 / 0.85 / 1.00 mm min/nom/max); a later ADI CP-72-6
outline revises the minimum to 0.75 mm while keeping the same 1.00 mm maximum.

This is a generic shared-footprint-family envelope, not manufacturer CAD or
an exact AD9528 package fit. The AD9528-specific drawing gives nominal body Y
of 9.75 mm and lead lengths 0.24 / 0.42 / 0.60 mm; this generic model retains
the upstream family block's 10 x 10 mm body and lead dimensions. The generator
also infers lead height as one fifth of overall height when `lead_height` is
absent; this is a generator heuristic, not a sourced measurement. Inventory
records the overlay hash, source hashes, runtime, license, selected maximum
height, and these geometric limitations. The STEP header is copied unchanged.

- [ADI AD9528 Rev. G data sheet](https://www.analog.com/media/en/technical-documentation/data-sheets/ad9528.pdf), page 68 (identifies AD9528BCPZ as CP-72-6).
- [ADI AD9528 package outline drawing](https://www.analog.com/media/jp/technical-documentation/data-sheets/AD9528_jp.pdf), page 66 (product-specific CP-72-6 dimensions).
- [Current ADI CP-72-6 package outline](https://www.analog.com/media/en/package-pcb-resources/package/pkg_pdf/lfcspcp/cp-72/cp_72_6.pdf), showing the later 0.75 mm minimum height.

The LFCSP-72 model is generated from part
`LFCSP-72-1EP_10x10mm_Pitch0.5mm_EP5.3x5.3mm_ThermalVias`. The packaged
`lfcsp72-family-height-overlay.yaml` contains only the part ID, source
references, and sourced height range. Its SHA-256 and the full generation
overlay/source hashes are recorded in `inventory.json`.

To reproduce the generic family envelope, clone the official generator into a
writable copy, check out the recorded commit, and install its `[3d]`
dependencies. In the copied checkout, merge the packaged overlay's
`size_source` and `overall_height` values into the existing part entry in
`data/package/no_lead/qfn-7x.yaml`. Preserve the rest of the upstream entry;
the small packaged overlay is not a replacement for its geometry. Then run
from the checkout's `src` directory:

```sh
git clone https://gitlab.com/kicad/libraries/kicad-footprint-generator.git kicad-footprint-generator-copy
git -C kicad-footprint-generator-copy checkout a903a695bf4e69905e9e56548f44dd46cdc1130e
cd kicad-footprint-generator-copy/src
python -m generators.generate -g package/no_lead -p LFCSP-72-1EP_10x10mm_Pitch0.5mm_EP5.3x5.3mm_ThermalVias -m /path/to/new-models -j 1 -v
```

With the pinned checkout and its `[3d]` dependencies installed, regenerate using:

```sh
python -m generators.generate -g package/no_lead -p TDFN-8-1EP_2x2mm_P0.5mm_EP0.8x1.30mm -m /path/to/new-models -j 1
```

`inventory.json` records the model hash, generating sources, runtime versions,
dimensions and provenance. Assembly rendering uses it only for this exact missing
standard-library model. Installed models take precedence. Incomplete custom model
libraries and unknown aliases remain errors. The renderer records the asset and
provenance hashes, protects both from output collisions, and checks they remain
unchanged during rendering. The source board is not rewritten.

The STEP retains its upstream copyright and license header: Copyright © 2026
KiCAD; [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/legalcode), with
the [KiCad electronic-design exception](https://gitlab.com/kicad/libraries/kicad-packages3D/-/blob/master/LICENSE.md).
This third-party asset has that license independently of the application's MIT
license. No changes were made to the generated STEP.
