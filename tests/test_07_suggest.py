"""
This script tests :mod:`pathspec.suggest`, the rule suggester.

Run only these acceptance tests with::

	python -m pytest tests/test_07_suggest.py -v
"""

import itertools
import unittest

from pathspec.gitignore import GitIgnoreSpec
from pathspec.suggest import (
	DEFAULT_STATE_BUDGET,
	EXHAUSTED,
	OPTIMAL,
	suggest_rules,
	verify_rules)

#: The six-path example used for acceptance.
SIX_PATHS = [
	"build/a.o",
	"build/b.o",
	"build/c.o",
	"src/x.o",
	"src/y.c",
	"doc/readme.md",
]


class SuggestRulesTest(unittest.TestCase):
	"""
	Tests :func:`pathspec.suggest.suggest_rules`.
	"""

	def test_extension_covers_same_extension_in_two_dirs(self):
		"""
		One extension rule covers same-extension paths in two directories.
		"""
		excluded = ["build/a.o", "src/x.o"]
		kept = ["src/y.c", "doc/readme.md"]
		result = suggest_rules(excluded, kept)

		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ("*.o",))
		self.assertTrue(result.is_optimal)
		self.assertTrue(result.is_complete)
		self.assertTrue(verify_rules(result.rules, excluded, kept))

	def test_six_path_example_extension_hurts_keep(self):
		"""
		With all six paths observed, ``*.o`` would exclude ``src/x.o``
		which must be kept, so the narrower directory rule must win.
		"""
		excluded = ["build/a.o", "build/b.o", "build/c.o"]
		kept = ["src/x.o", "src/y.c", "doc/readme.md"]
		result = suggest_rules(excluded, kept)

		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ("/build/",))
		self.assertNotIn("*.o", result.rules)
		self.assertTrue(verify_rules(result.rules, excluded, kept))

		# Every build artifact is matched, the kept object file is not.
		spec = GitIgnoreSpec.from_lines(result.rules)
		self.assertTrue(all(spec.match_file(p) for p in SIX_PATHS[:3]))
		self.assertFalse(any(spec.match_file(p) for p in SIX_PATHS[3:]))

	def test_six_path_example_extension_safe(self):
		"""
		The same six paths with the source object files excluded too can
		be covered by a single extension rule.
		"""
		excluded = ["build/a.o", "build/b.o", "build/c.o", "src/x.o"]
		kept = ["src/y.c", "doc/readme.md"]
		result = suggest_rules(excluded, kept)

		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ("*.o",))
		self.assertTrue(verify_rules(result.rules, excluded, kept))

	def test_falls_back_to_exact_rules(self):
		"""
		When no directory or extension rule is safe, exact rules are used.
		"""
		excluded = ["dir/a.log", "dir/b.log"]
		kept = ["dir/c.log"]
		result = suggest_rules(excluded, kept)

		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ("/dir/a.log", "/dir/b.log"))
		self.assertTrue(verify_rules(result.rules, excluded, kept))

	def test_tie_break_length_and_stable_order(self):
		"""
		Equal-size, equal-coverage solutions prefer shorter total length
		and a stable lexicographic order.
		"""
		excluded = ["a/x.tmp", "b/x.tmp"]
		result = suggest_rules(excluded)

		self.assertEqual(result.status, OPTIMAL)
		# Both exact rules and the extension rule solve it in one rule;
		# the extension pattern is shortest.
		self.assertEqual(result.rules, ("*.tmp",))

	def test_paths_present_in_both_sets_are_kept(self):
		"""
		Kept paths take precedence when listed in both sets.
		"""
		result = suggest_rules(["a.o", "b.o"], ["a.o"])
		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ("/b.o",))

	def test_empty_excluded(self):
		"""
		Nothing to exclude yields no rules and no search states.
		"""
		result = suggest_rules([], ["a.o"])
		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ())
		self.assertEqual(result.states_used, 0)

	def test_windows_separators_normalized(self):
		"""
		Backslash separators are normalized like forward slashes.
		"""
		result = suggest_rules([r"build\a.o", r"build\b.o"], [r"src\x.o"])
		self.assertEqual(result.rules, ("/build/",))
		self.assertTrue(
			verify_rules(result.rules, [r"build\a.o"], [r"src\x.o"]))

	def test_budget_exceeded_reports_unknown(self):
		"""
		When the state budget is exhausted, status must be ``unknown``.
		"""
		excluded = [f"dir{i}/f{i}.dat" for i in range(12)]
		kept = [f"dir{i}/keep{i}.dat" for i in range(12)]
		result = suggest_rules(excluded, kept, budget=1)

		self.assertEqual(result.status, EXHAUSTED)
		self.assertFalse(result.is_optimal)
		self.assertGreater(result.states_used, result.budget)
		# Any returned best-effort rules must still be feasible.
		if result.rules is not None:
			self.assertTrue(
				verify_rules(result.rules, result.excluded, result.kept))

	def test_tiny_budget_unknown_can_still_return_best_effort(self):
		"""
		A budget of a single state cannot prove optimality; the status is
		unknown even though no complete solution is returned yet.
		"""
		result = suggest_rules(["a.o", "b.o"], budget=1)
		self.assertEqual(result.status, EXHAUSTED)
		self.assertEqual(result.rules, None)


