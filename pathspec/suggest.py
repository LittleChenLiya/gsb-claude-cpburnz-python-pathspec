"""
The *pathspec.suggest* module provides a rule suggester.

Given a finite set of observed file paths and the subset of them that should
be excluded, :func:`suggest_rules` searches for the smallest set of
``.gitignore`` style rules that excludes every unwanted path while never
excluding a path that must be kept.

Only three kinds of candidates are considered:

-	exact path rules (``/some/file.ext``);
-	directory rules (``/some/dir/``);
-	extension rules (``*.ext``).

The suggester only reasons about the finite set of observed paths it is given.
It never promises that a suggested rule is safe for paths that were not
observed.  Every rule set that is returned as an optimal solution is
re-verified against the observed paths with a real :class:`.PathSpec`
instance (see :func:`verify_rules`).
"""

from __future__ import annotations

from dataclasses import (
	dataclass,
	field)
from typing import (
	Final)

from .gitignore import (
	GitIgnoreSpec)

__all__ = [
	'EXHAUSTED',
	'OPTIMAL',
	'Suggestion',
	'suggest_rules',
	'verify_rules']

#: Status returned when the search proved the returned rules optimal.
OPTIMAL: Final = "optimal"

#: Status returned when the state budget was exceeded before optimality could
#: be proven.  The returned rules are the best feasible rules found so far,
#: or ``None`` if no feasible rule set was found.
EXHAUSTED: Final = "unknown"

#: Default state (search node) budget.
DEFAULT_STATE_BUDGET: Final = 10_000


def _normalize(path: str) -> str:
	"""
	Normalize an observed *path*.

	Backslashes are converted to forward slashes, leading slashes and
	duplicate separators are removed, and trailing slashes are stripped.
	"""
	normalized = str(path).replace("\\", "/")
	while "//" in normalized:
		normalized = normalized.replace("//", "/")
	normalized = normalized.lstrip("/")
	return normalized.rstrip("/")


def _split_components(path: str) -> list[str]:
	"""
	Split a normalized *path* into its path components.
	"""
	return [component for component in path.split("/") if component]


def _extension(path: str) -> "str | None":
	"""
	Return the extension (without the dot) of the base name of *path*, or
	``None`` when the base name has no extension.
	"""
	base = _split_components(path)[-1]
	index = base.rfind(".")
	if index <= 0 or index == len(base) - 1:
		# No dot, a leading dot only (".bashrc"), or a trailing dot.
		return None
	return base[index + 1:]


def _make_candidates(
	excluded: "tuple[str, ...]",
	kept: "tuple[str, ...]") -> "tuple[str, ...]":
	"""
	Build the de-duplicated, stable-ordered tuple of candidate rules.

	Candidates that exclude any observed path that must be kept are dropped
	up front because they can never be part of a feasible solution.
	"""
	patterns: "set[str]" = set()

	for path in excluded:
		# Exact path rule, anchored to the repository root so no wildcard
		# interpretation is possible.
		patterns.add(f"/{path}")

		components = _split_components(path)
		# Directory rules for every ancestor directory of an excluded path.
		for end in range(1, len(components)):
			patterns.add("/" + "/".join(components[:end]) + "/")

		extension = _extension(path)
		if extension is not None:
			# Extension rule matching the base name at any depth.
			patterns.add(f"*.{extension}")

	ordered = sorted(patterns)

	if not kept:
		return tuple(ordered)

	# A candidate is admissible only when it excludes none of the kept
	# paths.  Matching is decided with a real PathSpec.
	admissible: "list[str]" = []
	for pattern in ordered:
		spec = GitIgnoreSpec.from_lines([pattern])
		if not any(spec.match_file(path) for path in kept):
			admissible.append(pattern)
	return tuple(admissible)


class Suggestion:
	"""
	Result of :func:`suggest_rules`.

	Attributes:
		rules: The suggested gitignore rules in stable (lexicographic)
			order, or ``None`` when no feasible rule set was found within
			the state budget.
		status: Either :data:`OPTIMAL` when the result was proven optimal,
			or :data:`EXHAUSTED` (``"unknown"``) when the state budget was
			exceeded before optimality could be proven.
		states_used: Number of search states visited.
		budget: Configured state budget.
		excluded: Normalized observed paths that must be excluded.
		kept: Normalized observed paths that must be retained.
	"""

	def __init__(
			self,
			rules,
			status,
			states_used,
			budget,
			excluded,
			kept=()):
		self.rules = rules
		self.status = status
		self.states_used = states_used
		self.budget = budget
		self.excluded = excluded
		self.kept = tuple(kept)

	@property
	def is_optimal(self) -> bool:
		"""
		Return whether the result was proven optimal.
		"""
		return self.status == OPTIMAL

	@property
	def is_complete(self) -> bool:
		"""
		Return whether the rules cover every excluded observed path and
		none of the kept observed paths, as re-checked with PathSpec.
		"""
		if self.rules is None:
			return False
		return verify_rules(self.rules, self.excluded, self.kept)

	def __eq__(self, other) -> bool:
		if not isinstance(other, Suggestion):
			return NotImplemented
		return (
			self.rules == other.rules
			and self.status == other.status
			and self.states_used == other.states_used
			and self.budget == other.budget
			and self.excluded == other.excluded
			and self.kept == other.kept)

	def __repr__(self) -> str:
		return (
			f"Suggestion(rules={self.rules!r}, status={self.status!r}, "
			f"states_used={self.states_used!r}, budget={self.budget!r})")


def _cost(rules: "tuple[str, ...]"):
	"""
	Tie-breaking cost of a feasible rule set: rule count, total pattern
	length, then the patterns themselves in lexicographic order.
	"""
	return len(rules), sum(len(rule) for rule in rules), rules


