"""
This module provides hierarchical ignore evaluation for *gitignore* patterns.

A hierarchical ignore evaluator takes the root rules and snapshots of rules in
each subdirectory, and determines whether a relative path is ignored. It also
explains the last effective source file and rule line.

The matching scope is based on each rule's directory. When a parent directory is
already excluded, sub-rules cannot rescue individual files inside it, unless the
parent directory is also re-included. Directory and file paths are judged
separately.

Only the given paths and rules are consumed; the file system is not scanned.
Rule updates create new snapshots; existing evaluators are not modified.

Examples:

	Basic hierarchical matching:

		>>> from pathspec.hierarchical import IgnoreSnapshot
		>>> snapshot = IgnoreSnapshot.from_lines(
		...     root_lines=["build/", "*.log"],
		...     subdir_lines={"build": ["!keep.txt"]},
		... )
		>>> evaluator = snapshot.evaluator()
		>>> result = evaluator.evaluate("build/keep.txt")
		>>> result.ignored
		True
		>>> result.source.file
		'.gitignore'
		>>> result.source.line
		1

	Parent exclusion blocks child rescue:

		>>> snapshot = IgnoreSnapshot.from_lines(
		...     root_lines=["build/"],
		...     subdir_lines={"build": ["!keep.txt"]},
		... )
		>>> evaluator = snapshot.evaluator()
		>>> evaluator.evaluate("build/keep.txt").ignored
		True

	Parent re-inclusion allows child rules to take effect:

		>>> snapshot = IgnoreSnapshot.from_lines(
		...     root_lines=["build/", "!build/"],
		...     subdir_lines={"build": ["!keep.txt"]},
		... )
		>>> evaluator = snapshot.evaluator()
		>>> evaluator.evaluate("build/keep.txt").ignored
		False

	Nested directories:

		>>> snapshot = IgnoreSnapshot.from_lines(
		...     root_lines=["build/"],
		...     subdir_lines={
		...         "build": ["output/"],
		...         "build/output": ["!keep.txt"],
		...     },
		... )
		>>> evaluator = snapshot.evaluator()
		>>> evaluator.evaluate("build/output/keep.txt").ignored
		True

	Backslash normalization:

		>>> snapshot = IgnoreSnapshot.from_lines(root_lines=["build/"])
		>>> evaluator = snapshot.evaluator()
		>>> evaluator.evaluate("build\\\\keep.txt").ignored
		True

	Snapshot isolation (updates create new snapshots):

		>>> snapshot1 = IgnoreSnapshot.from_lines(root_lines=["build/"])
		>>> evaluator1 = snapshot1.evaluator()
		>>> evaluator1.evaluate("build/keep.txt").ignored
		True
		>>> snapshot2 = IgnoreSnapshot.from_lines(root_lines=["build/", "!build/"])
		>>> evaluator2 = snapshot2.evaluator()
		>>> evaluator2.evaluate("build/keep.txt").ignored
		False
		>>> evaluator1.evaluate("build/keep.txt").ignored
		True

Short command:

	python -c "from pathspec.hierarchical import IgnoreSnapshot; s = IgnoreSnapshot.from_lines(root_lines=['build/'], subdir_lines={'build': ['!keep.txt']}); e = s.evaluator(); print(e.evaluate('build/keep.txt').ignored)"

"""
from __future__ import annotations

from collections.abc import (
	Iterable,
	Mapping)
from dataclasses import (
	dataclass)
from typing import (
	Optional,
	Union)

from pathspec.patterns.gitignore.spec import (
	GitIgnoreSpecPattern,
	_DIR_MARK)
from pathspec.util import (
	normalize_file)

__all__ = [
	'HierarchicalIgnoreEvaluator',
	'IgnoreResult',
	'IgnoreSnapshot',
	'RuleSource',
]


@dataclass(frozen=True)
class RuleSource(object):
	"""
	Source information for a rule.

	*file* (:class:) is the source file name (e.g., ".gitignore").

	*line* (:class:) is the 1-based line number in the source file. Blank
	lines and comments are counted.

	*pattern* (:class:) is the raw pattern string.
	"""

	file: str
	"""
	*file* (:class:) is the source file name.
	"""

	line: int
	"""
	*line* (:class:) is the 1-based line number.
	"""

	pattern: str
	"""
	*pattern* (:class:) is the raw pattern string.
	"""


@dataclass(frozen=True)
class IgnoreResult(object):
	"""
	Result of an ignore evaluation.

	*include* (:class: or :data:) is whether the path should be
	included (:data:), excluded (:data:), or no rule matched
	(:data:).

	*source* (:class: or :data:) is the source of the last
	effective rule.
	"""

	include: Optional[bool]
	"""
	*include* (:class: or :data:) is whether the path should be
	included.
	"""

	source: Optional[RuleSource] = None
	"""
	*source* (:class: or :data:) is the source of the last
	effective rule.
	"""

	@property
	def ignored(self) -> bool:
		"""
		Whether the path is ignored.

		Returns :data: if the path is ignored; otherwise, :data:.
		"""
		return self.include is False


