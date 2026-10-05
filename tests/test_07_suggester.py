"""
This module tests the rule suggester.

Run with::

	python -m pytest tests/test_07_suggester.py -v
"""

from __future__ import annotations

import itertools

from pathspec import PathSpec
from pathspec.suggester import (
	suggest_rules)


def _verify(observed, excluded, rules):
	"""
	Verifies the rules against the observed paths with a real PathSpec.
	"""
	from pathspec.util import normalize_file

	spec = PathSpec.from_lines('gitignore', rules)
	excluded_set = {normalize_file(path) for path in excluded}
	for path in observed:
		norm = normalize_file(path)
		if norm in excluded_set:
			assert spec.match_file(norm), f"rule {rules!r} does not exclude {norm!r}"
		else:
			assert not spec.match_file(norm), f"rule {rules!r} wrongly excludes kept {norm!r}"


def _brute_force(observed, excluded):
	"""
	Brute-force exhaustive set cover over all candidate rules.
	"""
	from pathspec.suggester import _candidate_rules

	excluded_set = set(excluded)
	rules = set()
	for path in excluded:
		for rule, _kind in _candidate_rules(path):
			rules.add(rule)

	valid = []
	for rule in rules:
		spec = PathSpec.from_lines('gitignore', [rule])
		covered = []
		safe = True
		for path in observed:
			if spec.match_file(path):
				if path in excluded_set:
					covered.append(path)
				else:
					safe = False
					break
		if safe and covered:
			valid.append((rule, frozenset(covered)))

	best = None
	for r in range(len(valid) + 1):
		for combo in itertools.combinations(valid, r):
			covered = frozenset().union(*(c[1] for c in combo))
			if covered == excluded_set:
				rules_tuple = tuple(sorted(c[0] for c in combo))
				key = (len(combo), sum(len(c[0]) for c in combo), rules_tuple)
				if best is None or key < best[0]:
					best = (key, rules_tuple)
		if best is not None:
			break

	return best[1] if best is not None else None


def test_six_paths_extension_hurts_kept():
	"""
	Six paths: the same extension in two directories could be covered by one
	``*.txt`` rule, but that would exclude a kept ``.txt`` file, so narrower
	rules must be chosen.
	"""
	observed = [
		'a/x.txt',
		'a/y.txt',
		'b/z.txt',
		'b/keep.txt',
		'c/other.log',
		'README.md',
	]
	excluded = ['a/x.txt', 'a/y.txt', 'b/z.txt']

	result = suggest_rules(observed, excluded)

	assert result.status == 'ok'
	# The broad *.txt rule is rejected because it would exclude b/keep.txt.
	assert '*.txt' not in result.rules
	# The directory rule covers the two excluded files under a/, and the
	# remaining excluded file needs an exact rule.
	assert result.rules == ('a/', 'b/z.txt')
	_verify(observed, excluded, result.rules)


def test_six_paths_extension_safe():
	"""
	When the extension rule is safe (no kept file with that extension), it is
	chosen as the single rule.
	"""
	observed = [
		'a/x.txt',
		'a/y.txt',
		'b/z.txt',
		'c/other.log',
		'README.md',
	]
	excluded = ['a/x.txt', 'a/y.txt', 'b/z.txt']

	result = suggest_rules(observed, excluded)

	assert result.status == 'ok'
	assert result.rules == ('*.txt',)
	_verify(observed, excluded, result.rules)


def test_small_set_matches_exhaustive():
	"""
	For small instances, the suggester result must equal the exhaustive
	brute-force optimum.
	"""
	cases = [
		(
			['a/x.txt', 'a/y.txt', 'b/z.txt', 'b/keep.txt', 'c/other.log', 'README.md'],
			['a/x.txt', 'a/y.txt', 'b/z.txt'],
		),
		(
			['a/x.txt', 'a/y.txt', 'b/z.txt', 'c/other.log'],
			['a/x.txt', 'a/y.txt', 'b/z.txt'],
		),
		(
			['a/x.txt', 'b/y.txt', 'c/z.txt'],
			['a/x.txt', 'b/y.txt', 'c/z.txt'],
		),
		(
			['a/x.txt', 'a/y.txt', 'a/z.txt', 'b/x.txt', 'b/y.txt', 'keep.txt'],
			['a/x.txt', 'a/y.txt', 'a/z.txt', 'b/x.txt', 'b/y.txt'],
		),
		(
			['a/one.log', 'a/two.log', 'b/three.log', 'b/four.log', 'keep.txt'],
			['a/one.log', 'a/two.log', 'b/three.log', 'b/four.log'],
		),
	]

	for observed, excluded in cases:
		result = suggest_rules(observed, excluded)
		assert result.status == 'ok'
		expected = _brute_force(observed, excluded)
		assert expected is not None
		assert set(result.rules) == set(expected), (observed, excluded, result.rules, expected)
		_verify(observed, excluded, result.rules)


