"""
This script tests :mod:`pathspec.select`.

Run only these tests with::

	python -m pytest tests/test_07_select.py -q
"""

import itertools
import random
import unittest

from pathspec import (
	GitIgnoreSpec)
from pathspec.select import (
	Candidate,
	Selection,
	select_paths)


def _segments(path):
	"""
	Splits a POSIX path into segments.
	"""
	return tuple(seg for seg in path.split('/') if seg)


def _is_under(path, directory):
	"""
	Whether *path* lies under *directory* by whole path segments.
	"""
	parts = _segments(path)
	if directory == '':
		return True
	prefix = _segments(directory)
	return parts[:len(prefix)] == prefix


def _brute_force_best(candidates, quotas):
	"""
	Enumerates every subset of *candidates* and returns the feasible subset
	with the highest total priority. Ties are broken by preferring the
	lexicographically smallest tuple of input indexes.
	"""
	indexes = range(len(candidates))
	best_key = None
	best_subset = ()
	for size in range(len(candidates) + 1):
		for subset in itertools.combinations(indexes, size):
			paths = [candidates[i][0] for i in subset]
			feasible = all(
				sum(1 for p in paths if _is_under(p, directory)) <= quota
				for directory, quota in quotas.items()
			)
			if not feasible:
				continue
			total = sum(candidates[i][1] for i in subset)
			key = (total, tuple(-i for i in subset))
			if best_key is None or key > best_key:
				best_key = key
				best_subset = subset
	return best_subset


