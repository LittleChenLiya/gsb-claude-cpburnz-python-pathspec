"""
This module provides a rule suggester based on :class:`.PathSpec`.

Given a finite set of *observed* paths and a subset of them to exclude,
:func:`.suggest_rules` selects the minimum number of gitignore-style rules
from three candidate kinds:

-	*exact* rules (e.g., ``"a/b/c.txt"``) matching a single path,
-	*directory* rules (e.g., ``"a/"``) matching every path under a directory,
-	*extension* rules (e.g., ``"*.txt"``) matching every path with an
	extension.

A candidate rule is only accepted if it excludes no path that should be kept
(this is verified against the observed paths using a real :class:`.PathSpec`).
The search for the minimum rule set is a subset dynamic program with an
explicit state budget; if the budget is exhausted, the result status is
``"unknown"``.

.. warning::

	Safety is only promised for *observed* paths. Unobserved paths are not
	promised to be safe: a rule that is safe on the observed set may still
	exclude an unobserved path that should be kept.

All output rules are re-verified with a real :class:`.PathSpec` before being
returned.
"""

from __future__ import annotations

import heapq
from collections.abc import (
	Iterable)
from dataclasses import (
	dataclass)

from pathspec import (
	PathSpec)
from pathspec.util import (
	normalize_file)

__all__ = [
	'Suggestion',
	'suggest_rules',
]

_DEFAULT_BUDGET = 10_000
"""
The default state budget for the search.
"""


@dataclass(frozen=True)
class Candidate:
	"""
	A candidate rule.
	"""

	rule: str
	"""
	*rule* (:class:`str`) is the uncompiled gitignore-style rule.
	"""

	kind: str
	"""
	*kind* (:class:`str`) is the candidate kind: ``"exact"``, ``"directory"``,
	or ``"extension"``.
	"""

	coverage: int
	"""
	*coverage* (:class:`int`) is the bitmask of excluded paths this rule
	matches.
	"""

	length: int
	"""
	*length* (:class:`int`) is the length of :attr:`rule`.
	"""


@dataclass(frozen=True)
class Suggestion:
	"""
	The result of :func:`.suggest_rules`.
	"""

	status: str
	"""
	*status* (:class:`str`) is ``"ok"`` if a verified rule set was found, or
	``"unknown"`` if the search budget was exhausted or verification failed.
	"""

	rules: tuple[str, ...]
	"""
	*rules* (:class:`tuple` of :class:`str`) is the selected rules in stable
	(lexicographic) order.
	"""

	states: int
	"""
	*states* (:class:`int`) is the number of search states expanded.
	"""

	budget: int
	"""
	*budget* (:class:`int`) is the state budget used.
	"""

	observed_count: int
	"""
	*observed_count* (:class:`int`) is the number of observed paths.
	"""

	excluded_count: int
	"""
	*excluded_count* (:class:`int`) is the number of paths to exclude.
	"""


def _candidate_rules(path: str) -> list[tuple[str, str]]:
	"""
	Generates the candidate rules for an excluded path.

	*path* (:class:`str`) is the normalized path.

	Returns the candidate ``(rule, kind)`` pairs.
	"""
	candidates: list[tuple[str, str]] = []

	# Exact rule.
	candidates.append((path, 'exact'))

	# Directory rules: one per directory component.
	parts = path.split('/')
	seen_dirs: set[str] = set()
	for i in range(len(parts) - 1):
		dir_rule = '/'.join(parts[:i + 1]) + '/'
		if dir_rule not in seen_dirs:
			seen_dirs.add(dir_rule)
			candidates.append((dir_rule, 'directory'))

	# Extension rule.
	basename = parts[-1]
	dot = basename.rfind('.')
	if dot > 0:
		ext = basename[dot + 1:]
		if ext:
			candidates.append(('*.' + ext, 'extension'))

	return candidates


def _build_candidates(
	observed: list[str],
	excluded: list[str],
	excluded_set: set[str],
) -> list[Candidate]:
	"""
	Builds the valid, pruned candidates.

	*observed* (:class:`list` of :class:`str`) is the normalized observed paths.

	*excluded* (:class:`list` of :class:`str`) is the normalized paths to
	exclude.

	*excluded_set* (:class:`set` of :class:`str`) is the set of excluded paths.

	Returns the valid candidates.
	"""
	# Generate raw candidates.
	raw: dict[str, str] = {}
	for path in excluded:
		for rule, kind in _candidate_rules(path):
			raw.setdefault(rule, kind)

	rules = list(raw)
	if not rules:
		return []

	# Compute coverage and validity with a real PathSpec per candidate. Each
	# candidate is checked against every observed path: it is valid only if it
	# matches no kept path, and its coverage is the bitmask of excluded paths
	# it matches.
	excluded_index = {path: i for i, path in enumerate(excluded)}
	candidates: list[Candidate] = []
	for rule in rules:
		spec = PathSpec.from_lines('gitignore', [rule])
		coverage = 0
		valid = True
		for path in observed:
			if spec.match_file(path):
				if path in excluded_set:
					coverage |= 1 << excluded_index[path]
				else:
					# This would exclude a path that must be kept.
					valid = False
					break
		if valid and coverage:
			candidates.append(Candidate(rule=rule, kind=raw[rule], coverage=coverage, length=len(rule)))

	# Prune candidates with identical coverage: keep the shortest, then the
	# lexicographically smallest (stable order).
	best_by_coverage: dict[int, Candidate] = {}
	for candidate in candidates:
		existing = best_by_coverage.get(candidate.coverage)
		if existing is None or (candidate.length, candidate.rule) < (existing.length, existing.rule):
			best_by_coverage[candidate.coverage] = candidate
	candidates = list(best_by_coverage.values())

	# Prune dominated candidates: a candidate whose coverage is a superset and
	# whose rule is no longer/lexicographically smaller makes the other one
	# redundant.
	pruned: list[Candidate] = []
	for candidate in candidates:
		dominated = False
		for other in candidates:
			if other is candidate:
				continue
			if (other.coverage & candidate.coverage) == candidate.coverage and (
				(other.length, other.rule) <= (candidate.length, candidate.rule)
			):
				dominated = True
				break
		if not dominated:
			pruned.append(candidate)

	return pruned