def test_over_budget_reports_unknown():
	"""
	When the state budget is exhausted, the result status is ``"unknown"``.
	"""
	observed = [
		'a/x.txt', 'a/y.txt', 'a/z.txt',
		'b/x.txt', 'b/y.txt', 'b/z.txt',
		'c/x.txt', 'c/y.txt', 'c/z.txt',
		'keep.txt',
	]
	excluded = [
		'a/x.txt', 'a/y.txt', 'a/z.txt',
		'b/x.txt', 'b/y.txt', 'b/z.txt',
		'c/x.txt', 'c/y.txt', 'c/z.txt',
	]

	result = suggest_rules(observed, excluded, budget=1)

	assert result.status == 'unknown'
	assert result.rules == ()
	assert result.states > result.budget


def test_no_excluded():
	"""
	No paths to exclude yields an empty rule set.
	"""
	observed = ['a/x.txt', 'b/y.txt']
	result = suggest_rules(observed, [])

	assert result.status == 'ok'
	assert result.rules == ()
	_verify(observed, [], result.rules)


def test_all_excluded():
	"""
	When every observed path is excluded, all candidates are valid.
	"""
	observed = ['a/x.txt', 'a/y.txt', 'b/z.txt']
	excluded = list(observed)

	result = suggest_rules(observed, excluded)

	assert result.status == 'ok'
	# The extension rule covers everything.
	assert result.rules == ('*.txt',)
	_verify(observed, excluded, result.rules)


def test_excluded_must_be_observed():
	"""
	An excluded path that is not observed is rejected as unknown.
	"""
	observed = ['a/x.txt']
	excluded = ['a/x.txt', 'b/y.txt']

	result = suggest_rules(observed, excluded)

	assert result.status == 'unknown'
	assert result.rules == ()


def test_windows_separators():
	"""
	Windows-style path separators are normalized.
	"""
	observed = [r'a\x.txt', r'a\y.txt', r'b\z.txt', r'b\keep.txt']
	excluded = [r'a\x.txt', r'a\y.txt', r'b\z.txt']

	result = suggest_rules(observed, excluded)

	assert result.status == 'ok'
	assert '*.txt' not in result.rules
	_verify(['a/x.txt', 'a/y.txt', 'b/z.txt', 'b/keep.txt'], excluded, result.rules)


def test_result_is_verified_with_real_pathspec():
	"""
	The output rules are re-verified with a real PathSpec, not just the
	internal search.
	"""
	observed = ['a/x.txt', 'a/y.txt', 'b/z.txt', 'b/keep.txt']
	excluded = ['a/x.txt', 'a/y.txt', 'b/z.txt']

	result = suggest_rules(observed, excluded)

	assert result.status == 'ok'
	# Independently verify with a fresh PathSpec.
	spec = PathSpec.from_lines('gitignore', result.rules)
	for path in excluded:
		assert spec.match_file(path)
	for path in observed:
		if path not in set(excluded):
			assert not spec.match_file(path)


def test_random_small_sets_match_exhaustive():
	"""
	Randomized small instances: the suggester result must equal the exhaustive
	brute-force optimum.
	"""
	import random

	rng = random.Random(20261005)
	dirs = ['a', 'b', 'c']
	exts = ['txt', 'log', 'md']

	for _ in range(60):
		observed = []
		for d in dirs:
			for e in exts:
				observed.append(f'{d}/file.{e}')
		# Add a few nested paths.
		observed.append('a/sub/note.txt')
		observed.append('b/sub/note.log')

		# Exclude a random subset (3 to 6 paths).
		n_exclude = rng.randint(3, 6)
		excluded = rng.sample(observed, n_exclude)

		result = suggest_rules(observed, excluded)
		assert result.status == 'ok', (observed, excluded, result)

		expected = _brute_force(observed, excluded)
		assert expected is not None
		assert set(result.rules) == set(expected), (observed, excluded, result.rules, expected)
		_verify(observed, excluded, result.rules)
