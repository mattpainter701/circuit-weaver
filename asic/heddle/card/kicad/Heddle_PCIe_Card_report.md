# Heddle_PCIe_Card — Design Report

PCI Express x1 host card for the Heddle Tiny Tapeout tile: bridge to SPI and mux-control GPIO, breakout headers, tile clock

**Company:** Circuit Weaver contributors  
**Date:** 2026-11-28  
**Components:** 34  

## Power Tree

```
  U1 -> [VDD_1P0] -> (no consumers)
  J1 -> [VDD_3P3] -> J2, J3, U1, U2, X1, RP1
```

## BOM Summary

- **Placed components:** 34
- **Support passives (bypass caps, straps):** 8
- **Total pins:** 285

| Category | Count |
|-|-|
| buses | 1 |
| connector | 3 |
| mcu | 3 |
| passives | 27 |

### Component List

| Ref | MPN | Value | Category | Pins | Bypass | Straps |
|-|-|-|-|-|-|-|
| J1 | PCIE_X1_CARD_EDGE | PCIe x1 edge | connector | 36 | 1 | 0 |
| J2 | DF12NB(5.0)-60DP-0.5V | DF12NB(5.0)-60DP-0.5V | connector | 60 | 1 | 0 |
| J3 | DF12NB(5.0)-50DP-0.5V | DF12NB(5.0)-50DP-0.5V | connector | 50 | 1 | 0 |
| U1 | AX99100AQI | AX99100A | mcu | 69 | 2 | 0 |
| U2 | AT24C02D-SSHM | 24C02 | mcu | 8 | 1 | 0 |
| X1 | OSC_CMOS_3V3_3225 | 25MHz | mcu | 4 | 0 | 0 |
| C1 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C2 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| R1 | R_Small_20k 1% | 20k 1% | passives | 2 | 0 | 0 |
| R2 | R_Small_4.7k | 4.7k | passives | 2 | 0 | 0 |
| R3 | R_Small_10k | 10k | passives | 2 | 0 | 0 |
| R4 | R_Small_10k | 10k | passives | 2 | 0 | 0 |
| R7 | R_Small_10k | 10k | passives | 2 | 0 | 0 |
| R8 | R_Small_10k | 10k | passives | 2 | 0 | 0 |
| R9 | R_Small_33 | 33 | passives | 2 | 0 | 0 |
| R10 | R_Small_10k | 10k | passives | 2 | 0 | 0 |
| C3 | C_Small_4.7uF | 4.7uF | passives | 2 | 0 | 0 |
| C4 | C_Small_1uF | 1uF | passives | 2 | 0 | 0 |
| C5 | C_Small_10uF | 10uF | passives | 2 | 0 | 0 |
| C6 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C7 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C8 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C9 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C10 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C11 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C12 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C13 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C14 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C15 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C16 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C17 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| C18 | C_Small_100nF | 100nF | passives | 2 | 0 | 0 |
| R11 | R_Small_0 | 0 | passives | 2 | 0 | 0 |
| RP1 | PULLUPS_ONLY | I2C_PULLUPS | buses | 4 | 0 | 2 |

## Design Rationale

### RP1 — PULLUPS_ONLY

- I2C pull-ups: 62k to VDD_3P3 (3.3V)
- Speed: 100kHz, C_bus=20pF
- R = t_rise / (0.8473 * C_bus) = 59011 -> 62k

## Fabrication Notes

**PCB Specification:**
- Layer count: 2-layer sufficient
- Surface finish: ENIG or HASL lead-free (ENIG preferred for reliability)
- Solder type: Lead-free (RoHS)

**Assembly Notes:**
- **Fine-Pitch Components** — Paste stencil required; thermal pad via array recommended
- **SMT Assembly** — Stencil, pick-and-place, and reflow furnace required

