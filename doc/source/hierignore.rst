Hierarchical Ignore Evaluation
==============================

.. currentmodule:: pathspec.hierignore

The :class:`HierIgnoreEvaluator` class evaluates whether a relative path is
ignored, given a root rule snapshot plus per-subdirectory rule snapshots
(e.g. one ``.gitignore`` per directory). It explains which source file and
rule line ultimately took effect. It only consumes the paths and rules it is
given; it never scans the file system.

Semantics
---------

-	Each snapshot is scoped to its own directory. Patterns match paths
	relative to that directory.

-	Rules from a deeper directory take precedence over rules from a
	shallower one. Within one snapshot, the last matching line wins.

-	When a parent directory is excluded, rules below it cannot re-include
	individual files. The parent directory itself must be re-included first
	(e.g. ``!build/``) by a rule governing it.

-	Directories and files are judged separately: a directory-only pattern
	(``build/``) never matches a file path.

-	Snapshots and evaluators are immutable. Updating rules means creating a
	new snapshot and deriving a new evaluator with
	:meth:`HierIgnoreEvaluator.with_snapshot`; existing evaluators are
	unchanged.

-	Line numbers reported in decisions are 1-based and count blank lines
	and comments.

Example
-------

.. code:: python

	from pathspec.hierignore import HierIgnoreEvaluator, IgnoreSnapshot

	# Root rules ignore the whole build/ directory. The snapshot in build/
	# tries to re-include a single file.
	evaluator = HierIgnoreEvaluator.from_texts(
		"build/\n",                      # .gitignore
		{'build': "!keep.txt\n"},        # build/.gitignore
	)

	# A child rule cannot rescue a file inside an excluded directory.
	decision = evaluator.check('build/keep.txt')
	assert decision.ignored
	print(decision.explain())
	# file 'build/keep.txt' is ignored by .gitignore:1 (matched 'build')
	# 'build/': ancestor directory 'build' is excluded; ...

	# Re-include build/ itself: updating rules creates a new snapshot and a
	# new evaluator. The original evaluator is unchanged.
	root_v2 = IgnoreSnapshot.from_text("build/\n!build/\n", source='.gitignore')
	evaluator_v2 = evaluator.with_snapshot('', root_v2)

	decision = evaluator_v2.check('build/keep.txt')
	assert not decision.ignored
	print(decision.explain())
	# file 'build/keep.txt' is not ignored by build/.gitignore:1 '!keep.txt':
	# matched file rule

	# Directories and files are judged separately.
	assert evaluator.check('build', is_dir=True).ignored
	assert not evaluator.check('build', is_dir=False).ignored

	# Backslash paths are normalized.
	assert evaluator.check('build\keep.txt').ignored

Short commands
--------------

Run the hierarchical-ignore tests::

	python -m pytest tests/test_07_hierignore.py -q

Run the built-in demonstration::

	python -m pathspec.hierignore
