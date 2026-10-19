# USB-UART and I2C Expansion Prototype — Demo Design

A minimal USB-powered programming and I2C expansion prototype. It contains a USB-C power-only connector, a 3.3 V LDO, a CH340G USB-UART bridge with its 12 MHz crystal, two physical I2C pull-up resistors, a JST-PH I2C expansion connector, and 2.54 mm UART and USB-data headers. J1 carries VBUS only; connect VUSB/GND/USB D+/D− at J4 to a verified USB host breakout with a suitable harness. The design does not include a humidity sensor, Zigbee radio, battery, or charger.

## Design Blocks

| Ref | Template | IC | Purpose |
|-|-|-|-|
| U1 | ldo | AP2112K-3.3 | 5V USB -> 3.3V regulated supply |
| U2 | usb_controller | CH340G | USB-UART bridge with external 12 MHz crystal |
| Y1 | crystal_oscillator | HC-49S | 12 MHz reference for CH340G |
| RP1 | i2c_bus | PULLUPS_ONLY | Logical bus container for the physical pull-ups |
| J2 | connector | JST_PH_4P | Off-board VDD_3P3/GND/I2C expansion |
| J3 | connector | PIN_HEADER_4P | Off-board VDD_3P3/GND/UART breakout |
| J4 | connector | PIN_HEADER_4P | Off-board VUSB/GND/USB D+/D− breakout |

J2 exposes VDD_3P3, GND, I2C_SDA, and I2C_SCL for an off-board sensor. J3 exposes VDD_3P3, GND, UART_TX, and UART_RX. J4 exposes VUSB, GND, USB_DP, and USB_DM; J1 is the existing power-only USB-C connector, so connect J4 to a verified external USB-C data breakout with a suitable harness. The sample has no sensor or radio connected.

## Run the Demo

```bash
# 1. Require the guarded source validation to pass
circuit-weaver validate zigbee_humidistat.yaml --strict

# 2. Generate the schematic only after native KiCad ERC passes
circuit-weaver generate zigbee_humidistat.yaml --output ./output --no-svg --require-kicad

# 3. Build a prototype PCB on an explicit review canvas
circuit-weaver build-pcb ./output/main.kicad_sch --output ./output/USB_UART_I2C_Prototype.kicad_pcb --width 80 --height 60 --clearance 0.127

# 2. Estimate component costs at volume
circuit-weaver cost-bom zigbee_humidistat.yaml --qty 1,10,100

# 3. Check signal integrity constraints
circuit-weaver si-constraints zigbee_humidistat.yaml

# 4. Thermal analysis
circuit-weaver thermal-analysis zigbee_humidistat.yaml

# 5. Optimize component placement
circuit-weaver optimize-placement zigbee_humidistat.yaml --iterations 1000 --seed 42

# 6. Interactive placement viewer
circuit-weaver placement-viewer zigbee_humidistat.yaml --output output/viewer.html

# 7. Panelization hints (50x40mm board, 100 qty)
circuit-weaver panelize --board-width 50 --board-height 40 --qty 100

# 8. Generate 3D-printable enclosure
circuit-weaver design-enclosure --board-width 50 --board-height 40 --component-height 10 --output output/enclosure.scad

# 9. Export for JLCPCB assembly
circuit-weaver export-jlcpcb zigbee_humidistat.yaml --output output/jlcpcb
```

## Output Files

```
output/
  main.kicad_sch                  # KiCad schematic
  Zigbee_Humidistat_placement.kicad_pcb  # PCB with initial placement
  Zigbee_Humidistat_report.md     # Design report
  canonical_spec.yaml             # Normalized spec
  design_ir.json                  # Design intermediate representation
  viewer.html                     # Interactive placement viewer
  enclosure.scad                  # OpenSCAD enclosure (3D printable)
  jlcpcb/
    bom_jlcpcb.csv               # JLCPCB assembly BOM
    cpl_jlcpcb.csv               # Component placement file
    README.txt                    # Upload instructions
```
