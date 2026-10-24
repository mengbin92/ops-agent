"""Regression coverage for the 2026-10-24 execution safety review."""
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core
import execution


class ExecutionSafetyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.patch = mock.patch.object(core, "STATE_DIR", self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_shell_decoded_write_options_require_approval(self):
        victim = self.root / "victim"
        victim.write_text("keep")
        quoted = shlex.quote(str(victim))
        commands = [
            f"find {quoted} -de''lete",
            f"find {quoted} -de\\lete",
            f"find {quoted} -ex''ec touch {quoted} +",
            f"find {quoted} -fpr''int {shlex.quote(str(self.root / 'output'))}",
            "journalctl --vac''uum-time=1s",
            "journalctl --rot",
            "journalctl --flus",
            "docker compose config -''o /tmp/unused",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
                with mock.patch.object(core, "run_shell") as run:
                    self.assertEqual(core.exec_change(command)[0], 2)
                    self.assertEqual(core.check_snapshot_cmd(command)[0], 2)
                    run.assert_not_called()
                self.assertEqual(victim.read_text(), "keep")

    def test_read_commands_cannot_select_executable_helpers_or_write_logs(self):
        commands = [
            "git ls-remote --upload-pack='touch /tmp/unused' .",
            "git ls-remote --up''load-pack='touch /tmp/unused' .",
            "git ls-remote --upload-pac='touch /tmp/unused' .",
            "git diff --ext-diff",
            "git diff --ext-di",
            "git show --textconv HEAD:file",
            "git grep -O'touch /tmp/unused' pattern",
            "git grep -nOcat pattern",
            "less '+!touch /tmp/unused' /etc/hosts",
            "less -o/tmp/unused /etc/hosts",
            "less -SO/tmp/unused /etc/hosts",
            "less --log-file=/tmp/unused /etc/hosts",
            "less --log-fil=/tmp/unused /etc/hosts",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
                self.assertEqual(core.check_snapshot_cmd(command)[0], 2)

    def test_ordinary_read_commands_still_allowed(self):
        for command in ["find /tmp -name '*.log'", "git log --oneline -5",
                        "git diff HEAD~1", "git grep -n pattern", "less -S /etc/hosts",
                        "cat -- /etc/hosts", "git diff --no-ext-diff --no-textconv",
                        "journalctl -u nginx --since yesterday", "kubectl get pods 2>&1"]:
            with self.subTest(command=command):
                self.assertEqual(core.classify(command)[0], "R0")

    def test_dynamic_shell_parameters_cannot_synthesize_write_options(self):
        for command in ["find /tmp/victim -de${PATH:+lete}",
                        'find /tmp/victim "${PATH:+-de}${PATH:+lete}"',
                        "find /tmp/victim $FLAGS", "cat $HOME/file",
                        "echo $HOME", "find /tmp/victim $'-delete'"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
        for command in ["echo '$HOME'", r"echo \$HOME", "grep '$' /etc/hosts",
                        'echo "\'$HOME\'"']:
            with self.subTest(command=command):
                expected = "R2" if command == 'echo "\'$HOME\'"' else "R0"
                self.assertEqual(core.classify(command)[0], expected)

    def test_glob_and_brace_options_cannot_bypass_guard(self):
        # Match run_shell's default /bin/sh. On Darwin it supports brace
        # expansion; pathname expansion is also reproducible on Linux /bin/sh.
        (self.root / "-delete").write_text("glob marker")
        victim = self.root / "victim"
        victim.write_text("keep")
        demonstration = subprocess.run("find victim -de*", shell=True, cwd=self.root,
                                       capture_output=True, timeout=5)
        self.assertEqual(demonstration.returncode, 0)
        self.assertFalse(victim.exists())
        victim.write_text("keep")
        demonstration = subprocess.run("find victim *delete", shell=True, cwd=self.root,
                                       capture_output=True, timeout=5)
        self.assertEqual(demonstration.returncode, 0)
        self.assertFalse(victim.exists())
        victim.write_text("keep")
        for command in ["find victim -de*", "find victim *", "find victim *delete",
                        "find victim -delet?",
                        "find victim -{delete,print}", "find victim -de{lete,lete}",
                        "find victim {-delete,-print}", "git diff --ext-*",
                        "less -[oO] logfile /etc/hosts"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
                with mock.patch.object(core, "run_shell") as run:
                    self.assertEqual(core.exec_change(command)[0], 2)
                    self.assertEqual(core.check_snapshot_cmd(command)[0], 2)
                    run.assert_not_called()
                self.assertEqual(victim.read_text(), "keep")
        for command in ["find /tmp/* -print", "find ./v* -name '-de*'",
                        "find /tmp/{first,second} -print", "ls *", "cat /tmp/*.log",
                        "grep '[a-z]*' file", "grep 'a{1,2}' file", "grep a{1,2} file"]:
            with self.subTest(command=command):
                self.assertEqual(core.classify(command)[0], "R0")

    def test_kubectl_dynamic_resource_names_cannot_bypass_secret_gate(self):
        for command in ["kubectl get se{cret,crets} -oyaml", "kubectl get se* -oyaml",
                        "kubectl get se?rets -ojson", "kubectl get 'se'cre* -oyaml",
                        "kubectl config view --r{aw,aw} --context=test",
                        "KUBECONFIG=/tmp/cluster kubectl get se* -oyaml"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
                self.assertIsNotNone(core.sensitive_command(command))
                # Explicit approval must not override sensitive-read protection.
                core.approve(command)
                self.assertEqual(core.check(command)[0], 2)
        for command in ["kubectl get pods -o 'jsonpath={.items[*].metadata.name}'",
                        "kubectl get pods -o 'go-template={{.kind}}'",
                        "kubectl get -f /tmp/*.yaml", "kubectl get -f ./resources/*"]:
            with self.subTest(command=command):
                self.assertEqual(core.classify(command)[0], "R0")
                self.assertIsNone(core.sensitive_command(command))

    def test_kubeconfig_prefix_cannot_bypass_command_specific_write_checks(self):
        for command in ["git ls-remote --upload-pack='touch /tmp/unused' .",
                        "git grep -Ocat pattern", "git diff --ext-diff",
                        "less '+!touch /tmp/unused' /etc/hosts",
                        "less -o/tmp/unused /etc/hosts", "docker compose config -o/tmp/unused"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify("KUBECONFIG=/tmp/cluster " + command)[0], "R0")

    def test_content_globs_cannot_read_sensitive_matches_or_symlink_targets(self):
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            (self.root / ".env").write_text("unlabelled-private-material")
            (self.root / "alias.txt").symlink_to(self.root / ".env")
            (self.root / "credentials").symlink_to(self.root / ".env")
            for command in ["cat .env*", "cat .*", "head alias*.txt", "cat cred*",
                            "cat .{env,log}", "cat {.,}env"]:
                with self.subTest(command=command):
                    self.assertIsNotNone(core.sensitive_command(command))
                    with mock.patch.object(core, "run_shell") as run:
                        self.assertEqual(core.exec_change(command)[0], 2)
                        run.assert_not_called()
            command = "cat .env* > captured"
            core.approve(command)
            self.assertTrue(core.check_stamp(command))
            self.assertEqual(core.check(command)[0], 2)
            with mock.patch.object(core, "run_shell") as run:
                self.assertEqual(core.exec_change(command)[0], 2)
                run.assert_not_called()
            self.assertFalse((self.root / "captured").exists())
        finally:
            os.chdir(previous)

    def test_local_log_globs_and_quoted_grep_patterns_remain_usable(self):
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            logs = self.root / "logs"
            logs.mkdir()
            (logs / "first.log").write_text("plain first log\n")
            (logs / "second.log").write_text("plain second log\n")
            # A secret matching a quoted regex is not an input path.
            (self.root / ".env").write_text("unlabelled-private-material")
            for command in ["cat logs/*.log", "cat ./logs/*.log",
                            "grep '.*' logs/*.log", "grep '.env*' logs/first.log",
                            "grep 'a{1,2}' logs/first.log", "ls .*", "find ./logs/* -print"]:
                with self.subTest(command=command):
                    self.assertIsNone(core.sensitive_command(command))
                    self.assertEqual(core.check(command)[0], 0)
            code, _, output = core.exec_change("cat logs/*.log")
            self.assertEqual(code, 0)
            self.assertIn("plain first log", output)
            self.assertIn("plain second log", output)
        finally:
            os.chdir(previous)

    def test_remote_content_globs_are_rejected_without_local_inference(self):
        for command in ["cat /var/log/*.log", "grep error /var/log/app?.log",
                        "cat /tmp/.env*", "head /tmp/{one,two}.log"]:
            with self.subTest(command=command):
                self.assertIsNotNone(core.sensitive_command(command, strict=True))
                self.assertEqual(core.check(command, target="uncontacted-test-target")[0], 2)
        for command in ["ls /var/log/*", "find /var/log/* -print",
                        "grep '.*' /var/log/app.log", "grep 'a{1,2}' /var/log/app.log"]:
            with self.subTest(command=command):
                self.assertIsNone(core.sensitive_command(command, strict=True))

    def test_content_glob_match_count_is_bounded(self):
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            for i in range(3):
                (self.root / f"plain-{i}.log").write_text("plain log")
            with mock.patch.object(core, "MAX_SENSITIVE_GLOB_MATCHES", 2):
                reason = core.sensitive_command("cat plain-*.log")
                self.assertIn("超过 2 项", reason)
                self.assertEqual(core.check("cat plain-*.log")[0], 2)
        finally:
            os.chdir(previous)

    def test_kubectl_quoted_secret_and_raw_api_cannot_bypass_sensitive_gate(self):
        for command in ["kubectl get se''crets -oyaml", r"kubectl get se\crets -ojson",
                        "kubectl config view --r''aw", "kubectl get --raw=/api/v1/secrets",
                        "kubectl get --r''aw /api/v1/namespaces/default/secrets",
                        "kubectl get secrets --out''put=yaml"]:
            with self.subTest(command=command):
                self.assertIsNotNone(core.sensitive_command(command))
                code, message = core.check(command)
                self.assertEqual(code, 2)
                self.assertIn("敏感", message)
                self.assertEqual(core.check_snapshot_cmd(command)[0], 2)
        for command in ["kubectl get secrets", "kubectl get secret -o name",
                        "kubectl config view"]:
            with self.subTest(command=command):
                self.assertIsNone(core.sensitive_command(command))
                self.assertEqual(core.check(command)[0], 0)

    def test_parent_symlink_retarget_cannot_redirect_local_rollback(self):
        original = self.root / "original"
        original.mkdir()
        source = original / "config"
        source.write_text("saved")
        sid = core.snapshot_file(str(source))
        original.rename(self.root / "old")
        alternate = self.root / "alternate"
        alternate.mkdir()
        (alternate / "config").write_text("untouched")
        original.symlink_to(alternate, target_is_directory=True)
        with self.assertRaisesRegex(core.OpsxError, "父目录真实路径已改变"):
            core.rollback_plan(sid)
        with self.assertRaisesRegex(core.OpsxError, "父目录真实路径已改变"):
            core.rollback(sid, confirm=True)
        self.assertEqual((alternate / "config").read_text(), "untouched")
        self.assertEqual((self.root / "old" / "config").read_text(), "saved")
        records = execution.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "aborted")

    def test_final_component_symlink_is_replaced_without_writing_through(self):
        source = self.root / "config"
        source.write_text("saved")
        sid = core.snapshot_file(str(source))
        other = self.root / "other"
        other.write_text("untouched")
        source.unlink()
        source.symlink_to(other)
        core.rollback(sid, confirm=True)
        self.assertFalse(source.is_symlink())
        self.assertEqual(source.read_text(), "saved")
        self.assertEqual(other.read_text(), "untouched")

    def test_invalid_snapshot_environment_is_a_controlled_metadata_error(self):
        source = self.root / "config"
        source.write_text("saved")
        sid = core.snapshot_file(str(source))
        directory, meta = core.load_snapshot(sid)
        meta["env"] = ["invalid"]
        (directory / "meta.json").write_text(json.dumps(meta))
        with self.assertRaisesRegex(core.OpsxError, "快照环境非法"):
            core.rollback_plan(sid)


if __name__ == "__main__":
    unittest.main()
