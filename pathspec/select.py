"""
This module provides a quota-aware path selector.

Candidates (already filtered by a :class:`~pathspec.pathspec.PathSpec`) carry
non-negative priority scores, and directories (including their ancestors) each
have a limited number of slots. :func:`select_paths` picks the legal subset
with the highest total priority without touching the file system.

Run the targeted tests with::

	python -m pytest tests/test_07_select.py -q

Four-candidate example
----------------------

Quotas: root (``""``) 2, directory ``"a"`` 1, directory ``"b"`` 1.

Candidates (path, priority)::

	("a/x.txt", 10)
	("a/y.txt", 9)
	("b/z.txt", 8)
	("c/w.txt", 7)

Selection walks candidates by descending priority (input order breaks ties):

1.	``a/x.txt`` (10) is selected; consumes one slot of ``"a"`` and of the
	root.

2.	``a/y.txt`` (9) is rejected; the ``"a"`` quota (1) is exhausted.

3.	``b/z.txt`` (8) is selected; consumes one slot of ``"b"`` and of the
	root.

4.	``c/w.txt`` (7) is rejected; the root quota (2) is exhausted.

Result: ``a/x.txt`` (limited by ``"a"``) and ``b/z.txt`` (limited by ``"b"``)
for a total priority of 18. Filling each directory independently would wrongly
accept three paths and overflow the root quota.
"""
from __future__ import annotations

from collections.abc import (
	Iterable,
	Mapping)
from dataclasses import (
	dataclass)
from typing import (
	Optional,  # Replaced by `X | None` in 3.10.
	Tuple,  # Replaced by `tuple` in 3.9.
	Union)

from pathspec.pathspec import (
	PathSpec)
from pathspec.util import (
	StrPath,
	normalize_file)

ROOT_DIR = ""
"""
*ROOT_DIR* (:class:`str`) is the normalized key of the root directory quota.
"""


@dataclass(frozen=True)
class Candidate:
	"""
	The :class:`Candidate` data-class pairs a path with its non-negative
	priority score.
	"""

	path: str
	"""
	:attr:`path` (:class:`str`) is the candidate path.
	"""

	priority: float
	"""
	:attr:`priority` (:class:`float`) is the non-negative priority score.
	"""


@dataclass(frozen=True)
class Selection:
	"""
	The :class:`Selection` data-class describes a selected candidate and the
	quota that most tightly constrained it.
	"""

	path: str
	"""
	:attr:`path` (:class:`str`) is the normalized selected path.
	"""

	priority: float
	"""
	:attr:`priority` (:class:`float`) is the candidate priority score.
	"""

	limited_by: Optional[str]
	"""
	:attr:`limited_by` (:class:`str` or :data:`None`) is the applicable
	directory quota with the least remaining capacity after this selection
	(the deepest such directory on ties), or :data:`None` when no quota
	applies to the path.
	"""


def _split_segments(
	path: StrPath,
) -> Tuple[str, ...]:
	"""
	Normalizes *path* (pure string operation, no file-system access) and
	returns its segments. Directory relations are decided by whole segments,
	so ``"a"`` is not an ancestor of ``"ab"``.
	"""
	norm = normalize_file(path)
	segments = tuple(seg for seg in norm.split('/') if seg not in ('', '.'))
	if any(seg == '..' for seg in segments):
		raise ValueError(f"Path must not contain '..' segments: {path!r}.")
	return segments


def _ancestor_dirs(
	segments: Tuple[str, ...],
) -> Iterable[str]:
	"""
	Yields the ancestor directories of a file path, deepest first, ending
	with the root directory (:data:`ROOT_DIR`).
	"""
	for i in range(len(segments) - 1, 0, -1):
		yield '/'.join(segments[:i])
	yield ROOT_DIR


def select_paths(
	spec: Optional[PathSpec],
	candidates: Iterable[Union[Candidate, Tuple[str, float]]],
	quotas: Mapping[str, int],
) -> list[Selection]:
	"""
	Selects the legal subset of *candidates* with the highest total priority.

	*spec* (:class:`~pathspec.pathspec.PathSpec` or :data:`None`) is the
	ignore filter. Matched paths are dropped **before** any quota accounting
	(ignore takes precedence over quotas). Pass :data:`None` to disable
	filtering.

	*candidates* is an iterable of :class:`Candidate` instances or
	``(path, priority)`` pairs. Priorities must be non-negative. Duplicate
	path identities (after normalization) are rejected before quotas are
	considered; the first occurrence wins.

	*quotas* (:class:`~collections.abc.Mapping`) maps a directory path to its
	non-negative integer slot count. The root directory is ``""`` (or
	``"."``). Every selected path consumes one slot from each of its ancestor
	directories that has a quota, so a root quota limits the total across all
	subdirectories rather than letting each directory fill independently.

	Selection is greedy by descending priority with ties broken by input
	order (stable). Because directory quotas form a laminar (nested) family
	of cardinality constraints, this greedy order yields the maximum total
	priority.

	Returns the list of :class:`Selection` in selection order. Each entry
	reports :attr:`Selection.limited_by`, the applicable quota directory left
	with the least remaining capacity after the selection (deepest on ties),
	or :data:`None` when no quota applies.
	"""
	# Normalize quota directories to segment-joined keys.
	remaining: dict[str, int] = {}
	for directory, quota in quotas.items():
		if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
			raise ValueError(f"Quota for {directory!r} must be a non-negative int: {quota!r}.")
		key = '/'.join(_split_segments(directory))
		remaining[key] = quota

	# Filter candidates: the ignore spec applies first, then duplicate path
	# identities are rejected. Neither consumes any quota.
	kept: list[tuple[int, str, float, Tuple[str, ...]]] = []
	seen: set[str] = set()
	for index, item in enumerate(candidates):
		if isinstance(item, Candidate):
			path, priority = item.path, item.priority
		else:
			path, priority = item
		if not (priority >= 0):
			raise ValueError(f"Priority must be non-negative for {path!r}: {priority!r}.")
		segments = _split_segments(path)
		if not segments:
			raise ValueError(f"Candidate path must name a file: {path!r}.")
		norm_path = '/'.join(segments)
		if spec is not None and spec.match_file(norm_path):
			# Ignored paths are dropped before quotas.
			continue
		if norm_path in seen:
			# Duplicate path identity rejected.
			continue
		seen.add(norm_path)
		kept.append((index, norm_path, float(priority), segments))

	# Stable sort by descending priority; equal priorities keep input order.
	kept.sort(key=lambda entry: -entry[2])

	selections: list[Selection] = []
	for _index, norm_path, priority, segments in kept:
		applicable = [d for d in _ancestor_dirs(segments) if d in remaining]
		if all(remaining[d] > 0 for d in applicable):
			for d in applicable:
				remaining[d] -= 1
			# Report the quota left with the least remaining capacity;
			# iteration is deepest-first so ties keep the deepest directory.
			limited_by: Optional[str] = None
			for d in applicable:
				if limited_by is None or remaining[d] < remaining[limited_by]:
					limited_by = d
			selections.append(Selection(path=norm_path, priority=priority, limited_by=limited_by))

	return selections
