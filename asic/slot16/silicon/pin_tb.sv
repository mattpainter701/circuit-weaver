// Shared by RTL and post-route cell-netlist simulations.
`timescale 1ns/1ps
module pin_tb;
    reg clk = 0, rst_n = 0, ena = 1;
    reg [7:0] ui_in = 0;
    wire [7:0] uo_out, uio_out, uio_oe;
    tt_um_weftline16 chip (.clk(clk), .rst_n(rst_n), .ena(ena), .ui_in(ui_in),
                           .uo_out(uo_out), .uio_in(8'b0), .uio_out(uio_out), .uio_oe(uio_oe));
    integer fd, fields, edge_count = 0;
    reg [7:0] data, status, enables;
    initial begin
        fd = $fopen("pins.txt", "r");
        if (!fd) $fatal(1, "missing pins.txt");
        #20 clk = 1;
        #20 clk = 0;
        while (!$feof(fd)) begin
            fields = $fscanf(fd, "%h %h %h %h %h %h\n", ena, rst_n, ui_in, data, status, enables);
            if (fields != 6) $fatal(1, "malformed pin vector");
            #20;
            if ({uo_out,uio_out,uio_oe} !== {data,status,enables})
                $fatal(1, "edge %0d got %h/%h/%h expected %h/%h/%h",
                       edge_count, uo_out,uio_out,uio_oe, data,status,enables);
            clk = 1;
            #20 clk = 0;
            edge_count = edge_count + 1;
        end
        $display("%0d pin vectors passed", edge_count);
        $finish;
    end
endmodule
