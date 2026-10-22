"""W8 playbooks: discoverability, usable read probes and change guardrails."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
from agent import skills


W8_NAMES = ("k8s-crashloop", "docker-restart-loop", "dns-tls", "backup-recovery")
REVIEWED_NAMES = W8_NAMES + ("safe-service-restart", "nginx-5xx")


class W8PlaybookTests(unittest.TestCase):
    def setUp(self):
        # The user's same-name skills must not mask repository deliverables in CI.
        self.repository_skills = core.REPO_DIR / "skills"
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-w8-playbooks-")
        self.addCleanup(self.temp.cleanup)
        self.state_patch = patch.object(core, "STATE_DIR", Path(self.temp.name))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.directories = patch.object(skills, "SEARCH_DIRS", [self.repository_skills])
        self.directories.start()
        self.addCleanup(self.directories.stop)

    def test_catalog_exposes_all_roadmap_playbooks_with_descriptions(self):
        catalog = {item["name"]: item for item in skills.catalog()}
        for name in REVIEWED_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, catalog)
                self.assertTrue(catalog[name]["description"].strip())
                self.assertEqual(catalog[name]["file"], self.repository_skills / f"{name}.md")

    def test_load_returns_complete_untruncated_playbooks_with_optional_extension(self):
        for name in REVIEWED_NAMES:
            with self.subTest(name=name):
                source = (self.repository_skills / f"{name}.md").read_text(encoding="utf-8")
                self.assertLessEqual(len(source), skills.SKILL_LIMIT)
                self.assertEqual(skills.load(name), source)
                self.assertEqual(skills.load(name + ".md"), source)
                self.assertTrue(source.startswith("---\ndescription: "))
                self.assertNotIn("[skill 超过", skills.load(name))

    def test_user_directory_overrides_new_playbook_without_changing_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            text = "---\ndescription: 本机覆盖\n---\n本机运行手册\n"
            (local / "dns-tls.md").write_text(text, encoding="utf-8")
            with patch.object(skills, "SEARCH_DIRS", [self.repository_skills, local]):
                self.assertEqual(skills.load("dns-tls"), text)
                meta = next(item for item in skills.catalog() if item["name"] == "dns-tls")
                self.assertEqual(meta["description"], "本机覆盖")
                self.assertEqual(meta["file"], local / "dns-tls.md")

    def test_each_playbook_has_evidence_stop_approval_and_repeatable_acceptance(self):
        for name in REVIEWED_NAMES:
            with self.subTest(name=name):
                text = skills.load(name)
                for phrase in ("证据清单", "只读命令", "停止条件", "可重复验收",
                               "ops_approve", "ops_exec", "ops_rollback", "人工确认",
                               "unknown", "execution", "不自动重放"):
                    self.assertIn(phrase, text)
                self.assertRegex(text, r"(拒绝|无确认通道)")
                self.assertIn("绕审批", text)

    def test_default_readonly_probe_blocks_pass_current_gate(self):
        for name in ("k8s-crashloop", "docker-restart-loop", "backup-recovery",
                     "safe-service-restart", "nginx-5xx"):
            for block in re.findall(r"```bash\n(.*?)\n```", skills.load(name), re.S):
                for command in block.splitlines():
                    with self.subTest(name=name, command=command):
                        self.assertEqual(core.classify(command)[0], "R0")
                        self.assertIsNone(core.sensitive_command(command))

    def test_kubernetes_evidence_is_scoped_and_keeps_previous_instance(self):
        text = skills.load("k8s-crashloop")
        for command in re.findall(r"^kubectl .+$", text, re.M):
            with self.subTest(command=command):
                self.assertIn("--context=test", command)
                self.assertIn("--namespace=ops", command)
        for phrase in ("--previous", "initContainerStatuses", "restartCount", "UID",
                       "observedGeneration", "Secret", "不能当作 Kubernetes rollout undo"):
            self.assertIn(phrase, text)

    def test_docker_acceptance_does_not_confuse_running_with_business_health(self):
        text = skills.load("docker-restart-loop")
        for phrase in ("RestartCount", "OOMKilled", "完整 ID", "starting", "none",
                       "业务后验", "预检拒绝", "重计观察窗"):
            self.assertIn(phrase, text)
        self.assertNotRegex(text, r"(?m)^docker inspect app\s*$")

    def test_dns_tls_diagnostics_respect_non_r0_gate(self):
        text = skills.load("dns-tls")
        commands = re.findall(r"^(?:dig|openssl|timeout) .+$", text, re.M)
        self.assertGreaterEqual(len(commands), 5)
        for command in commands:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
        for phrase in ("不在 R0 白名单", "ops_approve", "ops_exec", "-servername",
                       "-verify_hostname", "-verify_return_error", "-checkend", "TTL",
                       "不证明服务在线", "不能直接交裸", "unknown"):
            self.assertIn(phrase, text)

    def test_backup_requires_actual_isolated_restore_and_permission_evidence(self):
        text = skills.load("backup-recovery")
        for phrase in ("RPO", "RTO", "数据截止时间", "mtime", "隔离", "属主/组/权限",
                       "路径穿越", "符号链接", "再次在另一个空", "不得重新启用旧",
                       "不能用本地桩测试代替", "原始秘密", "--quiesced",
                       "opsx state backup", "opsx state verify", "opsx state restore"):
            self.assertIn(phrase, text)

    def test_existing_playbooks_use_tool_confirmation_without_duplicate_ask_user(self):
        for name in ("safe-service-restart", "nginx-5xx"):
            with self.subTest(name=name):
                text = skills.load(name)
                self.assertNotIn("ask_user 确认 →", text)
                self.assertNotIn("ask_user 让用户确认", text)
                self.assertNotIn("失败则回滚", text)
                self.assertIn("工具内", text)
                self.assertIn("ops_rollback", text)
                self.assertIn("新的审批" if name == "nginx-5xx" else "另行", text)


if __name__ == "__main__":
    unittest.main()