**Design Checklist:**
- [ ] Gerbers exported and verified with Gerber viewer
- [ ] Component footprints match datasheets (especially fine-pitch packages)
- [ ] Thermal vias present under power dissipation components
- [ ] Solder mask clearance verified (0.1mm min from pad)
- [ ] Silkscreen text readable (>0.8mm height, >0.15mm width)

## Circuit Validation

All checks passed — no algebraic circuit issues detected.

- PASS: Pin mapping integrity
- PASS: Pinout verification
- PASS: Feedback dividers
- PASS: RC/LC filters
- PASS: Crystal load caps
- PASS: Decoupling coverage
- PASS: Inductor selection
- PASS: Capacitor voltage ratings
- PASS: Net connectivity
- PASS: Enable/shutdown pins
- PASS: Bus completeness
- PASS: Pin type conflicts (ERC)
- PASS: Power budget
- PASS: Thermal limits
- PASS: Signal integrity

## Layout Quality

| Sheet | Symbols | Body overlaps | Wire-body crossings |
|-------|---------|---------------|---------------------|
| `buses.kicad_sch` | 3 | 0 | 0 |
| `connectors.kicad_sch` | 6 | 0 | 0 |
| `mcu.kicad_sch` | 6 | 0 | 0 |
| `passives.kicad_sch` | 27 | 0 | 0 |

All sheets are geometrically clean (no overlaps, no wire-body crossings).

## Evidence Traceability