class _CompiledRule(object):
	"""
	A compiled rule with source information.
	"""

	__slots__ = (
		'line_no',
		'pattern',
		'include',
		'regex',
	)

	def __init__(
		self,
		line_no: int,
		pattern: str,
		compiled: GitIgnoreSpecPattern,
	) -> None:
		"""
		Initializes the :class: instance.

		*line_no* (:class:) is the 1-based line number.

		*pattern* (:class:) is the raw pattern string.

		*compiled* (:class:) is the compiled pattern.
		"""
		self.line_no = line_no
		self.pattern = pattern
		self.include = compiled.include
		self.regex = compiled.regex


class _Level(object):
	"""
	A level in the hierarchy (root or a subdirectory).
	"""

	__slots__ = (
		'dir_path',
		'rules',
		'source_file',
	)

	def __init__(
		self,
		dir_path: str,
		rules: list[_CompiledRule],
		source_file: str,
	) -> None:
		"""
		Initializes the :class: instance.

		*dir_path* (:class:) is the directory path this level applies to
		(an empty string for the root directory).

		*rules* (:class: of :class:) is the compiled rules.

		*source_file* (:class:) is the source file name.
		"""
		self.dir_path = dir_path
		self.rules = rules
		self.source_file = source_file


def _compile_rules(
	lines: Iterable[Union[str, bytes]],
	source_file: str,
) -> list[_CompiledRule]:
	"""
	Compile lines into rules with source information.

	*lines* (:class:) yields each uncompiled pattern
	(:class: or :class:).

	*source_file* (:class:) is the source file name.

	Returns the compiled rules (:class: of :class:).
	"""
	rules: list[_CompiledRule] = []
	for line_no, line in enumerate(lines, 1):
		if isinstance(line, bytes):
			line = line.decode('utf-8')
		compiled = GitIgnoreSpecPattern(line)
		if compiled.include is not None:
			rules.append(_CompiledRule(line_no, line, compiled))
	return rules


class IgnoreSnapshot(object):
	"""
	The :class: class represents an immutable snapshot of ignore
	rules for a directory tree.

	A snapshot contains the root rules and the rules for each subdirectory.
	Rule updates create new snapshots; existing evaluators are not modified.
	"""

	def __init__(
		self,
		root_rules: Iterable[Union[str, bytes]],
		subdir_rules: Optional[Mapping[str, Iterable[Union[str, bytes]]]] = None,
		*,
		root_source: str = '.gitignore',
		subdir_sources: Optional[Mapping[str, str]] = None,
	) -> None:
		"""
		Initializes the :class: instance.

		*root_rules* (:class:) yields each uncompiled
		pattern (:class: or :class:) for the root directory.

		*subdir_rules* (:class:) maps each
		subdirectory path (:class:) to the rules
		(:class: of :class: or :class:).

		*root_source* (:class:) is the source file name for the root rules.

		*subdir_sources* (:class:) maps each
		subdirectory path (:class:) to the source file name (:class:).
		"""
		self._levels: list[_Level] = []

		# Root level.
		root_rules_list = _compile_rules(root_rules, root_source)
		self._levels.append(_Level('', root_rules_list, root_source))

		# Subdirectory levels.
		subdir_rules = subdir_rules or {}
		subdir_sources = subdir_sources or {}

		for dir_path, lines in subdir_rules.items():
			dir_path = dir_path.strip('/')
			source_file = subdir_sources.get(dir_path, f'{dir_path}/.gitignore')
			rules = _compile_rules(lines, source_file)
			self._levels.append(_Level(dir_path, rules, source_file))

	@classmethod
	def from_lines(
		cls: type[IgnoreSnapshot],
		root_lines: Iterable[Union[str, bytes]],
		subdir_lines: Optional[Mapping[str, Iterable[Union[str, bytes]]]] = None,
		*,
		root_source: str = '.gitignore',
		subdir_sources: Optional[Mapping[str, str]] = None,
	) -> IgnoreSnapshot:
		"""
		Create a snapshot from lines.

		*root_lines* (:class:) yields each uncompiled
		pattern (:class: or :class:) for the root directory.

		*subdir_lines* (:class:) maps each
		subdirectory path (:class:) to the rules
		(:class: of :class: or :class:).

		*root_source* (:class:) is the source file name for the root rules.

		*subdir_sources* (:class:) maps each
		subdirectory path (:class:) to the source file name (:class:).

		Returns the snapshot (:class:).
		"""
		return cls(
			root_lines,
			subdir_lines,
			root_source=root_source,
			subdir_sources=subdir_sources,
		)

	def evaluator(self) -> HierarchicalIgnoreEvaluator:
		"""
		Create an evaluator from this snapshot.

		Returns the evaluator (:class:).
		"""
		return HierarchicalIgnoreEvaluator(self._levels)


