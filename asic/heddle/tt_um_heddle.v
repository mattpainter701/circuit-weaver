// Tiny Tapeout pin wrapper for the Heddle core.
//
// Two request ports share the core. The SPI port (mode 0, MSB first, one
// command per byte) is meant for a host bridge. The strobed parallel port
// needs only switches or a microcontroller. Every external input passes a
// synchronizer, so SCK may run at up to one eighth of the tile clock.
module tt_um_heddle (
    input wire [7:0] ui_in, output wire [7:0] uo_out,
    input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe,
    input wire ena, input wire clk, input wire rst_n
);
    wire reset = !rst_n || !ena;
    reg [2:0] sck_s, cs_s, go_s;
    reg [1:0] mosi_s, sample_s;
    reg [7:0] tx;
    reg [6:0] rx;
    reg [2:0] nbit;
    reg overrun, cfg_sample, sample_q, toggle, valid_q;
    wire busy, out_valid;
    wire [4:0] out_token;

    wire cs_active = !cs_s[1];
    wire cs_start = cs_s[2] && !cs_s[1];
    wire sck_rise = cs_active && !sck_s[2] && sck_s[1];
    wire sck_fall = cs_active && sck_s[2] && !sck_s[1];
    wire go_rise = !go_s[2] && go_s[1];
    wire [7:0] cmd = {rx, mosi_s[1]};
    wire cmd_done = sck_rise && nbit == 3'd7;
    wire [2:0] op = cmd[7:5];
    // 1 push token, 2 step, 3 clear, 4 stir sampler, 5 configure, others read status only
    wire spi_req = cmd_done && (op == 3'd1 || op == 3'd2 || op == 3'd3 || op == 3'd4);
    wire [1:0] spi_kind = op == 3'd1 ? 2'd0 : op == 3'd2 ? 2'd1 : op == 3'd3 ? 2'd2 : 2'd3;
    wire req_valid = spi_req || go_rise;
    wire [1:0] req_kind = spi_req ? spi_kind : {1'b0, !ui_in[6]};
    wire [4:0] req_token = spi_req ? cmd[4:0] : ui_in[4:0];
    wire [7:0] status = {busy, out_valid, overrun, out_token};
    wire load_status = cs_start || (sck_fall && nbit == 3'd0);

    heddle_core core (
        .clk(clk), .reset(reset), .req_valid(req_valid), .req_kind(req_kind), .req_token(req_token),
        .sample(sample_q), .busy(busy), .out_valid(out_valid), .out_token(out_token)
    );

    always @(posedge clk) begin
        if (reset) begin
            sck_s <= 3'b000;
            cs_s <= 3'b111;
            go_s <= 3'b000;
            mosi_s <= 2'b00;
            sample_s <= 2'b00;
            tx <= 8'h00;
            rx <= 7'h00;
            nbit <= 3'd0;
            overrun <= 1'b0;
            cfg_sample <= 1'b0;
            sample_q <= 1'b0;
            toggle <= 1'b0;
            valid_q <= 1'b0;
        end else begin
            sck_s <= {sck_s[1:0], uio_in[3]};
            cs_s <= {cs_s[1:0], uio_in[0]};
            go_s <= {go_s[1:0], ui_in[5]};
            mosi_s <= {mosi_s[0], uio_in[1]};
            sample_s <= {sample_s[0], ui_in[7]};
            if (!busy) sample_q <= sample_s[1] || cfg_sample;
            valid_q <= out_valid;
            if (out_valid && !valid_q) toggle <= !toggle;
            if (cs_start) nbit <= 3'd0;
            else if (sck_rise) begin
                rx <= cmd[6:0];
                nbit <= nbit + 3'd1;
            end
            if (load_status) tx <= status;
            else if (sck_fall) tx <= {tx[6:0], 1'b0};
            if (cmd_done && op == 3'd5) cfg_sample <= cmd[0];
            if (cs_start) overrun <= 1'b0;  // reading the flag at the start of a frame clears it
            if ((req_valid && busy) || (spi_req && go_rise)) overrun <= 1'b1;
        end
    end

    assign uo_out = {toggle, busy, out_valid, out_token};
    assign uio_out = {5'b00000, tx[7], 2'b00};
    assign uio_oe = {5'b00000, ena && !uio_in[0], 2'b00};
    wire _unused = &{1'b0, uio_in[7:4], uio_in[2]};
endmodule
