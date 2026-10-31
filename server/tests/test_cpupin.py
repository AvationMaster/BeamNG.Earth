"""cpupin: the core split and the loading boost's CPU sets."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpupin  # noqa: E402

CORES = [((0, c), [c, c + 8]) for c in range(8)]     # 8 cores, 16 CPUs, siblings c and c + 8


def fake_siblings(usable=None):
    return [(k, c) for k, c in CORES if usable is None or set(c) <= set(usable)]


# A 13700K: 8 P-cores (CPUs 0-15, SMT pairs) and 8 E-cores (CPUs 16-23, one CPU each)
HYBRID = [((0, i), [2 * i, 2 * i + 1]) for i in range(8)] + [((0, 8 + i), [16 + i]) for i in range(8)]
HYBRID_KINDS = ["p"] * 8 + ["e"] * 8


class BoostTest(unittest.TestCase):
    def test_split_and_boost(self):
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings):
            server, game = cpupin.split()
            self.assertEqual(server, {6, 7, 14, 15})
            b = cpupin.boosted(server, game)
        self.assertTrue(server < b)
        self.assertEqual(b - server, {3, 4, 5, 11, 12, 13})     # the game's last three of six cores, both siblings
        self.assertLessEqual(len(b & game), len(game) // 2)

    def test_boost_from_a_pinned_process(self):
        # the server calls boosted() after pinning itself to its own CPUs: the game's are still found
        with mock.patch.object(cpupin, "_siblings", side_effect=fake_siblings), \
                mock.patch.object(cpupin.os, "sched_getaffinity", return_value={6, 7, 14, 15}, create=True):
            self.assertEqual(cpupin.boosted({6, 7, 14, 15}, {0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13}),
                             {3, 4, 5, 6, 7, 11, 12, 13, 14, 15})

    def test_nothing_to_lend(self):
        self.assertIsNone(cpupin.boosted(None, {1, 2}))
        self.assertIsNone(cpupin.boosted({1}, set()))

    @unittest.skipUnless(hasattr(os, "sched_getaffinity"), "Linux")
    def test_pin_tree_keeps_this_process(self):
        cpus = os.sched_getaffinity(0)
        self.assertGreaterEqual(cpupin.pin_tree(cpus), 1)
        self.assertEqual(os.sched_getaffinity(0), cpus)


if __name__ == "__main__":
    unittest.main()


class HybridSplitTest(unittest.TestCase):
    """Alder Lake and later: the E-cores and 2 P-cores for the server, the rest of the P-cores for the game."""

    def setUp(self):
        self.old = cpupin._siblings, cpupin.kinds
        cpupin._siblings = lambda usable=None: HYBRID
        cpupin.kinds = lambda cores: HYBRID_KINDS

    def tearDown(self):
        cpupin._siblings, cpupin.kinds = self.old
        os.environ.pop("BNGE_SERVER_P_CORES", None)

    def test_e_cores_and_two_p_cores_for_the_server(self):
        server, game = cpupin.split()
        self.assertEqual(server, set(range(12, 24)))      # P6, P7 (both siblings) and all 8 E-cores
        self.assertEqual(game, set(range(12)))            # P0-P5, both siblings

    def test_server_p_cores_setting(self):
        os.environ["BNGE_SERVER_P_CORES"] = "0"          # the E-cores alone
        server, game = cpupin.split()
        self.assertEqual(server, set(range(16, 24)))
        self.assertEqual(game, set(range(16)))
        os.environ["BNGE_SERVER_P_CORES"] = "3"
        self.assertEqual(cpupin.split()[0], set(range(10, 24)))
        os.environ["BNGE_SERVER_P_CORES"] = "99"         # never more than half of the P-cores
        self.assertEqual(cpupin.split()[0], set(range(8, 24)))
        os.environ["BNGE_SERVER_P_CORES"] = "x"
        self.assertEqual(cpupin.split()[0], set(range(12, 24)))

    def test_settings_levels(self):
        # the settings page's three levels (CORE_LEVELS, the server's 'cores' message): fewer P-cores each step
        want = {"max": set(range(12, 24)), "balanced": set(range(14, 24)), "min": set(range(16, 24))}
        for level, server in want.items():
            self.assertEqual(cpupin.split(*cpupin.CORE_LEVELS[level])[0], server, level)
            self.assertTrue(cpupin.split(*cpupin.CORE_LEVELS[level])[1] >= {0, 1, 2, 3, 4, 5, 6, 7}, level)
        self.assertEqual(cpupin.CORE_LEVELS["max"], (cpupin.SERVER_SHARE, cpupin.HYBRID_P_CORES))

    def test_explicit_p_cores_beats_the_env(self):
        os.environ["BNGE_SERVER_P_CORES"] = "3"          # (the settings page's level wins over the start's env var)
        self.assertEqual(cpupin.split(cpupin.SERVER_SHARE, 0)[0], set(range(16, 24)))
        self.assertEqual(cpupin.split(cpupin.SERVER_SHARE, 1)[0], set(range(14, 24)))

    def test_game_keeps_four_p_cores(self):
        # 6 P + 8 E (an i5-13500): 2 P-cores to the server, the other 4 to the game
        cores = [((0, i), [2 * i, 2 * i + 1]) for i in range(6)] + [((0, 6 + i), [12 + i]) for i in range(8)]
        cpupin._siblings = lambda usable=None: cores
        cpupin.kinds = lambda c: ["p"] * 6 + ["e"] * 8
        server, game = cpupin.split()
        self.assertEqual(server, {8, 9, 10, 11} | set(range(12, 20)))
        self.assertEqual(game, set(range(8)))

    def test_one_kind_levels(self):
        cpupin.kinds = lambda cores: None        # a CPU of one kind: the share of the 16 cores, then one core
        self.assertEqual(len(cpupin.split(*cpupin.CORE_LEVELS["max"])[0]), 4)
        self.assertEqual(len(cpupin.split(*cpupin.CORE_LEVELS["balanced"])[0]), 2)
        self.assertEqual(cpupin.split(*cpupin.CORE_LEVELS["min"])[0], {23})     # (this CPU's last core is an E-core)

    def test_one_kind_is_the_last_quarter(self):
        cpupin.kinds = lambda cores: None        # a CPU of one kind: as before, the last quarter of the 16 cores
        server, game = cpupin.split()
        self.assertEqual(server, {20, 21, 22, 23})        # (which on a hybrid machine was the four slowest)
        self.assertEqual(game, set(range(20)))


class KindsTest(unittest.TestCase):
    def test_classes_to_kinds(self):
        cores = cpupin.cores_from_masks([0b11 << (2 * i) for i in range(8)] + [1 << (16 + i) for i in range(8)],
                                        list(range(24)))
        classes = {c: 1 for c in range(16)}
        classes.update({c: 0 for c in range(16, 24)})
        self.assertEqual(cpupin.kinds_from(cores, classes), HYBRID_KINDS)
        self.assertIsNone(cpupin.kinds_from(cores, {c: 0 for c in range(24)}))     # one class: not a hybrid
        self.assertIsNone(cpupin.kinds_from(cores, {0: 1}))                       # a core's class unknown
        self.assertIsNone(cpupin.kinds_from(cores, {}))

    def test_cpu_list(self):
        self.assertEqual(cpupin._cpu_list("0-3,8,12-15\n"), [0, 1, 2, 3, 8, 12, 13, 14, 15])


class WindowsSplitTest(unittest.TestCase):
    """KyzoFox 1.5.56: the split on Windows, from the cores' processor masks (an i5-11400H: 6 cores, 12 threads)."""

    def setUp(self):
        self.masks = [0b11 << (2 * i) for i in range(6)]
        self.old = cpupin._siblings
        cores = cpupin.cores_from_masks(self.masks, list(range(12)))
        cpupin._siblings = lambda usable=None: (cores if usable is None else
                                                cpupin.cores_from_masks(self.masks, usable))

    def tearDown(self):
        cpupin._siblings = self.old
        os.environ.pop("BNGE_SERVER_CORES", None)

    def test_cores_from_masks(self):
        cores = cpupin.cores_from_masks(self.masks, list(range(12)))
        self.assertEqual([c for _k, c in cores], [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11]])
        # a process limited to some CPUs sees only those
        self.assertEqual([c for _k, c in cpupin.cores_from_masks(self.masks, [0, 1, 2])], [[0, 1], [2]])

    def test_two_cores_for_the_server_four_for_the_game(self):
        server, game = cpupin.split()
        self.assertEqual(server, {8, 9, 10, 11})
        self.assertEqual(game, set(range(8)))

    def test_server_cores_setting(self):
        os.environ["BNGE_SERVER_CORES"] = "1"
        self.assertEqual(cpupin.split()[0], {10, 11})
        os.environ["BNGE_SERVER_CORES"] = "5"           # never more than half
        self.assertEqual(len(cpupin.split()[0]), 6)
        os.environ["BNGE_SERVER_CORES"] = "x"
        self.assertEqual(cpupin.split()[0], {8, 9, 10, 11})

    def test_mask(self):
        self.assertEqual(cpupin._mask({8, 9, 10, 11}), 0xF00)
        self.assertEqual(cpupin._bits(0xF00), [8, 9, 10, 11])
