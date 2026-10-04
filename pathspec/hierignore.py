"""
This module provides :class:`HierIgnoreEvaluator`, a hierarchical gitignore
evaluator. It takes a root rule snapshot plus per-subdirectory rule snapshots
and decides whether a given relative path is ignored, explaining the source
file and rule line that ultimately took effect.

The semantics mirror Git's *.gitignore* discovery:

-	Each snapshot is scoped to the directory it belongs to. Patterns are
	matched against paths relative to that directory.

-	A deeper directory's rules take precedence over a shallower directory's
	rules. Within one snapshot, the last matching line wins.

-	When a parent directory is excluded, rules from snapshots inside the
	excluded directory cannot re-include individual files below it. The parent
	directory itself must be re-included first (e.g. ``!build/``) by a rule
	from a snapshot at or above the parent directory.

-	Directories and files are evaluated separately: a directory-only pattern
	(ending in ``/``) never matches a file path.

The evaluator only consumes the paths and rules it is given. It never scans
the file system. Snapshots and evaluators are immutable: updating rules
creates a new snapshot (and a new evaluator via
:meth:`HierIgnoreEvaluator.with_snapshot`), leaving existing evaluators
untouched.
"""
from __future__ import annotations

from dataclasses import (
	dataclass)
from typing import (
	Dict,  # Replaced by `dict` in 3.9.
	Iterable,
	Mapping,
	Optional,  # Replaced by `X | None` in 3.10.
	Tuple)  # Replaced by `tuple` in 3.9.

from .patterns.gitignore.spec import (
	_DIR_MARK,
	GitIgnoreSpecPattern)
from .util import (
	StrPath,
	normalize_file)

_BACKSLASH_SEPS = ('\\', '/')
"""
Separators used to normalize query paths and snapshot directories so that
Windows-style backslash paths are treated the same as POSIX paths.
"""


def _split_path(path: StrPath) -> Tuple[str, ...]:
	"""
	Normalize *path* (backslashes become slashes, leading ``/`` and ``./``
	are removed) and split it into segments.

	Raises :exc:`ValueError` if the path is empty or escapes the root with
	``..``.
	"""
	norm = normalize_file(path, separators=_BACKSLASH_SEPS)
	segments = tuple(seg for seg in norm.split('/') if seg not in ('', '.'))
	if not segments:
		raise ValueError(f"path={path!r} does not name a path below the root.")
	if '..' in segments:
		raise ValueError(f"path={path!r} escapes the root directory.")
	return segments


@dataclass(frozen=True)
class IgnoreRule:
	"""
	An :class:`IgnoreRule` is a single, immutable gitignore rule parsed from
	a snapshot, retaining where it came from.
	"""

	source: str
	"""
	*source* (:class:`str`) labels the file the rule was read from (e.g.
	``".gitignore"`` or ``"build/.gitignore"``).
	"""

	lineno: int
	"""
	*lineno* (:class:`int`) is the 1-based line number in the source text.
	Blank lines and comments are counted.
	"""

	pattern: str
	"""
	*pattern* (:class:`str`) is the original rule line.
	"""

	include: bool
	"""
	*include* (:class:`bool`) is whether the rule re-includes matched paths
	(:data:`True` for negated ``!`` rules) or excludes them (:data:`False`).
	"""

	compiled: GitIgnoreSpecPattern
	"""
	*compiled* (:class:`.GitIgnoreSpecPattern`) is the compiled pattern.
	"""


