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