class HierarchicalIgnoreEvaluator(object):
	"""
	The :class: class evaluates ignore rules
	hierarchically.

	The evaluator is created from an :class:. Rule updates
	create new snapshots; existing evaluators are not modified.
	"""

	def __init__(self, levels: list[_Level]) -> None:
		"""
		Initializes the :class: instance.

		*levels* (:class: of :class:) is the compiled levels.
		"""
		self._levels = levels

	def evaluate(
		self,
		path: str,
		*,
		is_dir: Optional[bool] = None,
	) -> IgnoreResult:
		"""
		Evaluate whether the path is ignored.

		*path* (:class:) is the relative path to evaluate. Backslashes are
		normalized to forward slashes.

		*is_dir* (:class: or :data:) is whether the path is a
		directory. If :data:, the path is treated as a file.

		Returns the result (:class:).
		"""
		# Normalize path separators (backslash to forward slash).
		path = normalize_file(path, separators=(chr(92),))

		# Remove trailing slash for consistent matching.
		path = path.rstrip('/')

		# Build ancestor directory paths (without trailing slash).
		components = path.split('/') if path else []
		ancestor_paths: list[str] = []
		for i in range(len(components)):
			ancestor_paths.append('/'.join(components[:i + 1]))

		# For matching, append trailing slash for directories so directory
		# patterns (ending with /) match correctly.
		match_path = path + '/' if is_dir else path

		# Track states per ancestor directory.
		dir_states: dict[str, Optional[bool]] = {}
		dir_sources: dict[str, RuleSource] = {}

		file_state: Optional[bool] = None
		file_source: Optional[RuleSource] = None

		for level in self._levels:
			# Determine the relative path at this level.
			if level.dir_path:
				if not match_path.startswith(level.dir_path + '/'):
					continue
				rel_path = match_path[len(level.dir_path) + 1:]
			else:
				rel_path = match_path

			for rule in level.rules:
				if rule.include is None or rule.regex is None:
					continue
				match = rule.regex.search(rel_path)
				if match is None:
					continue

				is_dir_match = bool(match.groupdict().get(_DIR_MARK))

				if is_dir_match:
					# Directory pattern matched. Determine which ancestor
					# directory it matches.
					dir_mark_start = match.start(_DIR_MARK)
					matched_dir = rel_path[:dir_mark_start]

					for ancestor_path in ancestor_paths:
						# Check if the matched directory is this ancestor or a
						# descendant.
						if ancestor_path == matched_dir or ancestor_path.startswith(matched_dir + '/'):
							# Check if this ancestor is at or below the current
							# level.
							if level.dir_path == '' or ancestor_path.startswith(level.dir_path + '/'):
								# Flip semantics: rule.include=True means ignored
								# (excluded), so IgnoreResult.include=False.
								dir_states[ancestor_path] = not rule.include
								dir_sources[ancestor_path] = RuleSource(
									level.source_file,
									rule.line_no,
									rule.pattern,
								)
				else:
					# File pattern matched.
					# Flip semantics: rule.include=True means ignored (excluded),
					# so IgnoreResult.include=False.
					file_state = not rule.include
					file_source = RuleSource(
						level.source_file,
						rule.line_no,
						rule.pattern,
					)

		# Check if any ancestor directory is blocked.
		blocked = any(state is False for state in dir_states.values())

		if is_dir:
			# For directories, return the directory state.
			if path in dir_states:
				return IgnoreResult(dir_states[path], dir_sources[path])
			elif blocked:
				# Find the source of the blocked ancestor.
				for ancestor_path in reversed(ancestor_paths):
					if ancestor_path in dir_states and dir_states[ancestor_path] is False:
						return IgnoreResult(False, dir_sources[ancestor_path])
				return IgnoreResult(False, None)
			else:
				return IgnoreResult(True, None)
		else:
			# For files, return the file state (considering ancestor
			# directories).
			if blocked:
				# Find the source of the blocked ancestor.
				for ancestor_path in reversed(ancestor_paths):
					if ancestor_path in dir_states and dir_states[ancestor_path] is False:
						return IgnoreResult(False, dir_sources[ancestor_path])
				return IgnoreResult(False, None)
			elif file_state is not None:
				return IgnoreResult(file_state, file_source)
			elif path in dir_states:
				return IgnoreResult(dir_states[path], dir_sources[path])
			else:
				return IgnoreResult(True, None)