@dataclass(frozen=True)
class IgnoreSnapshot:
	"""
	An :class:`IgnoreSnapshot` is an immutable set of rules parsed from one
	rule file (e.g. a single ``.gitignore``). Updating rules means building a
	new snapshot; existing snapshots never change.
	"""

	source: str
	"""
	*source* (:class:`str`) labels the file the rules were read from.
	"""

	rules: Tuple[IgnoreRule, ...]
	"""
	*rules* is a tuple of the parsed :class:`IgnoreRule` entries in file
	order. Blank lines and comments are omitted but still consume line
	numbers.
	"""

	@classmethod
	def from_lines(
		cls,
		lines: Iterable[str],
		source: str = '<rules>',
	) -> 'IgnoreSnapshot':
		"""
		Parse *lines* into a new snapshot. Line numbers are 1-based and count
		blank lines and comments.
		"""
		rules = []
		for lineno, line in enumerate(lines, 1):
			compiled = GitIgnoreSpecPattern(line)
			if compiled.include is None:
				# Blank line or comment: consumes a line number only.
				continue
			rules.append(IgnoreRule(
				source=source,
				lineno=lineno,
				pattern=line,
				# In gitignore semantics a negated ("!") pattern re-includes.
				include=compiled.include is False,
				compiled=compiled,
			))
		return cls(source=source, rules=tuple(rules))

	@classmethod
	def from_text(
		cls,
		text: str,
		source: str = '<rules>',
	) -> 'IgnoreSnapshot':
		"""
		Parse rule *text* (newline separated) into a new snapshot.
		"""
		return cls.from_lines(text.splitlines(), source=source)


@dataclass(frozen=True)
class IgnoreDecision:
	"""
	An :class:`IgnoreDecision` explains whether a path is ignored and which
	source file and rule line ultimately took effect.
	"""

	path: str
	"""
	*path* (:class:`str`) is the normalized queried path.
	"""

	is_dir: bool
	"""
	*is_dir* (:class:`bool`) is whether the path was evaluated as a
	directory.
	"""

	ignored: bool
	"""
	*ignored* (:class:`bool`) is whether the path is ignored.
	"""

	source: Optional[str] = None
	"""
	*source* (:class:`str` or :data:`None`) is the source file of the
	effective rule, or :data:`None` when no rule decided the outcome.
	"""

	lineno: Optional[int] = None
	"""
	*lineno* (:class:`int` or :data:`None`) is the 1-based line number of the
	effective rule in its source (blank lines counted).
	"""

	pattern: Optional[str] = None
	"""
	*pattern* (:class:`str` or :data:`None`) is the effective rule line.
	"""

	include: Optional[bool] = None
	"""
	*include* (:class:`bool` or :data:`None`) is whether the effective rule
	re-includes (:data:`True`, a ``!`` rule) or excludes (:data:`False`).
	"""

	matched_path: Optional[str] = None
	"""
	*matched_path* (:class:`str` or :data:`None`) is the path the effective
	rule decided. It differs from *path* when an ancestor directory decided
	the outcome.
	"""

	reason: str = ''
	"""
	*reason* (:class:`str`) is a human-readable explanation.
	"""

	def explain(self) -> str:
		"""
		Render the decision as a one-line explanation naming the source file
		and rule line that took effect.
		"""
		what = 'directory' if self.is_dir else 'file'
		verb = 'ignored' if self.ignored else 'not ignored'
		head = f"{what} '{self.path}' is {verb}"
		if self.source is None:
			return f"{head}: {self.reason}"
		where = f"{self.source}:{self.lineno}"
		if self.matched_path is not None and self.matched_path != self.path:
			where += f" (matched '{self.matched_path}')"
		return f"{head} by {where} {self.pattern!r}: {self.reason}"


