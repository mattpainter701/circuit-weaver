// Signal view for synthesis and lint. Layout, timing and circuit views
// come from the pinned PDK. Simulation uses its functional Verilog model.
(* blackbox *) module RM_IHPSG13_1P_256x16_c2_bm_bist (
    input wire A_CLK, A_MEN, A_WEN, A_REN, A_DLY,
    input wire [7:0] A_ADDR,
    input wire [15:0] A_DIN, A_BM,
    output wire [15:0] A_DOUT,
    input wire A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN, A_BIST_REN,
    input wire [7:0] A_BIST_ADDR,
    input wire [15:0] A_BIST_DIN, A_BIST_BM
);
endmodule
