// A common peripheral harness for both real RTL cores. All measurements
// refer to rising-edge indices at the external source and sink.
module controller_tb;
    localparam START = 64, PERIOD = 128, SAMPLES = 64, BURST_PERIOD = 512;
    reg clk = 0, reset = 1;
    integer cycle, phase, alignment, burst, stall, start_edge, end_edge;
    integer generated_control = 0, generated_telemetry = 0, consumed_telemetry = 0;
    integer completed_control = 0, completed_telemetry = 0;
    reg control_pending = 0;
    reg [15:0] sample_word = 0;
    wire [15:0] telemetry_word = 16'hb13d + consumed_telemetry * 37;
    wire [1:0] rx_valid = {consumed_telemetry < generated_telemetry, control_pending};
    reg [1:0] tx_full = 0;
    reg [15:0] tx_latch [0:1];
    wire [1:0] tx_ready = ~tx_full;
    wire [1:0] take_input, put_output;
    wire [15:0] output_word;

`ifdef PICORV32
    reg [31:0] rom [0:255], ram [0:63];
    wire valid, instr, trap;
    wire [31:0] addr, wdata;
    wire [3:0] wstrb;
    reg bus_ready;
    reg [31:0] rdata;
    picorv32 cpu (.clk(clk), .resetn(!reset), .trap(trap), .mem_valid(valid),
                  .mem_instr(instr), .mem_addr(addr), .mem_wdata(wdata), .mem_wstrb(wstrb),
                  .mem_ready(bus_ready), .mem_rdata(rdata), .irq(32'd0), .pcpi_wr(1'b0),
                  .pcpi_rd(32'd0), .pcpi_wait(1'b0), .pcpi_ready(1'b0));
    always @* begin
        rdata = 0;
        bus_ready = 1;
        if (instr) rdata = rom[addr[9:2]];
        else if (addr >= 512 && addr < 768) rdata = ram[addr[7:2]];
        else case (addr)
            256: rdata = {30'd0, rx_valid[1] && tx_ready[1], rx_valid[0]};
            260: begin rdata = sample_word; bus_ready = rx_valid[0]; end
            264: begin rdata = telemetry_word; bus_ready = rx_valid[1]; end
            268: bus_ready = tx_ready[0];
            272: bus_ready = tx_ready[1];
        endcase
    end
    wire accepted = !reset && valid && !instr && bus_ready;
    assign take_input = {accepted && addr == 264 && !wstrb, accepted && addr == 260 && !wstrb};
    assign put_output = {accepted && addr == 272 && wstrb == 15, accepted && addr == 268 && wstrb == 15};
    assign output_word = wdata[15:0];
    always @(posedge clk) if (accepted && addr >= 512 && addr < 768 && wstrb == 15)
        ram[addr[7:2]] <= wdata;
    initial $readmemh("rv.hex", rom);
`else
    reg [15:0] rom [0:511];
    wire [8:0] imem_addr;
    wire [1:0] halted, faulted, in_ready, out_valid;
    slot16 cpu (.clk(clk), .reset(reset), .imem_addr(imem_addr), .imem_data(rom[imem_addr]),
                .in_valid(rx_valid), .in_data({telemetry_word,sample_word}), .in_ready(in_ready),
                .out_valid(out_valid), .out_data(output_word), .out_ready(tx_ready),
                .halted(halted), .faulted(faulted), .retire());
    assign take_input = rx_valid & in_ready;
    assign put_output = tx_ready & out_valid;
    initial $readmemh("slot.hex", rom);
`endif

    reg [1:0] take, put, drain;
    reg [15:0] payload;
    integer lane;
    initial begin
        if (!$value$plusargs("phase=%d", phase) || !$value$plusargs("align=%d", alignment) ||
            !$value$plusargs("burst=%d", burst) || !$value$plusargs("stall=%d", stall))
            $fatal(1, "missing scenario");
        repeat (4) begin #1 clk = 1; #1 clk = 0; end
        reset = 0;
        start_edge = START + alignment;
        end_edge = start_edge + SAMPLES * PERIOD;
        for (cycle = 0; cycle < end_edge + 65536; cycle = cycle + 1) begin
            if (cycle >= start_edge && cycle < end_edge && (cycle-start_edge) % PERIOD == 0) begin
                if (control_pending) $fatal(1, "control input overrun");
                sample_word = 16'he001 + generated_control * 997;
                generated_control = generated_control + 1;
                control_pending = 1;
            end
            if (cycle >= start_edge+phase && cycle < end_edge &&
                    (cycle-start_edge-phase) % BURST_PERIOD == 0)
                generated_telemetry = generated_telemetry + burst;
            #1;
`ifdef PICORV32
            if (trap) $fatal(1, "RISC-V trap");
`else
            if (|faulted || |halted) $fatal(1, "unexpected Slot16 stop");
`endif
            take = take_input;
            put = put_output;
            payload = output_word;
            drain = tx_full;
            if (cycle < end_edge && ((stall == 1 && cycle % 512 < 384) || stall == 2))
                drain[1] = 0;
            for (lane = 0; lane < 2; lane = lane + 1)
                if (drain[lane]) $display("OUT %0d %0d %04h", cycle, lane, tx_latch[lane]);
            #1 clk = 1;
            #1 clk = 0;
            if (take[0]) control_pending = 0;
            if (take[1]) consumed_telemetry = consumed_telemetry + 1;
            if (drain[0]) completed_control = completed_control + 1;
            if (drain[1]) completed_telemetry = completed_telemetry + 1;
            for (lane = 0; lane < 2; lane = lane + 1)
                if (put[lane]) tx_latch[lane] = payload;
            tx_full = (tx_full & ~drain) | put;
            if (cycle >= end_edge && completed_control == SAMPLES &&
                completed_telemetry == generated_telemetry) begin
                $display("DONE %0d %0d", generated_control, generated_telemetry);
                $finish;
            end
        end
        $fatal(1, "drain timeout");
    end
endmodule
