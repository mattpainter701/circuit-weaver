// Slot16 core. Instruction memory and stream endpoints belong to the integrator.
module slot16 (
    input  wire        clk,
    input  wire        reset,
    output wire [8:0]  imem_addr,
    input  wire [15:0] imem_data,

    input  wire [1:0]  in_valid,
    input  wire [31:0] in_data,
    output reg  [1:0]  in_ready,
    output reg  [1:0]  out_valid,
    output wire [15:0] out_data,
    input  wire [1:0]  out_ready,

    output reg  [1:0]  halted,
    output reg  [1:0]  faulted,
    output wire        retire
);
    localparam [3:0] HALT = 0, LDI = 1, ADD = 2, SUB = 3, XOR_R = 4,
                     LD = 5, ST = 6, BNZ = 7, SEND = 8, RECV = 9,
                     IN = 10, OUT = 11, VALID = 12, SHL = 13, AND_R = 14;

    reg slot;
    reg [7:0] pc [0:1];
    reg [15:0] register_file [0:15];
    wire [15:0] regs [0:15]; // Architectural view includes the latest synchronous LD.
    reg load_pending;
    reg [2:0] load_reg;
    wire [3:0] writeback_reg = {!slot, load_reg};
    wire [15:0] load_data;
    for (genvar r = 0; r < 16; r = r + 1) begin : architectural_register
        assign regs[r] = load_pending && writeback_reg == r ? load_data : register_file[r];
    end
    reg [1:0] mail_full;
    reg [15:0] mail [0:1]; // Indexed by recipient.

    assign imem_addr = {slot, pc[slot]};
    wire [3:0] op = imem_data[15:12];
    wire [3:0] dst = {slot, imem_data[11:9]};
    wire [3:0] src = {slot, imem_data[8:6]};
    // The pending load belongs to the other lane. Its bypass is only
    // needed for the between-edges architectural observation above.
    wire [15:0] dval = register_file[dst];
    wire [15:0] sval = register_file[src];
    // Unsigned base plus signed displacement, before narrowing to a bank index.
    wire [16:0] address = {1'b0, sval} + {{11{imem_data[5]}}, imem_data[5:0]};
    wire [6:0] bank_addr = {slot, address[5:0]};
    wire bad_address = (op == LD || op == ST) && address >= 17'd64;
    wire active = !reset && !halted[slot] && !faulted[slot];
    wire peer = !slot;

    // Streams are sampled on the assigned slot only. Data is meaningful
    // only while its lane's strobe is high (see the interface contract).
    always @* begin
        in_ready = 2'b00;
        out_valid = 2'b00;
        in_ready[slot] = active && op == IN;
        out_valid[slot] = active && op == OUT;
    end
    assign out_data = (|out_valid) ? dval : 16'b0;

    wire waiting = (op == SEND && mail_full[peer]) ||
                   (op == RECV && !mail_full[slot]) ||
                   (op == IN && !in_valid[slot]) ||
                   (op == OUT && !out_ready[slot]);
    wire illegal = op == 4'hf || bad_address;
    assign retire = active && !waiting && !illegal;

    wire read_word = retire && op == LD;
    wire write_word = retire && op == ST;
`ifdef WEFTLINE_IHP_SRAM
    // Rows 0..63 and 64..127 belong to separate lanes. Upper rows are unreachable.
    RM_IHPSG13_1P_256x16_c2_bm_bist scratch_ram (
        .A_CLK(clk), .A_MEN(read_word || write_word), .A_WEN(write_word), .A_REN(read_word),
        .A_ADDR({1'b0, bank_addr}), .A_DIN(dval),
        .A_BM(16'hffff), .A_DOUT(load_data), .A_DLY(1'b1),
        .A_BIST_CLK(1'b0), .A_BIST_EN(1'b0), .A_BIST_MEN(1'b0), .A_BIST_WEN(1'b0),
        .A_BIST_REN(1'b0), .A_BIST_ADDR(8'b0), .A_BIST_DIN(16'b0), .A_BIST_BM(16'b0)
    );
`else
    reg [15:0] scratch [0:127];
    reg [15:0] read_q;
    always @(posedge clk) begin
        if (write_word) scratch[bank_addr] <= dval;
        if (read_word) read_q <= scratch[bank_addr];
    end
    assign load_data = read_q;
`endif

    integer i;
    always @(posedge clk) begin
        if (reset) begin
            slot <= 0;
            load_pending <= 0;
            pc[0] <= 0;
            pc[1] <= 0;
            halted <= 0;
            faulted <= 0;
            mail_full <= 0;
            for (i = 0; i < 16; i = i + 1)
                register_file[i] <= 0;
        end else begin
            // SRAM samples the request on the retiring edge. Commit its result
            // during the peer's slot, before this lane can use the register.
            load_pending <= read_word;
            if (read_word) load_reg <= dst[2:0];
            if (load_pending) register_file[writeback_reg] <= load_data;
            slot <= peer;
            if (active && illegal)
                faulted[slot] <= 1'b1;
            if (retire) begin
                pc[slot] <= pc[slot] + 8'd1;
                case (op)
                    HALT: halted[slot] <= 1'b1;
                    LDI: register_file[dst] <= {{7{imem_data[8]}}, imem_data[8:0]};
                    ADD: register_file[dst] <= dval + sval;
                    SUB: register_file[dst] <= dval - sval;
                    XOR_R: register_file[dst] <= dval ^ sval;
                    BNZ: if (dval != 0)
                             pc[slot] <= pc[slot] + 8'd1 + imem_data[7:0];
                    SEND: begin
                        mail[peer] <= dval;
                        mail_full[peer] <= 1'b1;
                    end
                    RECV: begin
                        register_file[dst] <= mail[slot];
                        mail_full[slot] <= 1'b0;
                    end
                    IN: register_file[dst] <= slot ? in_data[31:16] : in_data[15:0];
                    OUT: ; // The edge transfers dval to the selected endpoint.
                    VALID: register_file[dst] <= {15'b0, in_valid[slot]};
                    SHL: register_file[dst] <= dval << 1;
                    AND_R: register_file[dst] <= dval & sval;
                endcase
            end
        end
    end
`ifdef SLOT16_FORMAL
`include "properties.svh"
`endif
endmodule
