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
    reg [15:0] regs [0:15];
    reg [15:0] scratch [0:127];
    reg [1:0] mail_full;
    reg [15:0] mail [0:1]; // Indexed by recipient.

    assign imem_addr = {slot, pc[slot]};
    wire [3:0] op = imem_data[15:12];
    wire [3:0] dst = {slot, imem_data[11:9]};
    wire [3:0] src = {slot, imem_data[8:6]};
    wire [15:0] dval = regs[dst];
    wire [15:0] sval = regs[src];
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

    integer i;
    always @(posedge clk) begin
        if (reset) begin
            slot <= 0;
            pc[0] <= 0;
            pc[1] <= 0;
            halted <= 0;
            faulted <= 0;
            mail_full <= 0;
            for (i = 0; i < 16; i = i + 1)
                regs[i] <= 0;
        end else begin
            slot <= peer;
            if (active && illegal)
                faulted[slot] <= 1'b1;
            if (retire) begin
                pc[slot] <= pc[slot] + 8'd1;
                case (op)
                    HALT: halted[slot] <= 1'b1;
                    LDI: regs[dst] <= {{7{imem_data[8]}}, imem_data[8:0]};
                    ADD: regs[dst] <= dval + sval;
                    SUB: regs[dst] <= dval - sval;
                    XOR_R: regs[dst] <= dval ^ sval;
                    LD: regs[dst] <= scratch[bank_addr];
                    ST: scratch[bank_addr] <= dval;
                    BNZ: if (dval != 0)
                             pc[slot] <= pc[slot] + 8'd1 + imem_data[7:0];
                    SEND: begin
                        mail[peer] <= dval;
                        mail_full[peer] <= 1'b1;
                    end
                    RECV: begin
                        regs[dst] <= mail[slot];
                        mail_full[slot] <= 1'b0;
                    end
                    IN: regs[dst] <= slot ? in_data[31:16] : in_data[15:0];
                    OUT: ; // The edge transfers dval to the selected endpoint.
                    VALID: regs[dst] <= {15'b0, in_valid[slot]};
                    SHL: regs[dst] <= dval << 1;
                    AND_R: regs[dst] <= dval & sval;
                endcase
            end
        end
    end
`ifdef SLOT16_FORMAL
`include "properties.svh"
`endif
endmodule
