// Row-level equivalence: every pre-activation of every inference must equal the reference.
`include "heddle_model.vh"
`include "tb_params.vh"
module core_tb;
    reg clk = 0, reset = 1, req_valid = 0, sample = 0;
    reg [1:0] req_kind = 0;
    reg [4:0] req_token = 0;
    wire busy, out_valid;
    wire [4:0] out_token;
    heddle_core dut (.*);
    always #5 clk = !clk;
    reg [15:0] mem [0:`TB_CORE_ENTRIES-1];
    integer at = 0, row, checked = 0, runs = 0;
    reg [15:0] word, expect_value;
    initial begin
        $readmemh("core.hex", mem);
        repeat (3) @(negedge clk);
        reset = 0;
        while (at < `TB_CORE_ENTRIES) begin
            word = mem[at]; at = at + 1;
            req_kind = word[7:6]; sample = word[5]; req_token = word[4:0]; req_valid = 1;
            @(negedge clk);
            req_valid = 0;
            if (!word[7]) begin
                for (row = 0; row < `TB_ROWS; row = row + 1) begin
                    expect_value = mem[at]; at = at + 1;
                    if (!busy || out_valid || {{(16-`HEDDLE_VB){dut.value[`HEDDLE_VB-1]}}, dut.value} !== expect_value) begin
                        $display("FAIL run %0d row %0d: value %h expected %h busy %b", runs, row, dut.value, expect_value, busy);
                        $fatal;
                    end
                    checked = checked + 1;
                    @(negedge clk);
                end
                if (busy || !out_valid || out_token !== mem[at][4:0]) begin
                    $display("FAIL run %0d: token %0d expected %0d busy %b valid %b", runs, out_token, mem[at][4:0], busy, out_valid);
                    $fatal;
                end
                at = at + 1; runs = runs + 1;
            end else if (busy || (word[7:6] == 2'd2 && (out_valid || out_token !== 5'd0))) begin
                $display("FAIL: clear or seed left the core busy or valid");
                $fatal;
            end
        end
        $display("core: %0d inferences and %0d row values match the reference", runs, checked);
        $finish;
    end
endmodule
