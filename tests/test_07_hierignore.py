"""
Tests for :class:`pathspec.hierignore.HierIgnoreEvaluator`: hierarchical
ignore evaluation with per-directory rule snapshots.
"""
import dataclasses

import pytest

from pathspec.hierignore import (
	HierIgnoreEvaluator,
	IgnoreSnapshot)


def test_child_negation_cannot_rescue_file_in_excluded_dir():
	"""
	Acceptance: the root ignores ``build/``; a child ``!keep.txt`` rule cannot
	re-include ``build/keep.txt`` on its own.
	"""
	evaluator = HierIgnoreEvaluator.from_texts(
		"build/\n",
		{'build': "!keep.txt\n"},
	)

	decision = evaluator.check('build/keep.txt')

	assert decision.ignored is True
	# The effective source is the root rule that excluded the parent.
	assert decision.source == '.gitignore'
	assert decision.lineno == 1
	assert decision.pattern == 'build/'
	assert decision.matched_path == 'build'
	assert 'build' in decision.explain()


def test_child_negation_applies_after_parent_dir_reincluded():
	"""
	Acceptance: once ``build/`` itself is explicitly re-included, the child
	rule takes effect.
	"""
	evaluator = HierIgnoreEvaluator.from_texts(
		"build/\n!build/\n",
		{'build': "!keep.txt\n"},
	)

	decision = evaluator.check('build/keep.txt')

	assert decision.ignored is False
	assert decision.source == 'build/.gitignore'
	assert decision.lineno == 1
	assert decision.pattern == '!keep.txt'
	assert decision.include is True

	# Re-including the directory re-includes its other contents as well (Git
	# descends into the directory and evaluates each file on its own).
	other = evaluator.check('build/other.txt')
	assert other.ignored is False
	assert other.source is None


def test_lineno_counts_blank_lines_and_comments():
	"""
	Line numbers in explanations count blank lines and comments.
	"""
	snapshot = IgnoreSnapshot.from_text(
		"# header comment\n"
		"\n"
		"*.log\n"
		"\n"
		"!keep.log\n",
		source='.gitignore',
	)
	assert [rule.lineno for rule in snapshot.rules] == [3, 5]

	evaluator = HierIgnoreEvaluator(snapshot)
	decision = evaluator.check('keep.log')
	assert decision.ignored is False
	assert decision.lineno == 5
	assert decision.pattern == '!keep.log'

	decision = evaluator.check('debug.log')
	assert decision.ignored is True
	assert decision.lineno == 3


def test_nested_directories():
	"""
	Nested snapshots: deeper exclusion blocks deeper rules; re-including the
	intermediate directory unblocks the level below.
	"""
	evaluator = HierIgnoreEvaluator.from_texts(
		"build/\n!build/\n",
		{
			'build': "sub/\n",
			'build/sub': "!x.txt\n",
		},
	)

	# build/sub is excluded by the snapshot in build/.
	decision = evaluator.check('build/sub', is_dir=True)
	assert decision.ignored is True
	assert decision.source == 'build/.gitignore'
	assert decision.matched_path == 'build/sub'

	# The grandchild snapshot cannot rescue a file inside build/sub/.
	decision = evaluator.check('build/sub/x.txt')
	assert decision.ignored is True
	assert decision.source == 'build/.gitignore'
	assert decision.pattern == 'sub/'

	# Re-include build/sub/ from the build/ snapshot: now the grandchild
	# rule applies.
	evaluator2 = evaluator.with_snapshot(
		'build', IgnoreSnapshot.from_text("sub/\n!sub/\n", source='build/.gitignore'))
	decision = evaluator2.check('build/sub/x.txt')
	assert decision.ignored is False
	assert decision.source == 'build/sub/.gitignore'
	assert decision.pattern == '!x.txt'


