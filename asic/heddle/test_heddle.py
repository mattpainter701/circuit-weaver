"""Reference-model and host-protocol checks that need no simulator."""
import json
import unittest

import heddle
from build_rtl import placed_rows, shape
from pins import OP_PUSH, OP_STEP, Host, Tile
from vectors import pin_scenario


class HeddleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = heddle.Model.load()

    def test_model_file_is_ternary_and_sized_for_the_datapath(self):
        dims = shape(self.model)
        self.assertEqual(self.model.rows, dims["R"])
        for pos, neg, c, _, _ in placed_rows(self.model):
            self.assertEqual(pos & neg, 0)
            self.assertLess(max(pos, neg), 1 << dims["W"])
            low, high = c, c + 2 * bin(pos | neg).count("1") + max(self.model.gumbel)
            self.assertTrue(-(1 << dims["VB"] - 1) <= low and high < 1 << dims["VB"] - 1)

    def test_row_value_is_the_signed_ternary_dot_product(self):
        inputs, rows = self.model.layers[0]
        x = self.model.embedding[5] | self.model.embedding[20] << 32 | self.model.embedding[8] << 64
        for pos, neg, c in rows[:16]:
            total = sum(
                (1 if x >> j & 1 else -1) * ((pos >> j & 1) - (neg >> j & 1)) for j in range(inputs)
            )
            self.assertEqual(heddle.row_value((pos, neg, c), x, inputs), total + bin(pos | neg).count("1") + c)

    def test_sampler_has_full_period_structure_and_never_sticks_at_zero(self):
        state, seen = self.model.seed, set()
        for _ in range(4096):
            state = heddle.lfsr_advance(state)
            self.assertNotEqual(state, 0)
            seen.add(state)
        self.assertEqual(len(seen), 4096)
        self.assertEqual(heddle.lfsr_stir(1 << 30, 0), 1)

    def test_greedy_generation_is_deterministic_and_sampling_follows_the_seed(self):
        greedy = heddle.generate(self.model, "the ", 60, sample=False)
        self.assertEqual(greedy, heddle.generate(self.model, "the ", 60, sample=False))
        first = heddle.generate(self.model, "the ", 60, seed_bits=[3])
        self.assertEqual(first, heddle.generate(self.model, "the ", 60, seed_bits=[3]))
        self.assertNotEqual(first, heddle.generate(self.model, "the ", 60, seed_bits=[4]))

    def test_clear_restores_the_power_on_state(self):
        chip = heddle.Chip(self.model)
        opening = [chip.step(True) for _ in range(20)]
        chip.clear()
        self.assertEqual(opening, [chip.step(True) for _ in range(20)])

    def test_host_ports_agree_with_the_reference_and_latency_is_one_clock_per_row(self):
        vectors = pin_scenario(self.model)  # asserts every token internally
        self.assertGreater(len(vectors), 10000)
        host, ref = Host(Tile(self.model)), heddle.Chip(self.model)
        host.reset()
        host.spi(OP_PUSH << 5 | 1)
        while host.uo & 0x40:
            host.tick()
        self.assertEqual(sum(1 for vector in host.vectors if vector[4] & 0x40), self.model.rows)
        self.assertEqual(host.uo & 31, ref.push(1))
        self.assertEqual(host.spi_token(OP_STEP << 5), ref.step())

    def test_training_facts_are_recorded(self):
        facts = json.loads(heddle.MODEL.read_text())["training"]
        self.assertLess(facts["held_out_bits_per_char"], facts["unigram_bits_per_char"])
        self.assertEqual(len(facts["corpus_rev"]), 40)


if __name__ == "__main__":
    unittest.main()
