// Fixed-firmware Tiny Tapeout top. All inputs and strobes are synchronous.
module tt_um_weftline16 (
    input wire [7:0] ui_in, output wire [7:0] uo_out,
    input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe,
    input wire ena, input wire clk, input wire rst_n
);
    wire reset = !rst_n || !ena;
    reg test_mode;
    wire [8:0] pc;
    wire [15:0] instruction;
    wire [1:0] rx_ready, tx_valid, halted, faulted;
    wire [15:0] tx_data;
    reg [15:0] result0, result1;
    wire [15:0] selected = ui_in[4] ? result1 : result0;
    weftline_rom rom (.addr({test_mode, pc}), .word(instruction));
    slot16 cpu (
        .clk(clk), .reset(reset), .imem_addr(pc), .imem_data(instruction),
        .in_valid({1'b0, ui_in[2]}), .in_data({30'b0, ui_in[1:0]}), .in_ready(rx_ready),
        .out_valid(tx_valid), .out_data(tx_data), .out_ready(2'b11),
        .halted(halted), .faulted(faulted), .retire()
    );
    always @(posedge clk) begin
        if (reset) begin
            test_mode <= ui_in[5];
            result0 <= 0;
            result1 <= 0;
        end else begin
            if (tx_valid[0]) result0 <= tx_data;
            if (tx_valid[1]) result1 <= tx_data;
        end
    end
    assign uo_out = ui_in[3] ? selected[15:8] : selected[7:0];
    assign uio_out = {test_mode, faulted, halted, tx_valid, rx_ready[0]};
    assign uio_oe = {8{ena}};
    wire _unused = &{1'b0, uio_in, ui_in[7:6], rx_ready[1]};
endmodule
