"""
This module provides :class:`.PathSelector` which selects the highest-scoring
legal subset of path candidates subject to per-directory count quotas.

The candidates are first filtered by a :class:`~pathspec.PathSpec` (ignored
paths are rejected), duplicate path identities are rejected, and then the
remaining candidates are selected to maximize the total priority score while
respecting the quotas of every directory and its ancestors.

Directory ancestry is determined by path segments (e.g., ``"a"`` and ``"ab"``
are not ancestors of each other). Selecting a candidate at path ``p`` consumes
one quota from every ancestor directory of ``p``.

The quota constraints form a *laminar matroid*, so the optimal set is found by a
stable greedy: process candidates in descending score order, breaking ties by
ascending input order, and add a candidate whenever it does not violate any
quota. See https://en.wikipedia.org/wiki/Laminar_matroid for more information.
"""
from __future__ import annotations

from collections.abc import (
	Iterable,
	Mapping)
from dataclasses import (
	dataclass,
	field)
from typing import (
	Optional)

from pathspec.pathspec import (
	PathSpec)
from pathspec.util import (
	normalize_file)

# A quota value meaning "unlimited". Used for directories not present in the
# quotas mapping.
_UNLIMITED = float('inf')

# The root directory path (relative, no leading slash).
_ROOT = ''


@dataclass
class Candidate:
	"""
	A path candidate for selection.

	*path* (:class:`str`) is the path of the candidate. It is normalized using
	:func:`pathspec.util.normalize_file` before duplicate detection and quota
	consumption.

	*score* (:class:`float`) is the non-negative priority score. Higher scores
	are preferred.

	*data* (:class:`object`) is optional user-defined data carried through to
	the result. It is not used by the selector.
	"""

	path: str
	score: float
	data: object = None


@dataclass
class RejectedCandidate:
	"""
	A candidate that was not selected.

	*candidate* (:class:`.Candidate`) is the rejected candidate.

	*order* (:class:`int`) is the original input order (0-based).

	*reason* (:class:`str`) is why the candidate was rejected. One of:

	- ``"ignore"`` -- the candidate was ignored by the path-spec.
	- ``"duplicate"`` -- a duplicate path identity was already seen.
	- ``"quota"`` -- adding the candidate would exceed a directory quota.

	*limited_by* (:class:`str` or :data:`None`) is the directory whose quota
	prevented selection when :attr:`reason` is ``"quota"``; otherwise
	:data:`None`. It is the most specific ancestor directory (closest to the
	candidate) that was already at its quota.
	"""

	candidate: Candidate
	order: int
	reason: str
	limited_by: Optional[str] = None


@dataclass
class SelectionResult:
	"""
	The result of a selection.

	*selected* (:class:`list` of :class:`.Candidate`) is the selected candidates
	in input order.

	*rejected* (:class:`list` of :class:`.RejectedCandidate`) is the rejected
	candidates in input order.

	*total_score* (:class:`float`) is the sum of :attr:`Candidate.score` for the
	selected candidates.
	"""

	selected: list
	rejected: list
	total_score: float = field(init=False)

	def __post_init__(self) -> None:
		self.total_score = sum(c.score for c in self.selected)


def _normalize_dir(path: str) -> str:
	"""
	Normalize a directory path for use as a quota key.

	*path* (:class:`str`) is the directory path.

	Returns the normalized directory path (:class:`str`). Root is represented
	as :data:`_ROOT` (``""``).
	"""
	norm = normalize_file(path)
	if norm == '.':
		norm = _ROOT
	# Remove trailing slash (normalize_file only strips a leading slash).
	if norm.endswith('/'):
		norm = norm.rstrip('/')
	if norm == '.':
		norm = _ROOT
	return norm


def _normalize_quotas(quotas: Mapping[str, int]) -> dict:
	"""
	Normalize the quotas mapping.

	*quotas* (:class:`~collections.abc.Mapping`) maps directory paths to their
	count quotas.

	Returns a new mapping with normalized directory paths.
	"""
	return {_normalize_dir(k): v for k, v in quotas.items()}


def _ancestor_dirs(path: str) -> list:
	"""
	Return the ancestor directories of *path*, from root to immediate parent.

	*path* (:class:`str`) is the normalized path.

	Returns the ancestor directories (:class:`list` of :class:`str`). For
	``"a/b/c"`` this is ``["", "a", "a/b"]``. For a top-level file ``"x"`` this
	is ``[""]``.
	"""
	parts = path.split('/')
	# The candidate's own segment is not an ancestor.
	dirs = []
	cur = _ROOT
	for part in parts[:-1]:
		if cur == _ROOT:
			cur = part
		else:
			cur = cur + '/' + part
		dirs.append(cur)
	# Root is always an ancestor.
	dirs.insert(0, _ROOT)
	return dirs