class ExhaustiveEquivalenceTest(unittest.TestCase):
	"""
	The budgeted search must agree with brute-force enumeration on small
	instances ("small set agrees with exhaustive search").
	"""

	@staticmethod
	def _brute_force(excluded, kept):
		"""
		Enumerate every feasible candidate combination to find the true
		minimum rule set, using the same tie-breakers.
		"""
		# Build the same candidate set as the suggester.
		from pathspec.suggest import _make_candidates

		candidates = _make_candidates(tuple(excluded), tuple(kept))

		# Precompute each candidate's coverage with a real PathSpec.
		all_targets = (1 << len(excluded)) - 1
		masks = []
		for candidate in candidates:
			spec = GitIgnoreSpec.from_lines([candidate])
			mask = 0
			for index, path in enumerate(excluded):
				if spec.match_file(path):
					mask |= 1 << index
			masks.append(mask)

		best = None
		for size in range(1, len(candidates) + 1):
			for indexes in itertools.combinations(range(len(candidates)), size):
				covered = 0
				for index in indexes:
					covered |= masks[index]
				if covered != all_targets:
					continue
				combination = tuple(candidates[index] for index in indexes)
				cost = (
					len(combination),
					sum(len(rule) for rule in combination),
					combination)
				if best is None or cost < best[0]:
					best = (cost, combination)
			if best is not None:
				return best[1]
		return None

	def test_small_instances_match_brute_force(self):
		"""
		Compare the suggester against exhaustive enumeration for many
		small random-ish path universes.
		"""
		names = [
			"a.o", "b.o", "a.c", "b.c", "x.log", "y",
			"sub/a.o", "sub/b.c", "tmp/a.log",
		]

		# Sample subsets; full power-set of 9 would be large, so use a
		# deterministic spread of subset sizes.
		scenarios = 0
		for mask in range(1, 1 << len(names)):
			paths = [names[i] for i in range(len(names)) if mask & (1 << i)]
			if len(paths) > 5:
				continue
			for keep_mask in range(0, 1 << len(paths)):
				kept = [paths[i] for i in range(len(paths)) if keep_mask & (1 << i)]
				excluded = [p for p in paths if p not in kept]
				if not excluded:
					continue
				result = suggest_rules(excluded, kept, budget=DEFAULT_STATE_BUDGET)
				self.assertEqual(result.status, OPTIMAL, (excluded, kept))
				expected = self._brute_force(result.excluded, result.kept)
				self.assertEqual(
					result.rules, expected,
					msg=f"excluded={excluded}, kept={kept}")
				scenarios += 1
		self.assertGreater(scenarios, 500)

	def test_budget_large_enough_is_exhaustive(self):
		"""
		With a generous budget the search proves optimality.
		"""
		result = suggest_rules(
			[f"d{i}/f{i}.o" for i in range(8)],
			[f"d{i}/k{i}.c" for i in range(8)],
			budget=100_000)
		self.assertEqual(result.status, OPTIMAL)
		self.assertEqual(result.rules, ("*.o",))


if __name__ == "__main__":
	unittest.main()