def suggest_rules(excluded, kept=(), *, budget: int = DEFAULT_STATE_BUDGET):
	"""
	Suggest the fewest gitignore rules covering the *excluded* paths.

	Args:
		excluded: Iterable of observed paths that should be excluded.
		kept: Iterable of observed paths that must be retained.  Paths
			present in both sets are treated as kept.
		budget: Maximum number of search states to visit.  When the budget
			is exceeded the search stops and reports status
			:data:`EXHAUSTED` (``"unknown"``); the best feasible rules
			found so far (possibly ``None``) are still returned.

	Returns:
		A :class:`Suggestion`.

	The search only guarantees correctness for the finite set of observed
	paths; unobserved paths are never promised to be safe.  An optimal
	result is always re-verified against the observed paths with a real
	:class:`.PathSpec`.
	"""
	kept_set = {_normalize(path) for path in kept}
	excluded_list = sorted({
		_normalize(path)
		for path in excluded
		if _normalize(path) not in kept_set
	})

	excluded_tuple = tuple(excluded_list)
	kept_tuple = tuple(sorted(kept_set))

	if not excluded_tuple:
		return Suggestion(
			rules=(),
			status=OPTIMAL,
			states_used=0,
			budget=budget,
			excluded=excluded_tuple,
			kept=kept_tuple)

	candidates = _make_candidates(excluded_tuple, kept_tuple)
	num_targets = len(excluded_tuple)

	# Coverage bitmask of every admissible candidate over the excluded
	# observed paths.  Matching is performed with a real PathSpec.
	coverage: "dict[str, int]" = {}
	for pattern in candidates:
		spec = GitIgnoreSpec.from_lines([pattern])
		mask = 0
		for index, path in enumerate(excluded_tuple):
			if spec.match_file(path):
				mask |= 1 << index
		if mask:
			coverage[pattern] = mask

	# Order candidates by descending coverage then pattern, so strong rules
	# are explored first and the incumbent improves quickly.
	ordered = sorted(
		coverage,
		key=lambda pattern: (-coverage[pattern].bit_count(), pattern))

	# For each excluded target, the ordered indices of candidates covering
	# it.
	coverers: "list[list[int]]" = [[] for _ in range(num_targets)]
	for cand_index, pattern in enumerate(ordered):
		mask = coverage[pattern]
		for target in range(num_targets):
			if mask & (1 << target):
				coverers[target].append(cand_index)

	states_used = 0
	over_budget = False

	# Incumbent feasible solution.
	best_rules = None
	best_cost = None

	def search(covered: int, chosen: "tuple[int, ...]") -> None:
		nonlocal states_used, over_budget
		nonlocal best_rules, best_cost

		if over_budget:
			return

		states_used += 1
		if states_used > budget:
			over_budget = True
			return

		all_targets = (1 << num_targets) - 1
		if covered == all_targets:
			rules = tuple(sorted(ordered[index] for index in chosen))
			cost = _cost(rules)
			if best_cost is None or cost < best_cost:
				best_rules = rules
				best_cost = cost
			return

		# Lower bound on the number of additional rules needed.
		remaining = all_targets ^ covered
		max_gain = 0
		for pattern in ordered:
			gain = (coverage[pattern] & remaining).bit_count()
			if gain > max_gain:
				max_gain = gain
		if max_gain:
			lower_bound = (
				(remaining.bit_count() + max_gain - 1) // max_gain)
		else:
			# Exact rules always exist, so this branch is unreachable in
			# practice; keep a safe bound for defensive purposes.
			lower_bound = remaining.bit_count()
		# Only branches that would strictly exceed the incumbent's rule
		# count can be pruned: equal-count branches may still win on the
		# total-length / stable-order tie-breakers.
		if best_cost is not None and len(chosen) + lower_bound > best_cost[0]:
			return

		# Branch on the uncovered target with the fewest covering
		# candidates (fail-fast set-cover branching).
		target = min(
			(
				index
				for index in range(num_targets)
				if not (covered & (1 << index))),
			key=lambda index: len(coverers[index]))

		chosen_set = set(chosen)
		for cand_index in coverers[target]:
			if cand_index in chosen_set:
				continue
			search(
				covered | coverage[ordered[cand_index]],
				chosen + (cand_index,))
			if over_budget:
				return

	search(0, ())

	if over_budget:
		return Suggestion(
			rules=best_rules,
			status=EXHAUSTED,
			states_used=states_used,
			budget=budget,
			excluded=excluded_tuple,
			kept=kept_tuple)

	# The search was exhaustive, so an incumbent must exist (exact rules
	# always cover each excluded path).  Re-verify with a real PathSpec.
	assert best_rules is not None
	assert verify_rules(best_rules, excluded_tuple, kept_tuple)

	return Suggestion(
		rules=best_rules,
		status=OPTIMAL,
		states_used=states_used,
		budget=budget,
		excluded=excluded_tuple,
		kept=kept_tuple)


def verify_rules(rules, excluded, kept=()) -> bool:
	"""
	Re-verify *rules* with a real :class:`.PathSpec`.

	Return ``True`` only when every path in *excluded* is matched and no
	path in *kept* is matched.  This checks the observed paths exclusively;
	it makes no claim about unobserved paths.
	"""
	spec = GitIgnoreSpec.from_lines(rules)
	excluded_norm = [_normalize(path) for path in excluded]
	kept_norm = [_normalize(path) for path in kept]
	return (
		all(spec.match_file(path) for path in excluded_norm)
		and not any(spec.match_file(path) for path in kept_norm))