def _search(
	candidates: list[Candidate],
	excluded_count: int,
	budget: int,
) -> tuple[str, tuple[str, ...], int]:
	"""
	Searches for the minimum rule set.

	*candidates* (:class:`list` of :class:`Candidate`) are the valid pruned
	candidates.

	*excluded_count* (:class:`int`) is the number of paths to exclude.

	*budget* (:class:`int`) is the state budget.

	Returns the status, rules, and number of states expanded.
	"""
	full = (1 << excluded_count) - 1

	# dp[mask] = (rule count, total length, sorted rules)
	dp: dict[int, tuple[int, int, tuple[str, ...]]] = {0: (0, 0, ())}
	processed: set[int] = set()
	heap: list[tuple[int, int]] = [(0, 0)]  # (popcount, mask)
	states = 0

	while heap:
		_popcount, mask = heapq.heappop(heap)
		if mask in processed:
			continue
		processed.add(mask)

		states += 1
		if states > budget:
			return 'unknown', (), states

		if mask == full:
			break

		base_count, base_length, base_rules = dp[mask]
		for candidate in candidates:
			new_mask = mask | candidate.coverage
			if new_mask == mask:
				continue

			solution = (
				base_count + 1,
				base_length + candidate.length,
				tuple(sorted(base_rules + (candidate.rule,))),
			)
			existing = dp.get(new_mask)
			if existing is None or solution < existing:
				dp[new_mask] = solution
				heapq.heappush(heap, (new_mask.bit_count(), new_mask))

	if full not in dp:
		return 'unknown', (), states

	_solution_count, _solution_length, rules = dp[full]
	return 'ok', rules, states


def suggest_rules(
	observed_paths: Iterable[str],
	exclude_paths: Iterable[str],
	*,
	budget: int = _DEFAULT_BUDGET,
) -> Suggestion:
	"""
	Suggests the minimum set of rules that exclude every path to exclude
	without excluding any path that must be kept.

	*observed_paths* (:class:`~collections.abc.Iterable` of :class:`str`)
	contains the finite set of observed paths. Safety is only promised for
	these paths.

	*exclude_paths* (:class:`~collections.abc.Iterable` of :class:`str`)
	contains the paths to exclude. Each must be an observed path.

	*budget* (:class:`int`) is the maximum number of search states to expand.
	If exceeded, the result status is ``"unknown"``. Default is ``10_000``.

	Returns the :class:`Suggestion`.
	"""
	observed = list(dict.fromkeys(
		normalize_file(path) for path in observed_paths
	))
	excluded = list(dict.fromkeys(
		normalize_file(path) for path in exclude_paths
	))
	observed_set = set(observed)
	excluded_set = set(excluded)

	unknown = Suggestion(
		status='unknown',
		rules=(),
		states=0,
		budget=budget,
		observed_count=len(observed),
		excluded_count=len(excluded),
	)

	if not excluded:
		return Suggestion(
			status='ok',
			rules=(),
			states=0,
			budget=budget,
			observed_count=len(observed),
			excluded_count=0,
		)

	if not excluded_set <= observed_set:
		return unknown

	candidates = _build_candidates(observed, excluded, excluded_set)
	if not candidates:
		return unknown

	status, rules, states = _search(candidates, len(excluded), budget)
	if status != 'ok':
		return Suggestion(
			status='unknown',
			rules=(),
			states=states,
			budget=budget,
			observed_count=len(observed),
			excluded_count=len(excluded),
		)

	# Re-verify the output rules with a real PathSpec.
	spec = PathSpec.from_lines('gitignore', rules)
	for path in excluded:
		if not spec.match_file(path):
			return unknown
	for path in observed:
		if path not in excluded_set and spec.match_file(path):
			return unknown

	return Suggestion(
		status='ok',
		rules=rules,
		states=states,
		budget=budget,
		observed_count=len(observed),
		excluded_count=len(excluded),
	)