- Evidence manifest: [`evidence_manifest.json`](evidence_manifest.json)
- Referenced evidence IDs: `EV-CALCULATION-200b6d143669`, `EV-CALCULATION-2d887b958598`, `EV-CALCULATION-4d5ac814fad3`, `EV-CALCULATION-b8154286a382`, `EV-CALCULATION-bc431a196fbe`, `EV-CALCULATION-d4d9c5b240bb`, `EV-CATALOG-018a89bec41d`, `EV-CATALOG-1491492bf20a`, `EV-CATALOG-28eee4be812c`, `EV-CATALOG-2b4302ed568a`, `EV-CATALOG-2d4499dcec84`, `EV-CATALOG-2f374ca2a515`, `EV-CATALOG-31ad49e4c62a`, `EV-CATALOG-37350a7bb579`, `EV-CATALOG-3faf9fd555d0`, `EV-CATALOG-41629971078d`, `EV-CATALOG-42cf32f42c78`, `EV-CATALOG-51127c8b6772`, `EV-CATALOG-51c50986c02b`, `EV-CATALOG-51cbf0dd515a`, `EV-CATALOG-5906f79a5c3a`, `EV-CATALOG-6465b1757e8a`, `EV-CATALOG-78354c9ea673`, `EV-CATALOG-7cc9ce428858`, `EV-CATALOG-7d925aa1d636`, `EV-CATALOG-86e5e61bac19`, `EV-CATALOG-871846f3d208`, `EV-CATALOG-8ccf23590445`, `EV-CATALOG-9d00c8bbbba1`, `EV-CATALOG-b9db901a8353`, `EV-CATALOG-d4c345dcff68`, `EV-CATALOG-d6038022c8a1`, `EV-CATALOG-d6888cee6629`, `EV-CATALOG-dab957f63b0a`, `EV-CATALOG-df28174729b4`, `EV-CATALOG-e1edce81970d`, `EV-CATALOG-ebba8d56655b`, `EV-CATALOG-ed679fdb750a`, `EV-CATALOG-f3156d7b1c8b`, `EV-DATASHEET-5fc939b94d40`, `EV-FOOTPRINT_LIB-037aaec4e570`, `EV-FOOTPRINT_LIB-03aab945d549`, `EV-FOOTPRINT_LIB-10975f8b1249`, `EV-FOOTPRINT_LIB-165013caafc2`, `EV-FOOTPRINT_LIB-17554c930056`, `EV-FOOTPRINT_LIB-1f6ccfe54e63`, `EV-FOOTPRINT_LIB-245402909fe4`, `EV-FOOTPRINT_LIB-264446b16a81`, `EV-FOOTPRINT_LIB-29e69f3d94c6`, `EV-FOOTPRINT_LIB-2d28e0f0605d`, `EV-FOOTPRINT_LIB-2f6aa9f091fe`, `EV-FOOTPRINT_LIB-3114afeef06d`, `EV-FOOTPRINT_LIB-3f567e03cf75`, `EV-FOOTPRINT_LIB-48014b5c3e11`, `EV-FOOTPRINT_LIB-483ca39ff3e2`, `EV-FOOTPRINT_LIB-4f03954de3d8`, `EV-FOOTPRINT_LIB-51c6a606af56`, `EV-FOOTPRINT_LIB-585bc6e2adef`, `EV-FOOTPRINT_LIB-5ab3d1417a09`, `EV-FOOTPRINT_LIB-665f949caedf`, `EV-FOOTPRINT_LIB-779cc1f978aa`, `EV-FOOTPRINT_LIB-87a7ea49f73d`, `EV-FOOTPRINT_LIB-af035516cbf5`, `EV-FOOTPRINT_LIB-b1e183193960`, `EV-FOOTPRINT_LIB-c33ba6db8cd9`, `EV-FOOTPRINT_LIB-c888f5640924`, `EV-FOOTPRINT_LIB-c8a3f21a03a2`, `EV-FOOTPRINT_LIB-da1fdca6599a`, `EV-FOOTPRINT_LIB-f0e2c63491ca`, `EV-FOOTPRINT_LIB-f5848b11ff12`, `EV-FOOTPRINT_LIB-fe12782f3a3d`, `EV-FOOTPRINT_LIB-fee4cf09257e`, `EV-FOOTPRINT_LIB-ff6bd15f323a`, `EV-SYMBOL_LIB-00a86c1c74a9`, `EV-SYMBOL_LIB-00dcbce03b14`, `EV-SYMBOL_LIB-01dfa06769bd`, `EV-SYMBOL_LIB-04678037060c`, `EV-SYMBOL_LIB-062e1133693c`, `EV-SYMBOL_LIB-09e964718964`, `EV-SYMBOL_LIB-0aad6635d468`, `EV-SYMBOL_LIB-0b891f718bab`, `EV-SYMBOL_LIB-0bf012b4bccc`, `EV-SYMBOL_LIB-0d08046943e4`, `EV-SYMBOL_LIB-0e1468e45632`, `EV-SYMBOL_LIB-0e1f89f4d96a`, `EV-SYMBOL_LIB-0ec4cfded22f`, `EV-SYMBOL_LIB-0ed03f8b7dd7`, `EV-SYMBOL_LIB-0f3381e50bd3`, `EV-SYMBOL_LIB-0fe086793ae7`, `EV-SYMBOL_LIB-106c4af4a47c`, `EV-SYMBOL_LIB-107131393d98`, `EV-SYMBOL_LIB-1090a8d8c706`, `EV-SYMBOL_LIB-129362689345`, `EV-SYMBOL_LIB-1350a4353869`, `EV-SYMBOL_LIB-137bb247c568`, `EV-SYMBOL_LIB-14351adaf270`, `EV-SYMBOL_LIB-147a903423f5`, `EV-SYMBOL_LIB-16640369b076`, `EV-SYMBOL_LIB-16883300c643`, `EV-SYMBOL_LIB-1a9599c7a7bf`, `EV-SYMBOL_LIB-1b8bb57e1bc5`, `EV-SYMBOL_LIB-1bd0971c2466`, `EV-SYMBOL_LIB-1c5ee900d7cf`, `EV-SYMBOL_LIB-1cfafc38bd42`, `EV-SYMBOL_LIB-1d9432561593`, `EV-SYMBOL_LIB-1e185e492aef`, `EV-SYMBOL_LIB-1e26e036f96c`, `EV-SYMBOL_LIB-1e46cc885031`, `EV-SYMBOL_LIB-1ed182a27bd5`, `EV-SYMBOL_LIB-1eeefa5db57d`, `EV-SYMBOL_LIB-1fe083311c8f`, `EV-SYMBOL_LIB-20775d3c1065`, `EV-SYMBOL_LIB-20fa7695e935`, `EV-SYMBOL_LIB-212e804a65e8`, `EV-SYMBOL_LIB-21c48f2c4235`, `EV-SYMBOL_LIB-2262cb2fb18f`, `EV-SYMBOL_LIB-22f3934d42c5`, `EV-SYMBOL_LIB-25a80134d6c6`, `EV-SYMBOL_LIB-260290002d41`, `EV-SYMBOL_LIB-2650934f2b62`, `EV-SYMBOL_LIB-27325f50e436`, `EV-SYMBOL_LIB-27674e090f9d`, `EV-SYMBOL_LIB-2796a9d11dfe`, `EV-SYMBOL_LIB-28bc6d405aee`, `EV-SYMBOL_LIB-2b2388df311e`, `EV-SYMBOL_LIB-2d0cb2445f8f`, `EV-SYMBOL_LIB-2e1386b34514`, `EV-SYMBOL_LIB-2e2de6f19d93`, `EV-SYMBOL_LIB-31ac8d984d75`, `EV-SYMBOL_LIB-323d033dc476`, `EV-SYMBOL_LIB-32895a83a8ba`, `EV-SYMBOL_LIB-33317187aeff`, `EV-SYMBOL_LIB-335683bb5cfe`, `EV-SYMBOL_LIB-337510e23db2`, `EV-SYMBOL_LIB-338063030e92`, `EV-SYMBOL_LIB-33c22035405e`, `EV-SYMBOL_LIB-3409e1b10c61`, `EV-SYMBOL_LIB-35a4c9ec5a1a`, `EV-SYMBOL_LIB-36e4948f6698`, `EV-SYMBOL_LIB-36e906e5b7b6`, `EV-SYMBOL_LIB-3a6611c3487c`, `EV-SYMBOL_LIB-3bf2717c8f01`, `EV-SYMBOL_LIB-3c9f695ee195`, `EV-SYMBOL_LIB-3cc7caa12f46`, `EV-SYMBOL_LIB-3d746fe48f2c`, `EV-SYMBOL_LIB-3dc8a3655cfb`, `EV-SYMBOL_LIB-3dd86d752b8b`, `EV-SYMBOL_LIB-4149aaace4ac`, `EV-SYMBOL_LIB-417e7581fdff`, `EV-SYMBOL_LIB-43ee81800749`, `EV-SYMBOL_LIB-43effded2971`, `EV-SYMBOL_LIB-443aaa5b4f99`, `EV-SYMBOL_LIB-451343e58342`, `EV-SYMBOL_LIB-452479312525`, `EV-SYMBOL_LIB-45dd2ad07c25`, `EV-SYMBOL_LIB-4609924c764d`, `EV-SYMBOL_LIB-476b65b05d89`, `EV-SYMBOL_LIB-4ab96788b445`, `EV-SYMBOL_LIB-4b15ab075752`, `EV-SYMBOL_LIB-4c1880c88a8c`, `EV-SYMBOL_LIB-4cc8613caeff`, `EV-SYMBOL_LIB-4de358351d4f`, `EV-SYMBOL_LIB-50611e521bdf`, `EV-SYMBOL_LIB-50f5fd709afd`, `EV-SYMBOL_LIB-522b5f28b40a`, `EV-SYMBOL_LIB-523c45521e48`, `EV-SYMBOL_LIB-53173e8054c4`, `EV-SYMBOL_LIB-5383d8ec64e2`, `EV-SYMBOL_LIB-54a4919e1d5c`, `EV-SYMBOL_LIB-55b5572ba0e5`, `EV-SYMBOL_LIB-55ff9b3d9158`, `EV-SYMBOL_LIB-560b92461f43`, `EV-SYMBOL_LIB-56f59fd90c1b`, `EV-SYMBOL_LIB-57b1aa3d28af`, `EV-SYMBOL_LIB-5800b0d75016`, `EV-SYMBOL_LIB-58032fa9ac7b`, `EV-SYMBOL_LIB-586c2a5f16c7`, `EV-SYMBOL_LIB-58d461d914ec`, `EV-SYMBOL_LIB-5954747d7e2e`, `EV-SYMBOL_LIB-5a0542ee282a`, `EV-SYMBOL_LIB-5a09b1b1ec24`, `EV-SYMBOL_LIB-5e0a09c307d2`, `EV-SYMBOL_LIB-5e5e8abe037b`, `EV-SYMBOL_LIB-5efd6e127738`, `EV-SYMBOL_LIB-5f3d9b654785`, `EV-SYMBOL_LIB-604666235fe5`, `EV-SYMBOL_LIB-6192244caf42`, `EV-SYMBOL_LIB-641d382d0b0d`, `EV-SYMBOL_LIB-64800b719ebc`, `EV-SYMBOL_LIB-64911eac302c`, `EV-SYMBOL_LIB-64b3fc4be8db`, `EV-SYMBOL_LIB-65b9701ab0ab`, `EV-SYMBOL_LIB-6658d03168d5`, `EV-SYMBOL_LIB-66cb27189812`, `EV-SYMBOL_LIB-681986c4aa8c`, `EV-SYMBOL_LIB-6e8c7c6c3f54`, `EV-SYMBOL_LIB-6ece46207697`, `EV-SYMBOL_LIB-6f512dd93d06`, `EV-SYMBOL_LIB-6f76ee73211a`, `EV-SYMBOL_LIB-7205a5fb558e`, `EV-SYMBOL_LIB-7214f95c957c`, `EV-SYMBOL_LIB-73ef5a0f2c8e`, `EV-SYMBOL_LIB-750cfd69aa9c`, `EV-SYMBOL_LIB-7606272c070e`, `EV-SYMBOL_LIB-77b13749ccf5`, `EV-SYMBOL_LIB-788ecd3674e6`, `EV-SYMBOL_LIB-79d9270551a6`, `EV-SYMBOL_LIB-7b7503756b0d`, `EV-SYMBOL_LIB-7c1a2153a04e`, `EV-SYMBOL_LIB-7c5356046d40`, `EV-SYMBOL_LIB-7d57b2b34434`, `EV-SYMBOL_LIB-7da85924096b`, `EV-SYMBOL_LIB-7ddd5ee5e13d`, `EV-SYMBOL_LIB-7ea002e41a70`, `EV-SYMBOL_LIB-7f678164d628`, `EV-SYMBOL_LIB-7f964f5ed8d2`, `EV-SYMBOL_LIB-810ead9f5ffb`, `EV-SYMBOL_LIB-815a2f4c9074`, `EV-SYMBOL_LIB-8229c9c862c9`, `EV-SYMBOL_LIB-83ddb74ff7b9`, `EV-SYMBOL_LIB-8453a3234aa7`, `EV-SYMBOL_LIB-883435df6c70`, `EV-SYMBOL_LIB-8a1942af7784`, `EV-SYMBOL_LIB-8a6420487d2e`, `EV-SYMBOL_LIB-8c1d44615d72`, `EV-SYMBOL_LIB-8e1eedba9dbb`, `EV-SYMBOL_LIB-8e9e36138c1a`, `EV-SYMBOL_LIB-8f740e6ab6f6`, `EV-SYMBOL_LIB-9007997ad0da`, `EV-SYMBOL_LIB-907cd1a6577c`, `EV-SYMBOL_LIB-918ac939b3ad`, `EV-SYMBOL_LIB-91a658dd48ef`, `EV-SYMBOL_LIB-93773dc54e8d`, `EV-SYMBOL_LIB-941c85f45738`, `EV-SYMBOL_LIB-9691929762d0`, `EV-SYMBOL_LIB-9a74f152019e`, `EV-SYMBOL_LIB-9ab1b503e5e3`, `EV-SYMBOL_LIB-9c5135a94dd9`, `EV-SYMBOL_LIB-9c7e9d41404f`, `EV-SYMBOL_LIB-9cc0496e8db8`, `EV-SYMBOL_LIB-9d1cb48cb744`, `EV-SYMBOL_LIB-9d52456c1413`, `EV-SYMBOL_LIB-9e4ecfe92342`, `EV-SYMBOL_LIB-9f0cc15cc9f7`, `EV-SYMBOL_LIB-9fe99519f452`, `EV-SYMBOL_LIB-a0c510e6d3d1`, `EV-SYMBOL_LIB-a1fa9f58f298`, `EV-SYMBOL_LIB-a21d4b8b1c7e`, `EV-SYMBOL_LIB-a25b6b2c03f1`, `EV-SYMBOL_LIB-a2b7a24cf1f5`, `EV-SYMBOL_LIB-a44b50200aa4`, `EV-SYMBOL_LIB-a49113b93eb2`, `EV-SYMBOL_LIB-a4d2d612b529`, `EV-SYMBOL_LIB-a5841341fbd5`, `EV-SYMBOL_LIB-a5baac502353`, `EV-SYMBOL_LIB-a63268926d5b`, `EV-SYMBOL_LIB-a685ef6db852`, `EV-SYMBOL_LIB-a7ad0a1bd2dc`, `EV-SYMBOL_LIB-a7b489f4e7ff`, `EV-SYMBOL_LIB-a8af97a55c3e`, `EV-SYMBOL_LIB-aa3826de4c41`, `EV-SYMBOL_LIB-aa88476e3091`, `EV-SYMBOL_LIB-ac705c70b34c`, `EV-SYMBOL_LIB-ac944391ac2d`, `EV-SYMBOL_LIB-ace27275571b`, `EV-SYMBOL_LIB-adc65be276ba`, `EV-SYMBOL_LIB-aff9c68d2db6`, `EV-SYMBOL_LIB-b110bdc5c734`, `EV-SYMBOL_LIB-b208a4ef648a`, `EV-SYMBOL_LIB-b22528adfea4`, `EV-SYMBOL_LIB-b25ea1dc6fc5`, `EV-SYMBOL_LIB-b2811675ea67`, `EV-SYMBOL_LIB-b35b3b126c6b`, `EV-SYMBOL_LIB-b459d623f7b3`, `EV-SYMBOL_LIB-b4accfda2032`, `EV-SYMBOL_LIB-b559e9181023`, `EV-SYMBOL_LIB-b5f958db6c6a`, `EV-SYMBOL_LIB-b68cbc6ed138`, `EV-SYMBOL_LIB-b94efd4b3305`, `EV-SYMBOL_LIB-b9d5388726ad`, `EV-SYMBOL_LIB-bb59fbf40e6c`, `EV-SYMBOL_LIB-bbc3c2be730f`, `EV-SYMBOL_LIB-bbd660ba6da7`, `EV-SYMBOL_LIB-bf9bbd67ddfa`, `EV-SYMBOL_LIB-bfd5a9f723c8`, `EV-SYMBOL_LIB-bfe5b344058e`, `EV-SYMBOL_LIB-bff979cec278`, `EV-SYMBOL_LIB-c05e573c8524`, `EV-SYMBOL_LIB-c06b0d502b22`, `EV-SYMBOL_LIB-c0d3522d9026`, `EV-SYMBOL_LIB-c1c9b9da3623`, `EV-SYMBOL_LIB-c2fb3fa02a1b`, `EV-SYMBOL_LIB-c4adbf1e97f4`, `EV-SYMBOL_LIB-c62bde0205e3`, `EV-SYMBOL_LIB-c6da783d5e6d`, `EV-SYMBOL_LIB-c7e39ce271ae`, `EV-SYMBOL_LIB-c84b3eb1d198`, `EV-SYMBOL_LIB-c86c78e64a40`, `EV-SYMBOL_LIB-ca67b30ba76b`, `EV-SYMBOL_LIB-cb1264b444ae`, `EV-SYMBOL_LIB-cc06038b9f8c`, `EV-SYMBOL_LIB-cd18dc77f3ce`, `EV-SYMBOL_LIB-cdc391f19ce1`, `EV-SYMBOL_LIB-cdcd312d1721`, `EV-SYMBOL_LIB-cf0fccb6931f`, `EV-SYMBOL_LIB-d061abe553d1`, `EV-SYMBOL_LIB-d0e045fa8f27`, `EV-SYMBOL_LIB-d28deee09ed3`, `EV-SYMBOL_LIB-d2fd1a3de1ba`, `EV-SYMBOL_LIB-d51023bcd935`, `EV-SYMBOL_LIB-d51914a1ebc4`, `EV-SYMBOL_LIB-d57983375973`, `EV-SYMBOL_LIB-d731d4560b5c`, `EV-SYMBOL_LIB-daab79febd7a`, `EV-SYMBOL_LIB-db4bebae963d`, `EV-SYMBOL_LIB-db4ee9401609`, `EV-SYMBOL_LIB-dcb1ee76e9b9`, `EV-SYMBOL_LIB-dcf8eedce4a9`, `EV-SYMBOL_LIB-de1d9327d0a9`, `EV-SYMBOL_LIB-de5008853abd`, `EV-SYMBOL_LIB-de78ef82cab6`, `EV-SYMBOL_LIB-dec95d9becca`, `EV-SYMBOL_LIB-e08d713e8e89`, `EV-SYMBOL_LIB-e0ec7e12d803`, `EV-SYMBOL_LIB-e13dab6aa04e`, `EV-SYMBOL_LIB-e21b301aafbd`, `EV-SYMBOL_LIB-e2a4e859acf4`, `EV-SYMBOL_LIB-e2e8430b6e28`, `EV-SYMBOL_LIB-e3fa163bec36`, `EV-SYMBOL_LIB-e44ce0fe1e7c`, `EV-SYMBOL_LIB-e595085af345`, `EV-SYMBOL_LIB-e61ae4bed0eb`, `EV-SYMBOL_LIB-e8636120faac`, `EV-SYMBOL_LIB-e9191adddbd0`, `EV-SYMBOL_LIB-e920446324d9`, `EV-SYMBOL_LIB-e96aa899c948`, `EV-SYMBOL_LIB-ea9e5ca83e01`, `EV-SYMBOL_LIB-eaa119a49fb4`, `EV-SYMBOL_LIB-ec5178ace413`, `EV-SYMBOL_LIB-ed2f55f1df18`, `EV-SYMBOL_LIB-eebae43301b6`, `EV-SYMBOL_LIB-f10106d11aac`, `EV-SYMBOL_LIB-f28e659fad19`, `EV-SYMBOL_LIB-f2de2953951b`, `EV-SYMBOL_LIB-f32e8b09e3e5`, `EV-SYMBOL_LIB-f449dbc7292c`, `EV-SYMBOL_LIB-f4ac9f299e4f`, `EV-SYMBOL_LIB-f5410c68e358`, `EV-SYMBOL_LIB-f5732016c6bb`, `EV-SYMBOL_LIB-f590c0cf1a07`, `EV-SYMBOL_LIB-f5ec4649c14e`, `EV-SYMBOL_LIB-f673a54c3cff`, `EV-SYMBOL_LIB-f81bdffeb481`, `EV-SYMBOL_LIB-fd5c32465ba8`, `EV-SYMBOL_LIB-fe3b88dc32a4`, `EV-SYMBOL_LIB-fe5bd4be786f`, `EV-SYMBOL_LIB-ff1bf147e89d`, `EV-SYMBOL_LIB-ff99b7f572d5`, `EV-TOOL_RESULT-83eac3042d2b`

---

*Generated by [Circuit Weaver](https://pypi.org/project/circuit-weaver/) v0.38.0.*