class PathSelector:
	"""
	The :class:`PathSelector` selects the highest-scoring legal subset of path
	candidates subject to per-directory count quotas.

	*spec* (:class:`~pathspec.PathSpec`) is the path-spec used to filter
	candidates. Candidates matched by *spec* are ignored (rejected).

	*quotas* (:class:`~collections.abc.Mapping`) maps directory paths to their
	count quotas. Selecting a candidate consumes one quota from every ancestor
	directory of its path. The root directory is represented by ``""`` (or
	``"."``). Directories not present in *quotas* have no quota (unlimited).
	"""

	def __init__(
		self,
		spec: PathSpec,
		quotas: Mapping[str, int],
	) -> None:
		"""
		Initialize the :class:`PathSelector` instance.
		"""
		self.spec: PathSpec = spec
		"""
		*spec* (:class:`~pathspec.PathSpec`) is the path-spec used to filter
		candidates.
		"""

		self.quotas: dict = _normalize_quotas(quotas)
		"""
		*quotas* (:class:`dict`) maps normalized directory paths to their count
		quotas.
		"""

	def select(
		self,
		candidates: Iterable[Candidate],
	) -> SelectionResult:
		"""
		Select the highest-scoring legal subset of candidates.

		*candidates* (:class:`~collections.abc.Iterable` of :class:`.Candidate`)
		is the candidates to select from.

		Returns the selection result (:class:`.SelectionResult`).
		"""
		# Materialize candidates and validate scores.
		cand_list = list(candidates)
		for c in cand_list:
			if c.score < 0:
				raise ValueError(f"candidate score must be non-negative: {c!r}")

		rejected: list = []

		# Phase 1: reject duplicate path identities. The first occurrence is
		# kept; subsequent occurrences are rejected. This happens before ignore
		# filtering and quota processing.
		seen: dict = {}
		survivors: list = []
		for order, cand in enumerate(cand_list):
			norm_path = normalize_file(cand.path)
			if norm_path in seen:
				rejected.append(RejectedCandidate(
					candidate=cand,
					order=order,
					reason='duplicate',
				))
			else:
				seen[norm_path] = order
				survivors.append((order, cand, norm_path))

		# Phase 2: ignore filtering via the path-spec. Ignored candidates are
		# rejected before quota processing.
		eligible: list = []
		for order, cand, norm_path in survivors:
			if self.spec.match_file(cand.path):
				rejected.append(RejectedCandidate(
					candidate=cand,
					order=order,
					reason='ignore',
				))
			else:
				eligible.append((order, cand, norm_path))

		# Phase 3: quota optimization. The quota constraints form a laminar
		# matroid, so the stable greedy (descending score, ascending input
		# order) yields the maximum-total-score legal set.
		eligible.sort(key=lambda item: (-item[1].score, item[0]))

		counts: dict = {}
		selected: list = []
		for order, cand, norm_path in eligible:
			ancestors = _ancestor_dirs(norm_path)
			# Find the most specific ancestor directory already at its quota.
			limiting: Optional[str] = None
			for d in reversed(ancestors):
				if counts.get(d, 0) >= self.quotas.get(d, _UNLIMITED):
					limiting = d
					break
			if limiting is None:
				selected.append((order, cand))
				for d in ancestors:
					counts[d] = counts.get(d, 0) + 1
			else:
				rejected.append(RejectedCandidate(
					candidate=cand,
					order=order,
					reason='quota',
					limited_by=limiting,
				))

		# Sort selected and rejected by input order for stable output.
		selected.sort(key=lambda item: item[0])
		rejected.sort(key=lambda item: item.order)

		return SelectionResult(
			selected=[c for _, c in selected],
			rejected=rejected,
		)


def select_paths(
	spec: PathSpec,
	candidates: Iterable[Candidate],
	quotas: Mapping[str, int],
) -> SelectionResult:
	"""
	Select the highest-scoring legal subset of candidates.

	This is a convenience function that creates a :class:`.PathSelector` and
	calls :meth:`~.PathSelector.select`.

	*spec* (:class:`~pathspec.PathSpec`) is the path-spec used to filter
	candidates.

	*candidates* (:class:`~collections.abc.Iterable` of :class:`.Candidate`)
	is the candidates to select from.

	*quotas* (:class:`~collections.abc.Mapping`) maps directory paths to their
	count quotas.

	Returns the selection result (:class:`.SelectionResult`).
	"""
	return PathSelector(spec, quotas).select(candidates)