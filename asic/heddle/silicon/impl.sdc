# Leave margin for wire parasitics that are unknown before routing.
source $::env(SCRIPTS_DIR)/base.sdc
set_max_transition 0.32 [current_design]
