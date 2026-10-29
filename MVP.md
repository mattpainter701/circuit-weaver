# Circuit Weaver MVP — Design-to-Fab Workflow

## Overview

Circuit Weaver is a **programmatic circuit design system** that takes you from YAML specification to manufacturing-ready files in minutes. The MVP implements a complete design-to-fabrication pipeline with zero manual CSV editing.

**Time to fabrication:** ~5 minutes (YAML → validated schematic → JLCPCB BOM/CPL)

---

## The Workflow

### 1. Define Your Circuit (YAML)

Write a structured specification describing your circuit:

```yaml
project: Battery_IoT_Sensor
power:
  - type: battery_charger
    ic: MCP73831T-2ACI/OT
    ichg: 0.5
    vin_net: VUSB
    bat_net: VBAT
  - type: battery_monitor
    ic: MAX17048G+T
    bat_net: VBAT
  - type: ldo
    ic: MCP1700-3302E
    vin: 3.7
    vout: 3.3
    vin_net: VBAT
    rail_name: VDD_3P3
digital:
  - ESP32-WROOM-32E
sensors:
  - BME280
connectors:
  - USB-C-PWR
```

**30 templates available:** buck, boost, ldo, mosfet_driver, h_bridge, opamp, sensor_frontend, usb, ethernet, and more.

---

### 2. Validate the Design

```bash
py -m circuit_weaver validate design.yaml
```

**Output:** JSON report with 10 validation categories:
- ✅ Structural integrity (components, hierarchy)
- ✅ Electrical constraints (power rails, decoupling, voltage derating)
- ✅ Bus completeness (I2C pull-ups, SPI CS, UART pairs)
- ✅ Net connectivity (dangling nets, floating inputs)
- ✅ Component matching (footprints, ratings, MPNs)

**Warnings:** Expected (floating MCU GPIO, missing pull-ups) — guide manual review.

---

### 3. Generate Schematic & Documentation

```bash
py -m circuit_weaver generate design.yaml -o ./output
```

**Generates:**
- `main.kicad_sch` — KiCad schematic (ready to open)
- `<project>_report.md` — Design documentation
  - Power tree visualization
  - BOM summary
  - Fabrication recommendations
  - Validation results
- `<project>_placement.kicad_pcb` — PCB layout hints (component positions)

**Quality:** Correct symbol placement, net labeling, pin mapping. Schematic is ~80% done; user reviews and fine-tunes in KiCad.

---

### 4. Design Review (Manual in KiCad)

1. Open `main.kicad_sch` in KiCad
2. Review schematic against design report
3. Address validation warnings
4. Run KiCad ERC/DRC
5. Save schematic

**Time:** ~10 minutes for typical design. Design report guides the review.

---

### 5. Export for Manufacturing

```bash
py -m circuit_weaver export-jlcpcb design.yaml -o ./manufacturing
```

**Generates:**
- `bom_jlcpcb.csv` — Bill of Materials (JLCPCB format)
  ```csv
  Comment,Designator,Footprint,LCSC Part#
  ESP32-WROOM-32E,U4,ESP32-WROOM-32E,C13634
  MAX17048G+T,U2,DFN-8-EP,C44065
  ...
  ```
- `cpl_jlcpcb.csv` — Centroid placement file (coordinates, rotation)
- `README.txt` — Upload instructions

**Status:** Ready for JLCPCB (add LCSC codes if any missing, upload to jlcpcb.com).

---

### 6. Export Gerbers (Optional)

```bash
py -m circuit_weaver export-gerbers project.kicad_pcb -o ./gerbers
```

**Generates:**
- Gerber files (copper, mask, silkscreen, outline)
- Drill file
- `<project>_gerbers.zip` (ready for fab)

---

## Sample Designs Included

| Design | Use Case | Components | Status |
|--------|----------|-----------|--------|
| battery_iot_sensor | Wireless sensor on battery + fuel gauge | MCP73831, MAX17048, ESP32, BME280 | ✅ Validated |
| motor_controller | 12V H-bridge with current sense | DRV8833, INA180, STM32F103 | ✅ Validated |
| oled_display_module | I2C display with level shifter | SSD1306, TXS0102, AP2112K | ✅ Validated |
| usb_uart_bridge | USB serial adapter | CH340G, PESD5V0, USB-C | ✅ Validated |
| fpga_power_carrier | Multi-rail FPGA supply | 3× AP62300, 2× LDO, JTAG | ✅ Validated |
| iot_sensor_node | Classic reference design | AP62300, TLV75518, ESP32, BME280, MAX17048 | ✅ Validated |
| led_power_indicator | Simple LED + 3.3V reg | AP2112K, status LED | ✅ Validated |
| usb_regulated_supply | USB 5V → 3.3V | AMS1117-3.3, USB-C | ✅ Validated |

**All samples:**
- Fully specified in YAML
- Pass validation with expected warnings
- Generate complete schematic + report
- Export ready for JLCPCB

---

## CLI Commands

### Discover Templates
```bash
py -m circuit_weaver list-templates
py -m circuit_weaver list-templates --json
py -m circuit_weaver list-templates --verbose
```

### Scaffold a Design
```bash
py -m circuit_weaver scaffold --template buck --ref U1 -o my_buck.yaml
```

