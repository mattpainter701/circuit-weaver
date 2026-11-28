// Pin replay: the tile must match the Python pin model on every clock.
`include "tb_params.vh"
module pin_tb;
    reg clk = 0, rst_n = 0, ena = 1;
    reg [7:0] ui_in = 0, uio_in = 8'h01;
    wire [7:0] uo_out, uio_out, uio_oe;
    tt_um_heddle dut (.*);
    always #5 clk = !clk;
    reg [43:0] mem [0:`TB_PIN_CLOCKS-1];
    integer at;
    initial begin
        $readmemh("pins.hex", mem);
        @(negedge clk);
        for (at = 0; at < `TB_PIN_CLOCKS; at = at + 1) begin
            {ena, rst_n, ui_in, uio_in} = mem[at][41:24];
            @(negedge clk);
            if ({uo_out, uio_out, uio_oe} !== mem[at][23:0]) begin
                $display("FAIL clock %0d: uo %h uio_out %h uio_oe %h expected %h", at, uo_out, uio_out, uio_oe, mem[at][23:0]);
                $fatal;
            end
        end
        $display("pins: %0d clocks match the pin model", `TB_PIN_CLOCKS);
        $finish;
    end
endmodule
