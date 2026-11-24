module test_slot16;
    reg clk = 0, reset = 0;
    reg [1:0] in_valid, out_ready;
    reg [31:0] in_data;
    wire [8:0] imem_addr;
    reg [15:0] rom [0:511];
    wire [15:0] imem_data = rom[imem_addr];
    wire [1:0] in_ready, out_valid, halted, faulted;
    wire [15:0] out_data;
    wire retire;
    slot16 dut (.*);

    // Independent encoder. Each bank defaults to HALT.
    function automatic [15:0] imm(input integer op, d, value);
        imm = {op[3:0], d[2:0], value[8:0]};
    endfunction
    function automatic [15:0] rr(input integer op, d, s, offset);
        rr = {op[3:0], d[2:0], s[2:0], offset[5:0]};
    endfunction

    integer cycle, retired [0:1], sent [0:1], read_count [0:1];
    integer out_cycle [0:1];
    reg [15:0] last_out [0:1];
    task automatic tick;
        integer lane;
        begin
            #1;
            if (!reset) begin
                lane = cycle % 2;
                if (imem_addr[8] !== lane[0]) $fatal(1, "wrong instruction bank at %d", cycle);
                if (in_ready[1-lane] || out_valid[1-lane]) $fatal(1, "unowned stream");
                if (retire) retired[lane] = retired[lane] + 1;
                if (in_ready[lane] && in_valid[lane]) read_count[lane] = read_count[lane] + 1;
                if (out_valid[lane] && out_ready[lane]) begin
                    sent[lane] = sent[lane] + 1;
                    last_out[lane] = out_data;
                    out_cycle[lane] = cycle;
                end
            end else if ({out_valid, in_ready, retire} !== 5'b0) $fatal(1, "active during reset");
            clk = 1;
            #1 clk = 0;
            cycle = cycle + 1;
        end
    endtask

    task automatic fresh;
        integer i;
        begin
            reset = 1;
            in_valid = 0;
            in_data = 0;
            out_ready = 3;
            for (i = 0; i < 512; i = i + 1) rom[i] = 0;
            for (i = 0; i < 2; i = i + 1) begin
                retired[i] = 0;
                sent[i] = 0;
                read_count[i] = 0;
                out_cycle[i] = -1;
                last_out[i] = 'x;
            end
            cycle = 0;
            tick;
            cycle = 0;
            reset = 0;
        end
    endtask

    task automatic stream_trial(input integer polling, peer_mode, arrival);
        integer expected_read, expected_output;
        begin
            fresh;
            if (polling) begin
                rom[0] = imm(1, 7, 1);     // Constant true.
                rom[1] = imm(12, 1, 0);    // Read input status.
                rom[2] = imm(7, 1, 1);     // Available -> consume.
                rom[3] = imm(7, 7, -3);    // Try status again.
                rom[4] = imm(10, 0, 0);
                rom[5] = imm(11, 0, 0);
            end else begin
                rom[0] = imm(10, 0, 0);    // IN
                rom[1] = imm(11, 0, 0);    // OUT
            end
            case (peer_mode)
                1: begin
                    rom[256] = imm(1, 2, 1);
                    rom[257] = rr(6, 2, 0, 0); // Repeated private stores.
                    rom[258] = imm(7, 2, -2);
                end
                2: rom[256] = imm(9, 0, 0);    // Blocked on empty mailbox.
                3: rom[256] = 16'hf000;        // Faulted.
            endcase
            repeat (arrival) tick;
            in_valid[0] = 1;
            in_data[15:0] = 16'hb123;
            while (!read_count[0] && cycle < arrival + 14) tick;
            in_valid[0] = 0;
            while (!sent[0] && cycle < arrival + 16) tick;
            // Poll samples status at 2 + 6k, then branches and reads.
            expected_read = polling ? 2 + 6 * ((arrival - 2 + 5) / 6) + 4
                                    : 2 * ((arrival + 1) / 2);
            expected_output = expected_read + 2;
            if (read_count[0] != 1 || sent[0] != 1 || last_out[0] !== 16'hb123 ||
                out_cycle[0] != expected_output || retired[0] != (polling ? expected_output / 2 + 1 : 2))
                $fatal(1, "stream trial failed");
            $display("poll=%0d peer=%0d input_at=%0d output_at=%0d retired=%0d",
                     polling, peer_mode, arrival, out_cycle[0], retired[0]);
        end
    endtask

    task automatic bank_trial(input integer lane, base, displacement);
        integer victim, neighbor;
        begin
            fresh;
            victim = 256 * lane;
            neighbor = 256 * (1-lane);
            rom[victim] = imm(1, 1, base);
            rom[victim+1] = imm(1, 2, 21);
            rom[victim+2] = rr(6, 2, 1, displacement);
            rom[victim+3] = imm(11, 2, 0);
            rom[neighbor] = imm(1, 2, 93);
            rom[neighbor+1] = rr(6, 2, 0, 0);
            rom[neighbor+2] = rr(5, 3, 0, 0);
            rom[neighbor+3] = imm(11, 3, 0);
            repeat (16) tick;
            if (faulted !== (1 << lane) || retired[lane] != 2 || sent[lane] ||
                !halted[1-lane] || last_out[1-lane] !== 16'd93)
                $fatal(1, "address fault escaped lane %d", lane);
        end
    endtask

    integer i, arrival;
    initial begin
        for (i = 0; i < 4; i = i + 1) begin
            for (arrival = 16; arrival < 22; arrival = arrival + 1) begin
                stream_trial(0, i, arrival);
                stream_trial(1, i, arrival);
            end
        end
        for (i = 0; i < 2; i = i + 1) begin
            bank_trial(i, 63, 1);
            bank_trial(i, 0, -1);
            bank_trial(i, -1, 1); // 65535 + 1 must fault before narrowing.
        end

        fresh;
        for (i = 0; i < 2; i = i + 1) begin
            rom[256*i]   = imm(1, 1, 64);
            rom[256*i+1] = imm(1, 2, 60+i);
            rom[256*i+2] = rr(6, 2, 1, -1); // Same local word 63.
            rom[256*i+3] = rr(5, 3, 1, -1);
            rom[256*i+4] = imm(11, 3, 0);
        end
        repeat (14) tick;
        if (last_out[0] !== 16'd60 || last_out[1] !== 16'd61 || halted !== 3 || faulted !== 0)
            $fatal(1, "private bank alias");

        fresh;
        rom[0] = imm(1, 0, -1);
        rom[1] = imm(1, 1, 3);
        rom[2] = rr(2, 0, 1, 0); // 2 after wrap
        rom[3] = rr(13, 0, 0, 0); // 4
        rom[4] = rr(3, 0, 1, 0); // 1
        rom[5] = rr(4, 0, 1, 0); // 2
        rom[6] = rr(14, 0, 1, 0);
        rom[7] = imm(7, 0, -9); // PC wraps to 255
        rom[255] = imm(11, 0, 0);
        repeat (18) tick;
        if (last_out[0] !== 16'd2 || imem_addr !== 9'd0) $fatal(1, "ALU/PC wrap");

        fresh;
        rom[0] = imm(1, 0, 33);
        rom[1] = imm(8, 0, 0);   // Fill peer mailbox.
        rom[2] = imm(1, 0, 44);
        rom[3] = imm(8, 0, 0);   // Block until peer reads first word.
        rom[4] = imm(9, 1, 0);
        rom[5] = imm(11, 1, 0);
        rom[256] = imm(10, 4, 0); // Hold consumer until host allows it.
        rom[257] = imm(9, 0, 0);
        rom[258] = imm(11, 0, 0);
        rom[259] = imm(9, 1, 0);
        rom[260] = rr(2, 1, 4, 0);
        rom[261] = imm(8, 1, 0); // Reply with second word + external input.
        out_ready[1] = 0;
        repeat (16) tick;
        if (retired[0] != 3 || retired[1] != 0) $fatal(1, "full/empty blocking");
        in_valid[1] = 1;
        in_data[31:16] = 16'd7;
        repeat (8) tick;
        in_valid = 0;
        if (retired[1] != 2 || sent[1] != 0) $fatal(1, "output backpressure");
        out_ready = 3;
        repeat (20) tick;
        if (read_count[1] != 1 || sent[0] != 1 || sent[1] != 1 || last_out[1] !== 16'd33 ||
            last_out[0] !== 16'd51 || halted !== 3 || faulted !== 0) $fatal(1, "message order");

        fresh;
        rom[0] = imm(8, 0, 0);
        rom[256] = 16'hf000;
        repeat (4) tick;
        if (faulted !== 2 || halted !== 1) $fatal(1, "illegal opcode");
        fresh;
        rom[256] = imm(9, 0, 0);
        rom[0] = rr(5, 0, 0, -1);
        repeat (8) tick;
        if (retired[1] != 0 || faulted !== 1 || halted !== 0) $fatal(1, "reset/fault state");
        $display("Slot16 RTL tests passed");
        $finish;
    end
endmodule
