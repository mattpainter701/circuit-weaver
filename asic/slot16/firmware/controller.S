/* RV32I, zero-wait local ROM and RAM. Status bit 0 indicates sensor input.
 * Bit 1 indicates telemetry input is available and the TX register is free.
 * Service control first on every pass through the dispatcher. */
.section .text
.global _start
_start:
    sw   zero, 512(zero)
dispatch:
    lw   t0, 256(zero)
    andi t1, t0, 1
    beq  t1, zero, telemetry
    lw   t2, 260(zero)
    lw   t3, 512(zero)
    sw   t2, 512(zero)
    add  t3, t3, t2
    sw   t3, 268(zero)
telemetry:
    andi t1, t0, 2
    beq  t1, zero, dispatch
    lw   t2, 264(zero)
    xori t2, t2, 85
    sw   t2, 272(zero)
    jal  zero, dispatch