class SelectPathsTest(unittest.TestCase):
	"""
	Tests :func:`pathspec.select.select_paths`.
	"""

	def test_01_ignore_applies_before_quota(self):
		"""
		Ignored paths are dropped before quota accounting, so they must not
		consume directory slots.
		"""
		spec = GitIgnoreSpec.from_lines(['a/ignored.txt'])
		selections = select_paths(spec, [
			('a/ignored.txt', 100),  # Highest priority, but ignored.
			('a/kept.txt', 10),
			('a/other.txt', 5),
		], {'': 2, 'a': 1})
		self.assertEqual(selections, [
			Selection(path='a/kept.txt', priority=10.0, limited_by='a'),
		])

	def test_02_duplicate_identity_rejected_first(self):
		"""
		Duplicate path identities are rejected before quotas; the first
		occurrence wins.
		"""
		selections = select_paths(None, [
			('a/x.txt', 5),
			('./a/x.txt', 9),  # Same identity after normalization.
			('a//x.txt', 7),  # Same identity after normalization.
			('a/y.txt', 1),
		], {'': 2})
		self.assertEqual(selections, [
			Selection(path='a/x.txt', priority=5.0, limited_by=''),
			Selection(path='a/y.txt', priority=1.0, limited_by=''),
		])

	def test_03_directory_relation_by_segments(self):
		"""
		Directory relations follow path segments: "a" is not an ancestor of
		"ab".
		"""
		selections = select_paths(None, [
			('ab/one.txt', 5),
			('a/two.txt', 4),
			('ab/three.txt', 3),
		], {'a': 1})
		self.assertEqual(selections, [
			Selection(path='ab/one.txt', priority=5.0, limited_by=None),
			Selection(path='a/two.txt', priority=4.0, limited_by='a'),
			Selection(path='ab/three.txt', priority=3.0, limited_by=None),
		])

	def test_04_ties_keep_input_order(self):
		"""
		Equal priorities are selected in input order (stable).
		"""
		selections = select_paths(None, [
			('a/first.txt', 7),
			('b/second.txt', 7),
			('c/third.txt', 7),
		], {'': 2})
		self.assertEqual([s.path for s in selections], [
			'a/first.txt',
			'b/second.txt',
		])

	def test_05_root_quota_limits_across_directories(self):
		"""
		Root quota 2 with one slot per subdirectory must pick the two highest
		items overall, not fill each directory independently (which would
		accept three).
		"""
		selections = select_paths(None, [
			('a/top.txt', 10),
			('b/top.txt', 9),
			('c/top.txt', 8),
		], {'': 2, 'a': 1, 'b': 1, 'c': 1})
		self.assertEqual([s.path for s in selections], [
			'a/top.txt',
			'b/top.txt',
		])
		# Each directory had its own slot, but the root quota caps the total.
		self.assertEqual(len(selections), 2)

	def test_06_four_candidate_example(self):
		"""
		The four-candidate example from the module docstring.
		"""
		selections = select_paths(None, [
			('a/x.txt', 10),
			('a/y.txt', 9),
			('b/z.txt', 8),
			('c/w.txt', 7),
		], {'': 2, 'a': 1, 'b': 1})
		self.assertEqual(selections, [
			Selection(path='a/x.txt', priority=10.0, limited_by='a'),
			Selection(path='b/z.txt', priority=8.0, limited_by='b'),
		])

	def test_07_reports_limiting_quota(self):
		"""
		Each selection reports the applicable quota left with the least
		remaining capacity (deepest on ties).
		"""
		selections = select_paths(None, [
			('a/b/deep.txt', 10),  # Saturates "a/b" (0 left), root has 1 left.
			('a/flat.txt', 5),  # Blocked: "a" is exhausted by the deep pick.
			('loose.txt', 1),  # Only the root quota applies.
		], {'': 2, 'a': 1, 'a/b': 1})
		self.assertEqual(selections, [
			Selection(path='a/b/deep.txt', priority=10.0, limited_by='a/b'),
			Selection(path='loose.txt', priority=1.0, limited_by=''),
		])

	def test_08_candidate_dataclass_input(self):
		"""
		:class:`Candidate` instances are accepted alongside tuples.
		"""
		selections = select_paths(None, [
			Candidate(path='a/x.txt', priority=3),
			('a/y.txt', 2),
		], {'a': 2})
		self.assertEqual([s.path for s in selections], [
			'a/x.txt',
			'a/y.txt',
		])

	def test_09_validation(self):
		"""
		Negative priorities, negative quotas, and ``..`` segments are
		rejected.
		"""
		with self.assertRaises(ValueError):
			select_paths(None, [('a/x.txt', -1)], {})
		with self.assertRaises(ValueError):
			select_paths(None, [('a/x.txt', 1)], {'a': -1})
		with self.assertRaises(ValueError):
			select_paths(None, [('a/../x.txt', 1)], {})

	def test_10_matches_brute_force(self):
		"""
		The greedy selection matches exhaustive enumeration on small random
		fixtures (distinct priorities make the optimum unique).
		"""
		rng = random.Random(20261005)
		dirs = ['', 'a', 'b', 'a/x', 'ab', 'c']
		for trial in range(50):
			count = rng.randint(2, 8)
			priorities = rng.sample(range(1, 100), count)
			candidates = [
				(f"{rng.choice(dirs)}/f{i}.txt".lstrip('/'), priorities[i])
				for i in range(count)
			]
			# Deduplicate paths to keep identities unique in this fixture.
			seen = set()
			unique = []
			for path, priority in candidates:
				if path not in seen:
					seen.add(path)
					unique.append((path, priority))
			candidates = unique

			quota_dirs = rng.sample(dirs, rng.randint(1, len(dirs)))
			quotas = {d: rng.randint(0, 3) for d in quota_dirs}

			selections = select_paths(None, candidates, quotas)
			selected_paths = {s.path for s in selections}

			# The selection itself must be feasible.
			for directory, quota in quotas.items():
				under = sum(1 for p in selected_paths if _is_under(p, directory))
				self.assertLessEqual(under, quota, (trial, directory))

			# Compare against exhaustive enumeration.
			best_subset = _brute_force_best(candidates, quotas)
			best_paths = {candidates[i][0] for i in best_subset}
			self.assertEqual(selected_paths, best_paths, (trial, candidates, quotas))


if __name__ == '__main__':
	unittest.main()
