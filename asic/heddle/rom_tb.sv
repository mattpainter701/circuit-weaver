// Dump the generated weight logic so vectors.py can compare it with the model file.
`include "heddle_model.vh"
`include "tb_params.vh"
module rom_tb;
    reg [`HEDDLE_RB-1:0] row;
    wire [`HEDDLE_W-1:0] pos, neg;
    wire signed [`HEDDLE_CB-1:0] c;
    wire layer_end, is_out;
    heddle_rom rom (.*);
    integer index;
    initial begin
        for (index = 0; index < `TB_ROWS; index = index + 1) begin
            row = index; #1;
            $display("ROW %h %h %h %b %b", pos, neg, c, layer_end, is_out);
        end
        $finish;
    end
endmodule
