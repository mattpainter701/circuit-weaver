// Included only by the formal build. An arbitrary fixed address quantifies
// over every word without duplicating a proof for each bank and index.
(* anyconst *) reg [6:0] watched_word;
(* anyconst *) reg [3:0] watched_reg;
(* anyconst *) reg watched_lane;
reg history = 0;
reg was_reset, was_slot, was_stopped, was_blocked, was_fault, was_retire;
reg [3:0] was_op;
reg [15:0] old_word, old_reg, old_mail;
reg [7:0] old_pc;
reg old_full, old_halted, old_faulted;

wire [3:0] instruction = imem_data[15:12];
wire signed [17:0] full_address = $signed({2'b0, regs[{slot, imem_data[8:6]}]})
                                + $signed(imem_data[5:0]);
wire expected_fault = instruction == 15 || ((instruction == 5 || instruction == 6)
                                       && (full_address < 0 || full_address > 63));
wire unavailable = (instruction == 8 && mail_full[!slot]) ||
                   (instruction == 9 && !mail_full[slot]) ||
                   (instruction == 10 && !in_valid[slot]) ||
                   (instruction == 11 && !out_ready[slot]);
wire running = !reset && !halted[slot] && !faulted[slot];

always @(posedge clk) begin
    history <= 1;
    was_reset <= reset;
    was_slot <= slot;
    was_op <= instruction;
    was_stopped <= !running;
    was_fault <= running && expected_fault;
    was_blocked <= running && unavailable;
    was_retire <= retire;
    old_word <= scratch[watched_word];
    old_reg <= regs[watched_reg];
    old_pc <= pc[watched_lane];
    old_full <= mail_full[watched_lane];
    old_mail <= mail[watched_lane];
    old_halted <= halted[watched_lane];
    old_faulted <= faulted[watched_lane];
end

always @* begin
    assert(retire == (running && !expected_fault && !unavailable));
    assert((in_ready & in_valid) == ((retire && instruction == 10) ? (2'b01 << slot) : 0));
    assert((out_valid & out_ready) == ((retire && instruction == 11) ? (2'b01 << slot) : 0));
    if (history) begin
        assert(slot == (was_reset ? 0 : !was_slot));
        if (!was_retire || was_op != 6 || watched_word[6] != was_slot)
            assert(scratch[watched_word] == old_word);
        if (was_reset) begin
            assert(pc[watched_lane] == 0 && regs[watched_reg] == 0);
            assert(!mail_full[watched_lane] && !halted[watched_lane] && !faulted[watched_lane]);
        end else begin
            if (!was_retire || watched_reg[3] != was_slot)
                assert(regs[watched_reg] == old_reg);
            if (was_stopped || was_blocked || was_fault || watched_lane != was_slot)
                assert(pc[watched_lane] == old_pc);
            if (!was_retire || (was_op != 8 && was_op != 9)) begin
                assert(mail_full[watched_lane] == old_full);
                assert(mail[watched_lane] == old_mail);
            end
            assert(halted[watched_lane] == (old_halted ||
                   (watched_lane == was_slot && was_retire && was_op == 0)));
            assert(faulted[watched_lane] == (old_faulted || (watched_lane == was_slot && was_fault)));
        end
    end
end
