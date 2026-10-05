"""
This script tests :class:`.PathSelector`.
"""

import itertools
import random
import unittest

from pathspec import (
	PathSpec)
from pathspec.selector import (
	Candidate,
	PathSelector,
	select_paths)
from pathspec.util import (
	normalize_file)


class PathSelectorTest(unittest.TestCase):
	"""
	The :class:`PathSelectorTest` class tests the :class:`.PathSelector`
	implementation.
	"""

	def test_00_root_quota_two_subdir_one(self):
		"""
		Tests that with a root quota of 2 and each subdirectory quota of 1, the
		selector picks the highest-scoring item from both sides rather than
		filling each directory independently.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2, 'a': 1, 'b': 1}
		candidates = [
			Candidate('a/x', 10),
			Candidate('a/y', 9),
			Candidate('b/z', 8),
			Candidate('b/w', 7),
		]

		result = select_paths(spec, candidates, quotas)

		# The two highest items overall are a/x (10) and b/z (8), one from each
		# subdirectory. Filling each directory independently would also give this,
		# but the key is the root quota of 2 is the binding constraint across both
		# subdirectories.
		self.assertEqual([c.path for c in result.selected], ['a/x', 'b/z'])
		self.assertEqual(result.total_score, 18)

		# The rejected candidates are limited by their respective subdirectory
		# quotas (each already has its one item).
		rejected = {r.candidate.path: r for r in result.rejected}
		self.assertEqual(rejected['a/y'].reason, 'quota')
		self.assertEqual(rejected['a/y'].limited_by, 'a')
		self.assertEqual(rejected['b/w'].reason, 'quota')
		self.assertEqual(rejected['b/w'].limited_by, 'b')

	def test_01_four_candidate_example(self):
		"""
		Tests the four-candidate example: two subdirectories, root quota 2, each
		subdirectory quota 1. The selector must choose the best item from each
		side.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2, 'src': 1, 'docs': 1}
		candidates = [
			Candidate('src/main.py', 5),
			Candidate('src/util.py', 3),
			Candidate('docs/readme.md', 4),
			Candidate('docs/changelog.md', 2),
		]

		result = select_paths(spec, candidates, quotas)

		self.assertEqual(
			[c.path for c in result.selected],
			['src/main.py', 'docs/readme.md'],
		)
		self.assertEqual(result.total_score, 9)

	def test_02_ignore_before_quota(self):
		"""
		Tests that ignored candidates are rejected before quota processing. An
		ignored candidate does not consume any quota.
		"""
		spec = PathSpec.from_lines('gitignore', ['*.log'])
		quotas = {'': 2, 'a': 1}
		candidates = [
			Candidate('a/x.log', 10),  # ignored
			Candidate('a/y.txt', 9),
			Candidate('a/z.log', 8),  # ignored
		]

		result = select_paths(spec, candidates, quotas)

		# The two .log candidates are ignored. Only a/y.txt is eligible and is
		# selected (it does not consume the a quota via an ignored candidate).
		self.assertEqual([c.path for c in result.selected], ['a/y.txt'])
		self.assertEqual(result.total_score, 9)

		rejected = {r.candidate.path: r for r in result.rejected}
		self.assertEqual(rejected['a/x.log'].reason, 'ignore')
		self.assertEqual(rejected['a/z.log'].reason, 'ignore')

	def test_03_duplicate_rejection(self):
		"""
		Tests that duplicate path identities are rejected. The first occurrence is
		kept; subsequent occurrences are rejected before quota processing.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2, 'a': 1}
		candidates = [
			Candidate('a/x', 10),
			Candidate('a/x', 9),  # duplicate
			Candidate('a/y', 8),
		]

		result = select_paths(spec, candidates, quotas)

		# a/x (first) is selected. The duplicate a/x is rejected before quota
		# processing. a/y is then rejected by the 'a' quota (already used by a/x).
		self.assertEqual([c.path for c in result.selected], ['a/x'])
		self.assertEqual(result.total_score, 10)

		rejected = {r.candidate.path: r for r in result.rejected}
		# The duplicate a/x is rejected as a duplicate (before quota).
		self.assertEqual(rejected['a/x'].reason, 'duplicate')
		# a/y is rejected by the 'a' quota.
		self.assertEqual(rejected['a/y'].reason, 'quota')
		self.assertEqual(rejected['a/y'].limited_by, 'a')

	def test_04_segment_distinction(self):
		"""
		Tests that directory ancestry is determined by path segments: ``"a"`` and
		``"ab"`` are not ancestors of each other.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2, 'a': 1}
		candidates = [
			Candidate('a/x', 10),
			Candidate('ab/y', 9),  # not under 'a'; only consumes root quota
		]

		result = select_paths(spec, candidates, quotas)

		# Both are selected: a/x consumes root + a, ab/y consumes root only.
		self.assertEqual([c.path for c in result.selected], ['a/x', 'ab/y'])
		self.assertEqual(result.total_score, 19)

	def test_05_tie_stability(self):
		"""
		Tests that candidates with equal scores are processed in input order
		(stable).
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2, 'a': 1, 'b': 1}
		candidates = [
			Candidate('a/x', 10),
			Candidate('a/y', 10),
			Candidate('b/z', 10),
		]

		result = select_paths(spec, candidates, quotas)

		# All scores are equal. Processed in input order: a/x (add), a/y (a full,
		# reject), b/z (add). The selected set is stable by input order.
		self.assertEqual([c.path for c in result.selected], ['a/x', 'b/z'])
		self.assertEqual(result.total_score, 20)

		rejected = {r.candidate.path: r for r in result.rejected}
		self.assertEqual(rejected['a/y'].reason, 'quota')
		self.assertEqual(rejected['a/y'].limited_by, 'a')

	def test_06_laminar_nesting(self):
		"""
		Tests a nested directory structure where filling each directory
		independently would violate an ancestor quota.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2, 'a': 1, 'a/b': 1}
		candidates = [
			Candidate('a/b/x', 10),
			Candidate('a/b/y', 9),
			Candidate('a/z', 8),
		]

		result = select_paths(spec, candidates, quotas)

		# a/b/x is the highest item. It consumes root + a + a/b. The a quota is
		# then full, so a/z is rejected (limited by 'a'). a/b/y is limited by
		# 'a/b'.
		self.assertEqual([c.path for c in result.selected], ['a/b/x'])
		self.assertEqual(result.total_score, 10)

		rejected = {r.candidate.path: r for r in result.rejected}
		self.assertEqual(rejected['a/b/y'].reason, 'quota')
		self.assertEqual(rejected['a/b/y'].limited_by, 'a/b')
		self.assertEqual(rejected['a/z'].reason, 'quota')
		self.assertEqual(rejected['a/z'].limited_by, 'a')

	def test_07_small_set_enumeration(self):
		"""
		Tests that the greedy result matches brute-force enumeration for small
		random candidate sets.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		random.seed(42)

		for _ in range(300):
			n = random.randint(1, 8)
			paths = []
			for _ in range(n):
				segs = [
					random.choice(['a', 'b', 'c', 'ab', 'x'])
					for _ in range(random.randint(1, 3))
				]
				paths.append('/'.join(segs))
			scores = [random.randint(0, 25) for _ in range(n)]
			candidates = [Candidate(p, s) for p, s in zip(paths, scores)]
			quotas = {
				'': random.choice([1, 2, 3, 4]),
				'a': random.choice([1, 2]),
				'b': random.choice([1, 2]),
				'ab': random.choice([1, 2]),
				'c': random.choice([1, 2]),
			}

			result = select_paths(spec, candidates, quotas)
			expected = self._brute_force(candidates, quotas, spec)
			self.assertEqual(result.total_score, expected, (candidates, quotas))

	def test_08_negative_score_rejected(self):
		"""
		Tests that negative scores are rejected (scores must be non-negative).
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2}
		candidates = [Candidate('a/x', -1)]

		with self.assertRaises(ValueError):
			select_paths(spec, candidates, quotas)

	def test_09_empty_candidates(self):
		"""
		Tests that an empty candidate list returns an empty result.
		"""
		spec = PathSpec.from_lines('gitignore', [])
		quotas = {'': 2}

		result = select_paths(spec, [], quotas)

		self.assertEqual(result.selected, [])
		self.assertEqual(result.rejected, [])
		self.assertEqual(result.total_score, 0)

	def test_10_pathselector_class(self):
		"""
		Tests the :class:`.PathSelector` class directly.
		"""
		spec = PathSpec.from_lines('gitignore', ['*.tmp'])
		quotas = {'': 2, 'src': 1}
		selector = PathSelector(spec, quotas)

		candidates = [
			Candidate('src/a.py', 5),
			Candidate('src/b.py', 3),
			Candidate('src/c.tmp', 4),  # ignored
		]

		result = selector.select(candidates)

		self.assertEqual([c.path for c in result.selected], ['src/a.py'])
		self.assertEqual(result.total_score, 5)

		rejected = {r.candidate.path: r for r in result.rejected}
		self.assertEqual(rejected['src/c.tmp'].reason, 'ignore')
		self.assertEqual(rejected['src/b.py'].reason, 'quota')
		self.assertEqual(rejected['src/b.py'].limited_by, 'src')

	@staticmethod
	def _brute_force(candidates, quotas, spec):
		"""
		Brute-force enumeration of the maximum-total-score legal subset.

		*candidates* is the candidate list.

		*quotas* is the quota mapping.

		*spec* is the path-spec.

		Returns the maximum total score (:class:`int`).
		"""
		# Deduplicate: keep the first occurrence of each path.
		seen = set()
		dedup = []
		for c in candidates:
			np = normalize_file(c.path)
			if np not in seen:
				seen.add(np)
				dedup.append(c)

		from pathspec.selector import (
			_UNLIMITED,
			_ancestor_dirs)

		best = 0
		for mask in range(1 << len(dedup)):
			subset = [dedup[i] for i in range(len(dedup)) if mask & (1 << i)]
			counts = {}
			ok = True
			for c in subset:
				if spec.match_file(c.path):
					ok = False
					break
				for d in _ancestor_dirs(normalize_file(c.path)):
					counts[d] = counts.get(d, 0) + 1
			if not ok:
				continue
			for d, cnt in counts.items():
				if cnt > quotas.get(d, _UNLIMITED):
					ok = False
					break
			if not ok:
				continue
			best = max(best, sum(c.score for c in subset))
		return best


if __name__ == '__main__':
	unittest.main()