def test_deeper_snapshot_takes_precedence_over_shallower():
	evaluator = HierIgnoreEvaluator.from_texts(
		"*.tmp\n",
		{'doc': "!*.tmp\n"},
	)
	assert evaluator.is_ignored('root.tmp') is True
	assert evaluator.is_ignored('doc/note.tmp') is False


def test_last_matching_line_wins_within_snapshot():
	evaluator = HierIgnoreEvaluator.from_texts("*.log\n!keep.log\n")
	assert evaluator.is_ignored('keep.log') is False
	assert evaluator.is_ignored('debug.log') is True


def test_backslash_normalization():
	"""
	Windows-style backslash paths are normalized to POSIX separators, both
	for queries and for snapshot directories.
	"""
	evaluator = HierIgnoreEvaluator.from_texts(
		"*.tmp\n",
		{'build\\sub': "*.gen\n"},
	)

	decision = evaluator.check('build\\sub\\out.gen')
	assert decision.ignored is True
	assert decision.path == 'build/sub/out.gen'
	assert decision.source == 'build/sub/.gitignore'

	# Directory queries with trailing backslashes are directories.
	decision = evaluator.check('build\\sub\\')
	assert decision.is_dir is True
	assert decision.ignored is False  # no rule excludes build/sub itself


def test_directories_and_files_judged_separately():
	"""
	A directory-only pattern never matches a file path of the same name.
	"""
	evaluator = HierIgnoreEvaluator.from_texts("build/\n")

	assert evaluator.check('build', is_dir=True).ignored is True
	assert evaluator.check('build/').ignored is True  # trailing slash => dir
	assert evaluator.check('build', is_dir=False).ignored is False
	assert evaluator.check('build').ignored is False  # default is a file


def test_snapshot_isolation():
	"""
	Updating rules creates a new snapshot and a new evaluator; the existing
	evaluator and snapshots are unchanged.
	"""
	root_v1 = IgnoreSnapshot.from_text("build/\n", source='.gitignore')
	evaluator_v1 = HierIgnoreEvaluator(root_v1)

	root_v2 = IgnoreSnapshot.from_text("build/\n!build/\n", source='.gitignore')
	evaluator_v2 = evaluator_v1.with_snapshot('', root_v2)
	child = IgnoreSnapshot.from_text("!keep.txt\n", source='build/.gitignore')
	evaluator_v3 = evaluator_v2.with_snapshot('build', child)

	# The original evaluator still ignores the file.
	assert evaluator_v1.is_ignored('build/keep.txt') is True
	# The new evaluators see the updated rules.
	assert evaluator_v2.is_ignored('build/keep.txt') is False  # build/ re-included
	assert evaluator_v3.is_ignored('build/keep.txt') is False

	# Snapshots are immutable value objects.
	assert root_v1.rules != root_v2.rules
	with pytest.raises(dataclasses.FrozenInstanceError):
		root_v1.rules = root_v2.rules  # type: ignore[misc]
	with pytest.raises(dataclasses.FrozenInstanceError):
		root_v1.rules[0].pattern = 'other/'  # type: ignore[misc]


def test_explain_reports_source_and_line():
	evaluator = HierIgnoreEvaluator.from_texts(
		"*.log\n",
		{'logs': "!today.log\n"},
	)
	text = evaluator.check('logs/today.log').explain()
	assert 'logs/.gitignore:1' in text
	assert '!today.log' in text
	assert 'not ignored' in text

	text = evaluator.check('logs/audit.log').explain()
	assert '.gitignore:1' in text
	assert 'ignored' in text


def test_no_matching_rule_is_not_ignored():
	evaluator = HierIgnoreEvaluator.from_texts("*.log\n")
	decision = evaluator.check('src/main.py')
	assert decision.ignored is False
	assert decision.source is None
	assert decision.lineno is None
	assert 'no matching rule' in decision.explain()


def test_path_escaping_root_rejected():
	evaluator = HierIgnoreEvaluator.from_texts("*.log\n")
	with pytest.raises(ValueError):
		evaluator.check('../outside.log')
