#!/usr/bin/env python3
"""Integration regressions for TopGit startup and Git hook installation."""

import hashlib
import itertools
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


LEGACY_EXEC = ('if which "tg" > /dev/null; then exec '
               '"$(tg --hooks-path)"/pre-commit "$@"; fi\n')
LEGACY_CHAIN = ('if which "tg" > /dev/null; then '
                '"$(tg --hooks-path)"/pre-commit "$@" || exit $?; fi\n')
SUCCESSOR_EXEC = ('if command -v "tg" >/dev/null 2>&1; then exec '
                  '"$("tg" --hooks-path)"/pre-commit "$@"; fi\n')
SUCCESSOR_CHAIN = ('if command -v "tg" >/dev/null 2>&1; then '
                   '"$("tg" --hooks-path)"/pre-commit "$@" || exit $?; fi\n')


class WorktreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix="topgit-tests-")
        cls.root = Path(cls.sandbox.name)
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        cls.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_EDITOR="true",
                       GIT_AUTHOR_NAME="TopGit Test", GIT_AUTHOR_EMAIL="tg@example.org",
                       GIT_COMMITTER_NAME="TopGit Test", GIT_COMMITTER_EMAIL="tg@example.org")
        installed = os.environ.get("TOPGIT_TEST_PREFIX")
        if installed:
            cls.prefix = Path(installed).resolve()
        else:
            source = Path(__file__).resolve().parents[1]
            build = cls.root / "build"
            shutil.copytree(source, build, ignore=shutil.ignore_patterns(
                ".git", "trash directory*", "test-results", "TG-TEST-CACHE", ".prove"))
            cls.prefix = cls.root / "install"
            subprocess.run(["make", "install", "prefix=" + str(cls.prefix)], cwd=build,
                           env=cls.env, check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        cls.tg = cls.prefix / "bin/tg"
        cls.hook_path = cls.prefix / "libexec/topgit/hooks"
        cls.env["PATH"] = str(cls.prefix / "bin") + os.pathsep + cls.env["PATH"]

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root, prefix="case-"))
        self.repo = self.case / "repo"
        self.run_cmd("git", "init", "-q", "--template=", str(self.repo))
        self.git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        (self.repo / "file").write_text("seed\n")
        self.git(self.repo, "add", "file")
        self.git(self.repo, "commit", "-qm", "seed")

    def run_cmd(self, *args, cwd=None, env=None, ok=True):
        result = subprocess.run(args, cwd=cwd or self.case, env=env or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, cwd, *args, **kwargs):
        return self.run_cmd("git", *args, cwd=cwd, **kwargs)

    def initialize(self, cwd):
        helper = self.case / "tg"
        helper.write_text('#!/bin/sh\ntg__include=1\n. "$1"\nsetup_ours\nsetup_hook pre-commit\n')
        self.run_cmd("sh", str(helper), str(self.tg), cwd=cwd)

    def git_path(self, cwd, name):
        value = self.git(cwd, "rev-parse", "--git-path", name).stdout.strip()
        return (cwd / value).resolve()

    def linked(self):
        worktree = self.case / "linked"
        self.git(self.repo, "worktree", "add", "-qb", "linked", str(worktree), "main")
        return worktree

    def write_hook(self, directory, contents):
        directory.mkdir(parents=True, exist_ok=True)
        hook = directory / "pre-commit"
        hook.write_text(contents)
        hook.chmod(0o755)
        return hook

    def snapshot(self, directory):
        result = {}
        for path in [directory, *directory.rglob("*")]:
            stat = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            result[str(path.relative_to(directory))] = (stat.st_mode, stat.st_size,
                                                       stat.st_mtime_ns, digest)
        return result

    def test_lookup_without_git_or_repository(self):
        outside = self.case / "outside"
        outside.mkdir()
        env = dict(self.env, PATH=str(outside))
        (outside / "git").write_text("#!/bin/sh\necho called > git-was-called\nexit 99\n")
        (outside / "git").chmod(0o755)
        result = self.run_cmd(str(self.tg), "--hooks-path", cwd=outside, env=env)
        self.assertEqual(result.stdout, str(self.hook_path) + "\n")
        self.assertEqual(result.stderr, "")
        self.assertFalse((outside / "git-was-called").exists())

    def test_lookup_never_initializes_repositories(self):
        for checkout in (self.repo, self.linked()):
            with self.subTest(checkout=checkout):
                before = self.snapshot(self.case)
                self.assertEqual(self.run_cmd(str(self.tg), "--hooks-path", cwd=checkout).stdout,
                                 str(self.hook_path) + "\n")
                self.assertEqual(self.snapshot(self.case), before)

    def test_commits_and_attributes_in_repository_matrix(self):
        # Each combination uses a fresh repository, including missing hook dirs.
        for layout, head, location in itertools.product(
                ("main", "linked"), ("ordinary", "detached", "topgit"),
                ("default", "absolute", "relative")):
            with self.subTest(layout=layout, head=head, hooks=location):
                self.setUp()
                checkout = self.repo if layout == "main" else self.linked()
                if head == "detached":
                    self.git(checkout, "checkout", "-q", "--detach")
                if location != "default":
                    path = str(self.case / "absolute hooks") if location == "absolute" else "local hooks"
                    self.git(checkout, "config", "core.hooksPath", path)
                expected_hook = self.git_path(checkout, "hooks") / "pre-commit"
                attributes = self.repo / ".git/info/attributes"
                attributes.parent.mkdir(exist_ok=True)
                attributes.write_text("*.existing text\n")
                nested = checkout / "nested"
                nested.mkdir()
                self.initialize(nested)
                self.assertTrue(expected_hook.is_file())
                self.assertTrue(os.access(expected_hook, os.X_OK))
                self.assertTrue(attributes.read_text().startswith("*.existing text\n"))
                for file in (".topmsg", ".topdeps"):
                    self.assertEqual(self.git(checkout, "check-attr", "merge", "--", file).stdout,
                                     file + ": merge: ours\n")
                if layout == "linked":
                    private = Path(self.git(checkout, "rev-parse", "--git-dir").stdout.strip())
                    self.assertFalse((private / "hooks").exists())
                    self.assertFalse((private / "info/attributes").exists())
                stable = (expected_hook.read_bytes(), expected_hook.stat().st_mtime_ns,
                          attributes.read_bytes(), attributes.stat().st_mtime_ns)
                self.initialize(checkout)
                self.assertEqual((expected_hook.read_bytes(), expected_hook.stat().st_mtime_ns,
                                  attributes.read_bytes(), attributes.stat().st_mtime_ns), stable)
                if head == "topgit":
                    self.run_cmd(str(self.tg), "create", "t/topic", "main", cwd=checkout)
                (checkout / "file").write_text("changed\n")
                self.git(checkout, "add", "file")
                self.git(checkout, "commit", "-qm", "with hook enabled")
                if head == "topgit":
                    # A genuine TopGit check must still reject bad metadata.
                    self.git(checkout, "rm", "-q", ".topmsg")
                    result = self.git(checkout, "commit", "-qm", "invalid topic", ok=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(".topmsg is missing", result.stderr)
                self.assertFalse(list(self.repo.glob(".git/**/tg-tmp.*")))

    def test_original_shared_wrapper_in_fresh_detached_worktree(self):
        hook = self.write_hook(self.repo / ".git/hooks", "#!/bin/sh\n" + LEGACY_EXEC)
        checkout = self.linked()
        self.git(checkout, "checkout", "-q", "--detach")
        private = Path(self.git(checkout, "rev-parse", "--git-dir").stdout.strip())
        self.assertFalse((private / "hooks").exists())
        (checkout / "file").write_text("through original wrapper\n")
        self.git(checkout, "add", "file")
        self.git(checkout, "commit", "-qm", "original reproducer")
        self.assertFalse((private / "hooks").exists())
        # Read-only hook execution leaves the legacy wrapper functional; an
        # explicit installation upgrades it without touching private metadata.
        self.initialize(checkout)
        self.assertNotIn(LEGACY_EXEC, hook.read_text())
        self.assertIn("tg_hooks_path=", hook.read_text())

    def test_preserve_existing_hook_and_upgrade_legacy_chain(self):
        for old in ("", LEGACY_CHAIN, LEGACY_EXEC, SUCCESSOR_CHAIN, SUCCESSOR_EXEC):
            with self.subTest(old=old):
                original = '#!/bin/sh\nprintf "%s\\n" "$@" >> hook-ran\nexit 23\n'
                hook = self.write_hook(self.repo / ".git/hooks", "#!/bin/sh\n" + old + original)
                self.initialize(self.repo)
                self.assertIn(original, hook.read_text())
                self.assertNotIn(LEGACY_EXEC, hook.read_text())
                self.assertNotIn(LEGACY_CHAIN, hook.read_text())
                self.assertNotIn(SUCCESSOR_EXEC, hook.read_text())
                self.assertNotIn(SUCCESSOR_CHAIN, hook.read_text())
                self.assertEqual(hook.read_text().count("--hooks-path"), 1)
                result = self.run_cmd(str(hook), "one two", "three", cwd=self.repo, ok=False)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertEqual((self.repo / "hook-ran").read_text(), "one two\nthree\n")
                (self.repo / "hook-ran").unlink()

    def test_wrapper_lookup_failure_and_empty_result(self):
        self.initialize(self.repo)
        hook = self.repo / ".git/hooks/pre-commit"
        fake = self.case / "fake"
        fake.mkdir()
        tg = fake / "tg"
        env = dict(self.env, PATH=str(fake) + os.pathsep + self.env["PATH"])
        for body, status in (("echo /ignored; exit 19", 19), ("exit 0", 1)):
            with self.subTest(body=body):
                tg.write_text("#!/bin/sh\n" + body + "\n")
                tg.chmod(0o755)
                result = self.run_cmd(str(hook), cwd=self.repo, env=env, ok=False)
                self.assertEqual(result.returncode, status)
                self.assertNotIn("/pre-commit: not found", result.stderr)
                if status == 1:
                    self.assertIn("empty hooks path", result.stderr)

    def test_explicitly_disabled_hooks(self):
        self.git(self.repo, "config", "core.hooksPath", os.devnull)
        self.initialize(self.repo)
        self.assertFalse((self.repo / ".git/hooks/pre-commit").exists())

    def test_index_tree_survives_read_only_topic_commands(self):
        self.run_cmd(str(self.tg), "create", "t/topic", "main", cwd=self.repo)
        (self.repo / "file").write_text("indexed change\n")
        self.git(self.repo, "add", "file")
        self.assertEqual(self.run_cmd(str(self.tg), "files", "-i", cwd=self.repo).stdout, "file\n")
        tree = self.git(self.repo, "write-tree").stdout.strip()
        self.git(self.repo, "cat-file", "-e", tree + "^{tree}")
        self.assertEqual(self.run_cmd(str(self.tg), "files", "-w", cwd=self.repo).stdout, "file\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
