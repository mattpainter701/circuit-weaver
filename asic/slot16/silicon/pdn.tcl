# The carrier accepts vertical TopMetal1 stripes. Connect the rotated SRAM's
# Metal4 supply fingers to these stripes, including its separate array feed.
source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl
set_global_connections
set_voltage_domain -name CORE -power $::env(VDD_NET) -ground $::env(GND_NET)
define_pdn_grid -name core_grid -voltage_domain CORE -starts_with POWER -pins TopMetal1
add_pdn_stripe -grid core_grid -layer TopMetal1 -width $::env(PDN_VWIDTH) \
    -pitch $::env(PDN_VPITCH) -offset $::env(PDN_VOFFSET) -spacing $::env(PDN_VSPACING) -starts_with POWER
add_pdn_stripe -grid core_grid -layer Metal1 -width $::env(PDN_RAIL_WIDTH) -followpins
add_pdn_connect -grid core_grid -layers {Metal1 TopMetal1}
define_pdn_grid -macro -default -name sram_grid -starts_with POWER \
    -halo "$::env(PDN_HORIZONTAL_HALO) $::env(PDN_VERTICAL_HALO)"
add_pdn_connect -grid sram_grid -layers {Metal4 TopMetal1}
