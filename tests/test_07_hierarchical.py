"""
This script tests :class:`.HierarchicalIgnoreEvaluator` and
:class:`.IgnoreSnapshot`.
"""
from __future__ import annotations

import unittest

from pathspec.hierarchical import (
	HierarchicalIgnoreEvaluator,
	IgnoreResult,
	IgnoreSnapshot,
	RuleSource,
)


class HierarchicalIgnoreEvaluatorTest(unittest.TestCase):
	"""
	The :class:`HierarchicalIgnoreEvaluatorTest` class tests the
	:class:`.HierarchicalIgnoreEvaluator` class.
	"""

	def test_acceptance_parent_excluded_blocks_child(self):
		"""
		Test the acceptance criteria: root rule ignores build/, sub-rule
		!keep.txt cannot rescue build/keep.txt alone.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["build/"],
			subdir_lines={"build": ["!keep.txt"]},
		)
		evaluator = snapshot.evaluator()

		# build/keep.txt should be ignored because build/ is excluded
		result = evaluator.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)
		self.assertEqual(result.source.file, ".gitignore")
		self.assertEqual(result.source.line, 1)
		self.assertEqual(result.source.pattern, "build/")

		# build/ directory should be ignored
		result = evaluator.evaluate("build", is_dir=True)
		self.assertTrue(result.ignored)

		# build/keep.txt should still be ignored even with !keep.txt
		result = evaluator.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)

	def test_parent_reincluded_allows_child(self):
		"""
		Test that re-including the parent directory allows sub-rules to take
		effect.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["build/", "!build/"],
			subdir_lines={"build": ["!keep.txt"]},
		)
		evaluator = snapshot.evaluator()

		# build/keep.txt should not be ignored because build/ is re-included
		result = evaluator.evaluate("build/keep.txt")
		self.assertFalse(result.ignored)
		self.assertEqual(result.source.file, "build/.gitignore")
		self.assertEqual(result.source.line, 1)
		self.assertEqual(result.source.pattern, "!keep.txt")

	def test_nested_directories(self):
		"""
		Test nested directory hierarchies.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["build/"],
			subdir_lines={
				"build": ["output/"],
				"build/output": ["!keep.txt"],
			},
		)
		evaluator = snapshot.evaluator()

		# build/output/keep.txt should be ignored because build/ is excluded
		result = evaluator.evaluate("build/output/keep.txt")
		self.assertTrue(result.ignored)

		# build/output/ directory should be ignored
		result = evaluator.evaluate("build/output", is_dir=True)
		self.assertTrue(result.ignored)

		# Re-include build/ and output/ to allow !keep.txt
		snapshot2 = IgnoreSnapshot.from_lines(
			root_lines=["build/", "!build/"],
			subdir_lines={
				"build": ["output/", "!output/"],
				"build/output": ["!keep.txt"],
			},
		)
		evaluator2 = snapshot2.evaluator()

		result = evaluator2.evaluate("build/output/keep.txt")
		self.assertFalse(result.ignored)

	def test_backslash_normalization(self):
		"""
		Test that backslashes are normalized to forward slashes.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["build/"],
		)
		evaluator = snapshot.evaluator()

		# Test with backslashes
		result = evaluator.evaluate("build\\keep.txt")
		self.assertTrue(result.ignored)

		# Test with mixed separators
		result = evaluator.evaluate("build/output\\keep.txt")
		self.assertTrue(result.ignored)

		# Test directory with backslashes
		result = evaluator.evaluate("build\\output", is_dir=True)
		self.assertTrue(result.ignored)

	def test_snapshot_isolation(self):
		"""
		Test that rule updates create new snapshots and do not modify existing
		evaluators.
		"""
		# Create initial snapshot
		snapshot1 = IgnoreSnapshot.from_lines(
			root_lines=["build/"],
		)
		evaluator1 = snapshot1.evaluator()

		# Verify initial behavior
		result = evaluator1.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)

		# Create a new snapshot with updated rules
		snapshot2 = IgnoreSnapshot.from_lines(
			root_lines=["build/", "!build/"],
		)
		evaluator2 = snapshot2.evaluator()

		# Verify new behavior
		result = evaluator2.evaluate("build/keep.txt")
		self.assertFalse(result.ignored)

		# Verify old evaluator is not modified
		result = evaluator1.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)

		# Verify snapshots are independent
		result = evaluator1.evaluate("other.txt")
		self.assertFalse(result.ignored)

		result = evaluator2.evaluate("other.txt")
		self.assertFalse(result.ignored)

	def test_line_number_preserves_blank_lines(self):
		"""
		Test that source line numbers preserve blank lines and comments.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=[
				"",           # line 1: blank
				"# comment",  # line 2: comment
				"",           # line 3: blank
				"build/",     # line 4: pattern
			],
		)
		evaluator = snapshot.evaluator()

		result = evaluator.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)
		self.assertEqual(result.source.line, 4)

	def test_source_attribution(self):
		"""
		Test that source file and line number are correctly attributed.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["*.log"],
			subdir_lines={"build": ["!keep.txt"]},
			root_source="root.gitignore",
			subdir_sources={"build": "build.gitignore"},
		)
		evaluator = snapshot.evaluator()

		# File matched by root rule
		result = evaluator.evaluate("test.log")
		self.assertTrue(result.ignored)
		self.assertEqual(result.source.file, "root.gitignore")
		self.assertEqual(result.source.line, 1)
		self.assertEqual(result.source.pattern, "*.log")

		# File matched by subdir rule (after parent re-inclusion)
		snapshot2 = IgnoreSnapshot.from_lines(
			root_lines=["build/", "!build/"],
			subdir_lines={"build": ["!keep.txt"]},
			root_source="root.gitignore",
			subdir_sources={"build": "build.gitignore"},
		)
		evaluator2 = snapshot2.evaluator()

		result = evaluator2.evaluate("build/keep.txt")
		self.assertFalse(result.ignored)
		self.assertEqual(result.source.file, "build.gitignore")
		self.assertEqual(result.source.line, 1)
		self.assertEqual(result.source.pattern, "!keep.txt")

	def test_directory_vs_file_judgment(self):
		"""
		Test that directory and file paths are judged separately.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["build/"],
		)
		evaluator = snapshot.evaluator()

		# Directory path
		result = evaluator.evaluate("build", is_dir=True)
		self.assertTrue(result.ignored)

		# File path under the directory
		result = evaluator.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)

		# Non-existent path (treated as file) - directory pattern does not
		# match a file named "build"
		result = evaluator.evaluate("build")
		self.assertFalse(result.ignored)

	def test_no_match_returns_included(self):
		"""
		Test that paths with no matching rules are included.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=["build/"],
		)
		evaluator = snapshot.evaluator()

		result = evaluator.evaluate("other.txt")
		self.assertFalse(result.ignored)
		self.assertIsNone(result.source)

	def test_bytes_input(self):
		"""
		Test that bytes input is handled correctly.
		"""
		snapshot = IgnoreSnapshot.from_lines(
			root_lines=[b"build/"],
			subdir_lines={"build": [b"!keep.txt"]},
		)
		evaluator = snapshot.evaluator()

		result = evaluator.evaluate("build/keep.txt")
		self.assertTrue(result.ignored)


if __name__ == "__main__":
	unittest.main()
