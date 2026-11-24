# Leave routing margin for the SRAM pins before parasitics are extracted.
source $::env(SCRIPTS_DIR)/base.sdc
set_max_transition 0.32 [current_design]
set_max_fanout 6 [current_design]