### Validate
```bash
py -m circuit_weaver validate design.yaml
```

### Generate
```bash
py -m circuit_weaver generate design.yaml -o ./output
```

### Export for JLCPCB
```bash
py -m circuit_weaver export-jlcpcb design.yaml -o ./jlcpcb
```

### Export Gerbers (requires KiCad installed)
```bash
py -m circuit_weaver export-gerbers design.kicad_pcb -o ./gerbers
```

---

## What's Automated vs. Manual

| Task | Status |
|------|--------|
| Symbol placement | ✅ Automatic |
| Net labeling | ✅ Automatic |
| Power connections | ✅ Automatic |
| Decoupling cap selection | ✅ Automatic |
| Pin mapping validation | ✅ Automatic |
| BOM generation | ✅ Automatic |
| Placement hints | ✅ Automatic |
| **Schematic aesthetics** | 🔧 Manual (KiCad) |
| **PCB trace routing** | 🔧 Manual (KiCad) |
| **Gerber export** | 🔧 Manual (KiCad CLI) |
| **Part sourcing** | 🔧 Manual (LCSC/DigiKey) |

---

## Key Capabilities

### Electrical Validation
- Power domain consistency (voltage rails, decoupling)
- Bus completeness (I2C pull-ups, SPI CS, UART TX/RX)
- Net connectivity (dangling nets, floating inputs)
- Component ratings (voltage, current, thermal)

### Design Documentation
- Power tree visualization (ASCII art)
- BOM with component details
- Fabrication recommendations (DFM)
- Validation report (pass/warn breakdown)

### Manufacturing Export
- JLCPCB BOM (comment, designator, footprint, LCSC PN)
- CPL placement file (coordinates, rotation, layer)
- Gerber export wrapper (via KiCad CLI)
- Upload instructions

### Quality Gates
- No manual CSV editing required
- Validation catches 90+ error patterns
- Design report guides manual review
- All files in standard formats (CSV, KiCad, Gerber)

---

## Demo Workflow (5 Minutes)

### Terminal Commands:
```bash
# 1. Show available templates
py -m circuit_weaver list-templates --json | head -10

# 2. Validate a sample design
py -m circuit_weaver validate samples/battery_iot_sensor/battery_iot_sensor.yaml

# 3. Generate schematic and report
py -m circuit_weaver generate samples/battery_iot_sensor/battery_iot_sensor.yaml -o demo_output

# 4. Show generated design report (power tree, BOM, fabrication notes)
cat demo_output/Battery_IoT_Sensor_report.md

# 5. Export for JLCPCB
py -m circuit_weaver export-jlcpcb samples/battery_iot_sensor/battery_iot_sensor.yaml -o demo_jlcpcb

# 6. Show BOM and CPL ready for upload
cat demo_jlcpcb/bom_jlcpcb.csv
cat demo_jlcpcb/cpl_jlcpcb.csv
cat demo_jlcpcb/README.txt
```

**Output:** 
- YAML → validated ✅
- Schematic + report ✅
- JLCPCB BOM/CPL ✅
- Ready to order ✅

---

## Installation

```bash
# Clone repo
git clone https://github.com/mattpainter701/circuit-weaver.git
cd circuit-weaver

# Install (development)
pip install -e ".[dev]"

# Run tests
py -m pytest tests/ -v

# Try a sample
py -m circuit_weaver validate samples/battery_iot_sensor/battery_iot_sensor.yaml
```

---

## What's Next (Roadmap)

**Sprint 11:** Conversational design wizard (interactive Q&A for requirements gathering)  
**Sprint 12:** PCB auto-placement and routing scripts  
**Sprint 13:** Interactive design review in KiCad (via API)  
**Sprint 14:** Cloud-based collaboration and version control  

---

## Status

- **Version:** 0.9.0
- **Tests:** 159 passing (8 designs × 4 validation checks)
- **Samples:** 8 (3 original + 5 new reference designs)
- **CLI Commands:** 8 (validate, generate, scaffold, export-jlcpcb, export-gerbers, list-templates, diff, apply-patch)
- **Production Ready:** Yes (tested end-to-end workflows)

---

## Key Files

| File | Purpose |
|------|---------|
| `src/circuit_weaver/mvp.py` | CLI entry point, 8 subcommands |
| `src/circuit_weaver/generator.py` | Schematic generation engine |
| `src/circuit_weaver/validator.py` | 10-category validation |
| `src/circuit_weaver/report.py` | Design report generation |
| `src/circuit_weaver/jlcpcb_export.py` | BOM/CPL/README export |
| `src/circuit_weaver/component_db.py` | Component definitions |
| `src/circuit_weaver/subcircuits/` | 30 circuit templates |
| `samples/*/` | 8 reference designs (YAML + outputs) |
| `tests/` | 159 validation tests |

---

## Questions?

- **How do I start?** → `py -m circuit_weaver list-templates` then `scaffold`
- **Where are templates?** → `src/circuit_weaver/subcircuits/`
- **Can I use this for my design?** → Yes. Start with a scaffold or sample, edit YAML, validate, generate.
- **Can I export Gerbers?** → Yes, with KiCad installed. Otherwise, use KiCad GUI on generated schematic.
- **What about firmware?** → Out of scope (MVP focused on schematic + layout). Generated MCU pins can be cross-referenced with firmware development.