class HierIgnoreEvaluator:
	"""
	The :class:`HierIgnoreEvaluator` class evaluates relative paths against a
	root rule snapshot plus per-subdirectory snapshots. It is immutable:
	:meth:`with_snapshot` returns a new evaluator and never modifies the
	existing one.
	"""

	def __init__(
		self,
		root: IgnoreSnapshot,
		snapshots: Optional[Mapping[str, IgnoreSnapshot]] = None,
	) -> None:
		"""
		*root* (:class:`IgnoreSnapshot`) contains the rules scoped to the root
		directory.

		*snapshots* (:class:`~collections.abc.Mapping`) maps each subdirectory
		(relative to the root) to its :class:`IgnoreSnapshot`.
		"""
		scoped: Dict[Tuple[str, ...], IgnoreSnapshot] = {(): root}
		if snapshots:
			for directory, snapshot in snapshots.items():
				key = _split_path(directory)
				if key in scoped:
					raise ValueError(f"duplicate snapshot for directory={directory!r}.")
				scoped[key] = snapshot
		# Store sorted by depth so deeper snapshots are considered later.
		self._snapshots: Tuple[Tuple[Tuple[str, ...], IgnoreSnapshot], ...] = tuple(
			sorted(scoped.items(), key=lambda item: len(item[0]))
		)

	@classmethod
	def from_texts(
		cls,
		root_text: str,
		dir_texts: Optional[Mapping[str, str]] = None,
		*,
		root_source: str = '.gitignore',
		sources: Optional[Mapping[str, str]] = None,
	) -> 'HierIgnoreEvaluator':
		"""
		Build an evaluator from rule texts.

		*root_text* (:class:`str`) is the root rule file's text.

		*dir_texts* (:class:`~collections.abc.Mapping`) maps each subdirectory
		to its rule file's text.

		*root_source* and *sources* label the source files used in
		explanations. By default a directory ``build`` is labeled
		``"build/.gitignore"``.
		"""
		root = IgnoreSnapshot.from_text(root_text, source=root_source)
		snapshots = {}
		for directory, text in (dir_texts or {}).items():
			key = '/'.join(_split_path(directory))
			if sources and directory in sources:
				source = sources[directory]
			else:
				source = f"{key}/.gitignore"
			snapshots[key] = IgnoreSnapshot.from_text(text, source=source)
		return cls(root, snapshots)

	def with_snapshot(
		self,
		directory: str,
		snapshot: IgnoreSnapshot,
	) -> 'HierIgnoreEvaluator':
		"""
		Return a new evaluator with *snapshot* installed for *directory*
		(``""`` replaces the root snapshot). This evaluator is left unchanged.
		"""
		key = '/'.join(_split_path(directory)) if directory else ''
		kept = {
			'/'.join(dir_key): snap
			for dir_key, snap in self._snapshots
			if dir_key
		}
		if key:
			kept.pop(key, None)
			kept[key] = snapshot
			root = self._snapshots[0][1]
		else:
			root = snapshot
		return HierIgnoreEvaluator(root, kept)

	def is_ignored(self, path: StrPath, is_dir: Optional[bool] = None) -> bool:
		"""
		Check whether *path* is ignored. See :meth:`check`.
		"""
		return self.check(path, is_dir=is_dir).ignored

	def check(self, path: StrPath, is_dir: Optional[bool] = None) -> IgnoreDecision:
		"""
		Evaluate *path* relative to the root.

		*is_dir* (:class:`bool` or :data:`None`) is whether to evaluate the
		path as a directory. Default is :data:`None`: a trailing slash means
		a directory, anything else a file. Directories and files are judged
		separately; directory-only patterns (``build/``) never match files.
		"""
		raw = str(path)
		if is_dir is None:
			is_dir = raw.endswith(('/', '\\'))
		segments = _split_path(raw)

		# Walk the ancestor directories (and the path itself when it is a
		# directory) from the root downwards. Once a directory is excluded,
		# everything below it stays excluded (Git never descends into an
		# excluded directory), so deeper rules are not consulted until the
		# excluded directory itself is re-included by a rule governing it.
		dir_count = len(segments) if is_dir else len(segments) - 1
		dir_decision: Optional[IgnoreDecision] = None
		blocked: Optional[IgnoreDecision] = None
		for end in range(1, dir_count + 1):
			if blocked is not None:
				dir_decision = self._inherit_block(segments[:end], blocked, is_dir=True)
			else:
				dir_decision = self._decide_dir(segments[:end])
				if dir_decision.ignored:
					blocked = dir_decision

		if is_dir:
			assert dir_decision is not None
			return dir_decision
		return self._decide_file(segments, blocked)

	@staticmethod
	def _inherit_block(
		segments: Tuple[str, ...],
		blocked: IgnoreDecision,
		is_dir: bool,
	) -> IgnoreDecision:
		"""
		Build the decision for a path below the excluded directory decided by
		*blocked*, pointing at the rule that excluded that ancestor.
		"""
		return IgnoreDecision(
			path='/'.join(segments),
			is_dir=is_dir,
			ignored=True,
			source=blocked.source,
			lineno=blocked.lineno,
			pattern=blocked.pattern,
			include=blocked.include,
			matched_path=blocked.matched_path,
			reason=(
				f"ancestor directory '{blocked.matched_path}' is excluded; rules "
				"below an excluded directory cannot re-include it"
			),
		)

	def _match_rules(
		self,
		segments: Tuple[str, ...],
		target: str,
		file_level: bool,
	) -> Optional[IgnoreRule]:
		"""
		Find the rule that decides the path made of *segments* plus *target*
		(a final component; give it a trailing slash for directories).
		Deeper snapshots win over shallower ones; within a snapshot the last
		matching line wins.
		"""
		best: Optional[IgnoreRule] = None
		best_key: Optional[Tuple[int, int]] = None
		for dir_key, snapshot in self._snapshots:
			if len(dir_key) > len(segments) or segments[:len(dir_key)] != dir_key:
				continue
			rel = '/'.join(segments[len(dir_key):])
			if target:
				rel = f"{rel}/{target}" if rel else target
			for rule in snapshot.rules:
				match = rule.compiled.match_file(rel)
				if match is None:
					continue
				if file_level and match.match.groupdict().get(_DIR_MARK):
					# The pattern matched an ancestor directory of the file,
					# not the file itself. Git only applies directory patterns
					# to directories; ancestor exclusion is resolved by the
					# directory walk, not here.
					continue
				key = (len(dir_key), rule.lineno)
				if best_key is None or key > best_key:
					best = rule
					best_key = key
		return best

	def _decide_dir(
		self,
		dir_segments: Tuple[str, ...],
	) -> IgnoreDecision:
		"""
		Decide a single directory. The caller guarantees no ancestor directory
		is excluded.
		"""
		path = '/'.join(dir_segments)
		rule = self._match_rules(
			dir_segments[:-1], target=dir_segments[-1] + '/', file_level=False)
		if rule is not None:
			return IgnoreDecision(
				path=path,
				is_dir=True,
				ignored=not rule.include,
				source=rule.source,
				lineno=rule.lineno,
				pattern=rule.pattern,
				include=rule.include,
				matched_path=path,
				reason='matched directory rule',
			)
		return IgnoreDecision(
			path=path,
			is_dir=True,
			ignored=False,
			reason='no matching rule',
		)

	def _decide_file(
		self,
		segments: Tuple[str, ...],
		blocked: Optional[IgnoreDecision],
	) -> IgnoreDecision:
		"""
		Decide a file. A file below an excluded directory stays excluded:
		negated rules inside the excluded directory cannot re-include it.
		"""
		path = '/'.join(segments)
		if blocked is not None:
			return self._inherit_block(segments, blocked, is_dir=False)
		# NOTE: Directory patterns only apply to directories. Re-including a
		# directory with ``!build/`` re-includes its contents as well (Git
		# descends into the directory and evaluates each file on its own).
		rule = self._match_rules(segments[:-1], target=segments[-1], file_level=True)
		if rule is not None:
			return IgnoreDecision(
				path=path,
				is_dir=False,
				ignored=not rule.include,
				source=rule.source,
				lineno=rule.lineno,
				pattern=rule.pattern,
				include=rule.include,
				matched_path=path,
				reason='matched file rule',
			)
		return IgnoreDecision(
			path=path,
			is_dir=False,
			ignored=False,
			reason='no matching rule',
		)


def _demo() -> None:
	"""
	Small demonstration used by ``python -m pathspec.hierignore``.
	"""
	evaluator = HierIgnoreEvaluator.from_texts(
		"build/\n",
		{'build': "!keep.txt\n"},
	)
	print('# Root ignores build/; child !keep.txt cannot rescue a file inside:')
	print(evaluator.check('build/keep.txt').explain())
	included = evaluator.with_snapshot(
		'', IgnoreSnapshot.from_text("build/\n!build/\n", source='.gitignore'))
	print('# After the root re-includes build/ itself, the child rule applies:')
	print(included.check('build/keep.txt').explain())


if __name__ == '__main__':
	_demo()
