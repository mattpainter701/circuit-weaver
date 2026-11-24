module trace_tb;
    reg clk = 0, reset = 1;
    reg [1:0] in_valid = 0, out_ready = 0;
    reg [31:0] in_data = 0;
    wire [8:0] imem_addr;
    wire [1:0] in_ready, out_valid, halted, faulted;
    wire [15:0] out_data;
    wire retire;
    reg [15:0] rom [0:511];
    wire [15:0] imem_data = rom[imem_addr];
    slot16 dut (.*);
    integer stream, fields, i;
    initial begin
        $readmemh("rom.hex", rom);
        for (i = 0; i < 128; i = i + 1)
            dut.scratch[i] = 16'h5a00 + i;
        #1 clk = 1;
        #1 clk = 0;
        stream = $fopen("events.txt", "r");
        if (!stream) $fatal(1, "missing stimulus");
        while (!$feof(stream)) begin
            fields = $fscanf(stream, "%h %h %h %h\n", reset, in_valid, in_data, out_ready);
            if (fields != 4) $fatal(1, "malformed stimulus");
            #1;
            $write("%03h %h %h %h %04h", imem_addr, retire, in_ready, out_valid, out_data);
            clk = 1;
            #1 clk = 0;
            $write(" %h %h %h", dut.slot, halted, faulted);
            for (i = 0; i < 2; i = i + 1)
                $write(" %02h %h %04h", dut.pc[i], dut.mail_full[i], dut.mail_full[i] ? dut.mail[i] : 16'd0);
            for (i = 0; i < 16; i = i + 1) $write(" %04h", dut.regs[i]);
            for (i = 0; i < 128; i = i + 1) $write(" %04h", dut.scratch[i]);
            $display("");
        end
        $finish;
    end
endmodule
