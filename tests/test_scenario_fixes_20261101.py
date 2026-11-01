"""Audit fixes 2026-11-01: placeholder validation at load, reserved param names,
usage ledger rotation."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import remote
import scenario
import usage


class AuditFixesTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.dirs_patch = patch.object(scenario, "_DIR_CACHE", [self.state / "scenarios",
                                                                core.REPO_DIR / "scenarios"])
        self.dirs_patch.start()
        self.addCleanup(self.dirs_patch.stop)

    def write_scenario(self, name, body):
        path = self.state / "scenarios" / (name + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")

    def execution_records(self):
        records = self.state / "executions"
        return list(records.glob("*.json")) if records.is_dir() else []


class LoadTimePlaceholderValidationTests(AuditFixesTestBase):
    def test_bad_postcheck_placeholder_rejected_at_load_naming_field(self):
        self.write_scenario("typo-postcheck", {
            "params": {"container": {"required": True}},
            "exec": {"cmd": "docker restart {container}"},
            "postcheck": [{"cmd": "docker inspect {contaner}"}]})
        with self.assertRaises(scenario.ScenarioError) as ctx:
            scenario.load("typo-postcheck")
        self.assertIn("postcheck[1].cmd", str(ctx.exception))
        self.assertIn("{contaner}", str(ctx.exception))

    def test_undeclared_placeholder_rejected_in_every_command_field(self):
        cases = {
            "precheck": {"exec": {"cmd": "echo hi"},
                         "precheck": [{"cmd": "echo {oops}"}]},
            "exec.cmd": {"exec": {"cmd": "echo {oops}"}},
            "snapshot_files": {"exec": {"cmd": "echo hi",
                                        "snapshot_files": ["/etc/{oops}"]}},
            "snapshot_cmds": {"exec": {"cmd": "echo hi",
                                       "snapshot_cmds": [{"command": "cat /etc/{oops}"}]}},
        }
        for name, body in cases.items():
            with self.subTest(field=name):
                self.write_scenario("oops-" + name.replace(".", "-"), body)
                with self.assertRaisesRegex(scenario.ScenarioError, r"\{oops\}"):
                    scenario.load("oops-" + name.replace(".", "-"))

    def test_bad_postcheck_placeholder_never_reaches_execution(self):
        self.write_scenario("typo-postcheck", {
            "params": {"container": {"required": True}},
            "exec": {"cmd": "docker restart {container}"},
            "postcheck": [{"cmd": "docker inspect {contaner}"}]})
        with patch.object(core, "exec_change") as local_exec, \
                patch.object(remote, "exec_change") as remote_exec:
            with self.assertRaises(scenario.ScenarioError):
                scenario.run("typo-postcheck", {"container": "web"})
        local_exec.assert_not_called()
        remote_exec.assert_not_called()
        self.assertEqual(self.execution_records(), [])

    def test_declared_but_unfilled_postcheck_param_fails_before_exec(self):
        # load() 只能证明占位符已声明；"声明了却没默认值也没传入"的可选参数
        # 必须在变更执行前（保险丝替换）报出来，而不是执行后才炸。
        self.write_scenario("optional-postcheck", {
            "params": {"container": {"pattern": "^[a-z]+$"}},
            "exec": {"cmd": "echo changed"},
            "postcheck": [{"cmd": "docker inspect {container}"}]})
        scenario.load("optional-postcheck")  # 声明过，load 不报
        with patch.object(core, "exec_change") as local_exec, \
                patch.object(remote, "exec_change") as remote_exec:
            with self.assertRaisesRegex(scenario.ScenarioError, r"\{container\}"):
                scenario.run("optional-postcheck")
        local_exec.assert_not_called()
        remote_exec.assert_not_called()
        self.assertEqual(self.execution_records(), [])

    def test_built_in_scenarios_still_load(self):
        for entry in scenario.catalog():
            self.assertFalse(entry["broken"], entry)


class ReservedParamNameTests(AuditFixesTestBase):
    def test_go_template_token_param_names_rejected_at_load(self):
        for key in ("end", "else", "if"):
            with self.subTest(param=key):
                self.write_scenario("reserved-" + key, {
                    "params": {key: {"required": True}},
                    "exec": {"cmd": "echo {" + key + "}"}})
                with self.assertRaisesRegex(scenario.ScenarioError, key):
                    scenario.load("reserved-" + key)

    def test_normal_param_does_not_touch_go_template_braces(self):
        # 非保留名参数替换 docker 模板命令时，{{...}} 必须原样保留。
        out = scenario.substitute(
            "docker inspect --format '{{if .State.Health}}{{else}}none{{end}}' {container}",
            {"container": "web"})
        self.assertEqual(
            out, "docker inspect --format "
                 "'{{if .State.Health}}{{else}}none{{end}}' web")


class UsageLedgerRotationTests(AuditFixesTestBase):
    def test_rotation_archives_generation_and_recent_spans_both(self):
        with patch.object(usage, "USAGE_ROTATE_BYTES", 1):
            usage.record("model", 10, 10)
            self.assertFalse((self.state / "usage.jsonl.1").exists())
            usage.record("model", 20, 20)
        archived = self.state / "usage.jsonl.1"
        self.assertTrue(archived.is_file())          # 被归档的上一代账单
        self.assertTrue((self.state / "usage.jsonl").is_file())
        recs = usage.recent()
        self.assertEqual([r["prompt_tokens"] for r in recs], [10, 20])
        today = usage.today()
        self.assertEqual((today["calls"], today["tokens"]), (2, 60))

    def test_concurrent_records_rotate_without_loss_or_deadlock(self):
        # 轮转在 state 锁内：并发记账时计数不丢（锁的保护范围），
        # 两个账单文件的每一行都仍是合法 JSON（无撕裂/无半个写入）。
        def hammer():
            for _ in range(10):
                usage.record("model", 1, 1)

        with patch.object(usage, "USAGE_ROTATE_BYTES", 1):
            threads = [threading.Thread(target=hammer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(usage.today()["calls"], 40)
        for name in ("usage.jsonl", "usage.jsonl.1"):
            path = self.state / name
            self.assertTrue(path.is_file())
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)  # 撕裂行会让这里炸
        self.assertGreaterEqual(len(usage.recent(tail=100)), 1)


if __name__ == "__main__":
    unittest.main()
