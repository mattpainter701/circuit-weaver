"""Virtual-board integration checks and an encoder replay against hardware RTL."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asm16 import assemble, write_image
from weftline import Board, FIRMWARE, load_bank, scratch_test_banks

HERE = Path(__file__).resolve().parent


class VirtualBoardTests(unittest.TestCase):
    def test_scratch_diagnostic_detects_a_corrupt_word(self):
        board = Board(scratch_test_banks(), scratch_seed=42)
        self.assertEqual(board.cpu.contexts[0].scratch,
                         Board(scratch_test_banks(), scratch_seed=42).cpu.contexts[0].scratch)
        self.assertNotEqual(board.cpu.contexts[0].scratch, board.cpu.contexts[1].scratch)
        for _ in range(6000):
            if board.cpu.rom[0][board.cpu.contexts[0].pc] >> 12 == 5:
                break
            board.tick()
        else:
            self.fail("firmware never reached its first read")
        board.cpu.contexts[0].scratch[31] ^= 1 << 15
        for _ in range(6000):
            board.tick()
        self.assertEqual(board.last_output, [0xffff, 0x00a5])
        self.assertTrue(all(ctx.halted for ctx in board.cpu.contexts))
        contents = [ctx.scratch[:] for ctx in board.cpu.contexts]
        board.reset()
        self.assertEqual([ctx.scratch for ctx in board.cpu.contexts], contents)

    def test_scratch_monitor_and_batch_status(self):
        command = [sys.executable, str(HERE / "weftline.py"), "--scratch-test", "--scratch-seed", "0xcafe"]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        self.assertIn("scratch test passed", result.stdout)
        short = subprocess.run([*command, "--cycles", "12"], text=True, capture_output=True)
        self.assertEqual(short.returncode, 1)
        self.assertIn("incomplete", short.stdout)
        result = subprocess.run([*command, "--monitor"], text=True, capture_output=True, check=True,
                                input="step 6000\nmem 0 62 2\nmem 1 62 2\nmem 1 63 2\nquit\n")
        self.assertIn("lane=0 address=62 ff94 ff95", result.stdout)
        self.assertIn("lane=1 address=62 006b 006a", result.stdout)
        self.assertIn("error: value out of range", result.stdout)

    def test_public_cli(self):
        command = [sys.executable, str(HERE / "weftline.py")]
        demo = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual([line.split("=")[2].split()[0] for line in demo.stdout.splitlines() if " out0=" in line],
                         ["0x0000", "0x0001", "0x0002", "0x0003", "0x0004", "0x0004", "0x0003", "0x0003", "0x0004"])
        self.assertIn("out1=0x0001", demo.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / "echo.hex"
            write_image(assemble("IN r0\nOUT r0\nHALT"), rom)
            script = "ready 0 0\ninput 0 0xbeef\nstep 8\nready 0 1\nstep 4\nreset\nstatus\nquit\n"
            result = subprocess.run([*command, "--rom0", str(rom), "--monitor"], input=script,
                                    capture_output=True, text=True, check=True)
            self.assertIn("waiting for output ready", result.stdout)
            self.assertEqual(result.stdout.count("out0=0xbeef"), 1)
            self.assertIn("clock=0 next_lane=0", result.stdout)
            rom.write_text("f000\n")
            fault = subprocess.run([*command, "--rom0", str(rom)], capture_output=True, text=True)
            self.assertEqual(fault.returncode, 1)
            self.assertIn("lane=0 pc=0x00 faulted", fault.stdout)
            for invalid in ["fffff", "zzzz", "0000\n" * 257]:
                rom.write_text(invalid)
                with self.assertRaises(ValueError):
                    load_bank(rom)

    def test_encoder_against_rtl(self):
        banks = [load_bank(FIRMWARE / f"{name}.s16") for name in ("encoder", "encoder_diagnostics")]
        board = Board(banks)
        ring = [0, 1, 3, 2]
        states = [2, 3] + [value for old in ring for new in ring for value in (old, new)]
        # Change the unused input bits to exercise the firmware input mask.
        board.inputs[0].extend(state | ((i * 71 << 2) & 65535) for i, state in enumerate(states))
        expected = [[], []]
        position = errors = 0
        previous = states[0]
        for state in states:
            distance = (ring.index(state) - ring.index(previous)) % 4
            position = (position + {0: 0, 1: 1, 2: 0, 3: -1}[distance]) % 65536
            errors += distance == 2
            expected[0].append(position)
            expected[1].append(errors)
            previous = state

        events, virtual_outputs, stimulus, seen = [], [], [], [[], []]
        mailbox_blocked = False
        for edge in range(400 + 100 * len(states)):
            board.ready = [edge % 19 < 10, edge >= 300 and edge % 7 != 0]
            valid, data, ready = board.pins()
            stimulus.append(f"0 {valid:x} {data:x} {ready:x}\n")
            mailbox_blocked |= board.describe(0) == "waiting for peer mailbox space"
            depths = [len(queue) for queue in board.inputs]
            output = board.tick()
            accepted = sum(1 << lane for lane in range(2) if len(board.inputs[lane]) < depths[lane])
            events.append((valid, ready, accepted))
            if output is not None:
                virtual_outputs.append(output)
                seen[output[1]].append(output[2])
        self.assertTrue(mailbox_blocked)
        self.assertEqual(seen, expected)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_image(board.cpu.rom[0] + board.cpu.rom[1], directory / "rom.hex")
            (directory / "events.txt").write_text("".join(stimulus))
            subprocess.run(["iverilog", "-g2012", "-s", "trace_tb", "-o", "trace", HERE / "slot16.sv",
                            HERE / "trace_tb.sv"], cwd=directory, check=True)
            result = subprocess.check_output(["vvp", "trace"], cwd=directory, text=True)
        trace = [line.split() for line in result.splitlines() if line.startswith(("0", "1"))]
        self.assertEqual(len(trace), len(events))
        hardware_outputs = []
        for edge, (fields, (valid, ready, accepted)) in enumerate(zip(trace, events)):
            self.assertEqual(int(fields[2], 16) & valid, accepted, f"input handshake on edge {edge}")
            strobe = int(fields[3], 16) & ready
            if strobe:
                hardware_outputs.append((edge, strobe.bit_length() - 1, int(fields[4], 16)))
        self.assertEqual(hardware_outputs, virtual_outputs)


if __name__ == "__main__":
    unittest.main()
