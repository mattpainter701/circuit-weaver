// Heddle core: a row-serial evaluator for a network whose weights are logic.
//
// One clock evaluates one neuron. The weight row is a constant selected by the
// row counter, so there is no weight memory and no multiplier. A ternary
// weight either passes its input bit, passes the inverted bit or contributes
// nothing; the row's pre-activation is twice the number of matching inputs
// plus a constant. Hidden rows shift their sign into `nxt`. Output rows feed a
// running maximum, optionally perturbed by the on-chip sampler.
`include "heddle_model.vh"
module heddle_core (
    input wire clk,
    input wire reset,
    input wire req_valid,       // ignored while busy
    input wire [1:0] req_kind,  // 0 push, 1 step, 2 clear, 3 seed
    input wire [4:0] req_token,
    input wire sample,
    output reg busy,
    output reg out_valid,
    output reg [4:0] out_token
);
    localparam W = `HEDDLE_W, HW = `HEDDLE_HW, D0 = `HEDDLE_D0, E = `HEDDLE_E, K = `HEDDLE_K;
    localparam RB = `HEDDLE_RB, PB = `HEDDLE_PB, CB = `HEDDLE_CB, VB = `HEDDLE_VB, GB = `HEDDLE_GB;
    localparam [1:0] PUSH = 2'd0, STEP = 2'd1, CLEAR = 2'd2, SEED = 2'd3;

    reg [5*K-1:0] ctx;      // the last K tokens, oldest in the low bits
    reg [HW-1:0] act, nxt;  // previous layer's outputs, and the layer being collected
    reg [RB-1:0] row;
    reg first, have_best;
    reg [30:0] lfsr;
    reg signed [VB-1:0] best;
    reg [4:0] best_idx, out_idx;

    wire [W-1:0] pos, neg;
    wire signed [CB-1:0] c;
    wire layer_end, is_out;
    heddle_rom rom (.row(row), .pos(pos), .neg(neg), .c(c), .layer_end(layer_end), .is_out(is_out));

    wire [D0-1:0] ctx_bits;
    genvar position;
    generate
        for (position = 0; position < K; position = position + 1) begin : embed
            heddle_emb emb (.token(ctx[5*position +: 5]), .bits(ctx_bits[E*position +: E]));
        end
    endgenerate
    wire [W-1:0] ctx_wide = ctx_bits;  // zero-extended to the datapath width
    wire [W-1:0] act_wide = act;
    wire [W-1:0] xin = first ? ctx_wide : act_wide;
    wire [W-1:0] hit = (pos & xin) | (neg & ~xin);
    wire [PB-1:0] agree;
    heddle_popcount count (.bits(hit), .count(agree));
    wire [VB-1:0] twice = {agree, 1'b0};
    wire signed [VB-1:0] value = $signed(twice) + c;
    wire fire = !value[VB-1];

    wire [GB-1:0] noise;
    heddle_gumbel gumbel (.index(lfsr[4:0]), .value(noise));
    wire [VB-1:0] noise_wide = sample ? noise : {GB{1'b0}};
    wire signed [VB-1:0] score = value + $signed(noise_wide);
    wire better = !have_best || score > best;

    // Five steps of x^31 + x^28 + 1 per output row give five fresh bits.
    wire [30:0] lfsr_next = {lfsr[25:0], lfsr[30:26] ^ lfsr[27:23]};
    wire [30:0] lfsr_stir = {lfsr[25:0], req_token};

    wire [4:0] new_token = req_kind == PUSH ? req_token : out_token;

    always @(posedge clk) begin
        if (reset) begin
            busy <= 1'b0;
            out_valid <= 1'b0;
            out_token <= 5'd0;
            ctx <= {(5*K){1'b0}};
            act <= {HW{1'b0}};
            nxt <= {HW{1'b0}};
            row <= {RB{1'b0}};
            first <= 1'b0;
            have_best <= 1'b0;
            lfsr <= `HEDDLE_SEED;
            best <= {VB{1'b0}};
            best_idx <= 5'd0;
            out_idx <= 5'd0;
        end else if (!busy) begin
            if (req_valid) case (req_kind)
                CLEAR: begin
                    ctx <= {(5*K){1'b0}};
                    lfsr <= `HEDDLE_SEED;
                    out_valid <= 1'b0;
                    out_token <= 5'd0;
                end
                SEED: lfsr <= lfsr_stir == 31'd0 ? 31'd1 : lfsr_stir;
                default: begin  // PUSH or STEP
                    ctx <= {new_token, ctx[5*K-1:5]};
                    busy <= 1'b1;
                    out_valid <= 1'b0;
                    row <= {RB{1'b0}};
                    first <= 1'b1;
                    have_best <= 1'b0;
                    out_idx <= 5'd0;
                end
            endcase
        end else begin
            row <= row + 1'b1;
            if (!is_out) begin
                nxt <= {fire, nxt[HW-1:1]};
                if (layer_end) begin
                    act <= {fire, nxt[HW-1:1]};
                    first <= 1'b0;
                end
            end else begin
                lfsr <= lfsr_next;
                out_idx <= out_idx + 1'b1;
                if (better) begin
                    best <= score;
                    best_idx <= out_idx;
                    have_best <= 1'b1;
                end
                if (layer_end) begin
                    out_token <= better ? out_idx : best_idx;
                    out_valid <= 1'b1;
                    busy <= 1'b0;
                end
            end
        end
    end
endmodule
