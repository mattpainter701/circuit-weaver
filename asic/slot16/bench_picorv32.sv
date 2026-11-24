// Optional baseline. Supply the upstream picorv32.v named in README.md.
// Both programs block for one input word and immediately echo it.
module bench_picorv32;
    reg clk = 0, resetn = 0;
    wire trap, valid, instr;
    wire [31:0] addr, wdata;
    wire [3:0] wstrb;
    reg ready;
    reg [31:0] rdata;
    integer cycle, arrival, inputs, outputs;
    reg armed;

    picorv32 cpu (.clk(clk), .resetn(resetn), .trap(trap), .mem_valid(valid),
                  .mem_instr(instr), .mem_addr(addr), .mem_wdata(wdata),
                  .mem_wstrb(wstrb), .mem_ready(ready), .mem_rdata(rdata),
                  .irq(32'd0), .pcpi_wr(1'b0), .pcpi_rd(32'd0),
                  .pcpi_wait(1'b0), .pcpi_ready(1'b0));

    always @* begin
        ready = 1;
        case (addr)
            0: rdata = 32'h10002083; // lw x1, 256(x0)
            4: rdata = 32'h10102223; // sw x1, 260(x0)
            8: rdata = 32'h0000006f; // jal x0, 0 (done)
            256: begin
                ready = cycle >= arrival && inputs == 0;
                rdata = 32'h0000b123;
            end
            default: rdata = 0;
        endcase
    end

    task automatic tick;
        begin
            #1;
            if (resetn && valid && !instr && addr == 256 && !wstrb) begin
                if (cycle < arrival) armed = 1;
                if (ready) inputs <= inputs + 1;
            end
            if (resetn && valid && !instr && addr == 260 && wstrb == 4'b1111) begin
                if (wdata !== 32'h0000b123 || inputs != 1) $fatal(1, "bad echo");
                outputs = outputs + 1;
                $display("PicoRV32 input_at=%0d output_at=%0d latency=%0d", arrival, cycle, cycle-arrival);
            end
            clk = 1;
            #1 clk = 0;
            cycle = cycle + 1;
        end
    endtask

    initial begin
        for (arrival = 16; arrival < 22; arrival = arrival + 1) begin
            resetn = 0;
            cycle = 0;
            inputs = 0;
            outputs = 0;
            armed = 0;
            repeat (4) tick;
            cycle = 0;
            resetn = 1;
            while (!outputs && cycle < 64 && !trap) tick;
            if (!armed || outputs != 1 || trap) $fatal(1, "incomplete baseline");
        end
        $finish;
    end
endmodule
