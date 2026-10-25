"""Read-only shell regressions from Docker restart-policy diagnostics."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core
import mcp_server
from agent import tools


class DockerReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        patch = mock.patch.object(core, "STATE_DIR", self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def test_docker_templates_are_data_not_shell_syntax(self):
        for template in ["{{.Name}} => {{.HostConfig.RestartPolicy.Name}}",
                         "{{.Name}} | {{.State.Status}}; ready && healthy",
                         "x > y &> z", "tee", "--output", "-exec",
                         "{{range $p, $conf := .NetworkSettings.Ports}}{{$p}}{{end}}",
                         "literal `backticks` and $(text)"]:
            command = "docker inspect --format " + shlex.quote(template) + " app worker"
            with self.subTest(template=template):
                self.assertEqual(core.classify(command)[0], "R0")
                self.assertEqual(core.check(command)[0], 0)

    def test_grep_patterns_and_escaped_operators_remain_readonly(self):
        for command in [
            "docker inspect app | grep -E 'Name|RestartPolicy|\"Name\"'",
            r'grep -v "backups\|preview\|prepared" compose.yml',
            'grep -nE "^  [a-zA-Z0-9_-]+:$" compose.yml',
            "grep -E 'a;b|c&&d|e>f|tee|--output|-exec' compose.yml",
            r"echo left\|right\;value\>limit\&ready",
            "echo '$HOME ${PATH} $(text) `text`'",
            "echo $", "echo '$:'", 'echo "$:"',
        ]:
            with self.subTest(command=command):
                self.assertEqual(core.classify(command)[0], "R0")

    def test_real_write_operators_are_not_hidden_by_quotes_or_comments(self):
        prefix = "docker inspect --format '{{.Name}} => ready; yes|no' app"
        for suffix in [" > /tmp/unused", " >> /tmp/unused", " &> /tmp/unused",
                       " 2>/tmp/unused", " | tee /tmp/unused", "; touch /tmp/unused",
                       " && docker restart app", " || docker restart app",
                       " & docker restart app", "\ndocker restart app",
                       " # comment\ndocker restart app", "''#literal; docker restart app"]:
            with self.subTest(suffix=suffix):
                self.assertNotEqual(core.classify(prefix + suffix)[0], "R0")
        for suffix in [" 2>/dev/null", " 2>&1", " # | ignored > /tmp/unused"]:
            with self.subTest(suffix=suffix):
                self.assertEqual(core.classify(prefix + suffix)[0], "R0")

    def test_shell_expansions_and_unclosed_quotes_still_require_approval(self):
        for value in ["$HOME", "${PATH}", "$(touch /tmp/unused)", "$((1+1))",
                      "$[1+1]", "$1", "$@", "$*", "$#", "$?", "$-", "$!", "$$",
                      "`touch /tmp/unused`"]:
            for quote in ("", '"'):
                command = "echo " + quote + value + quote
                with self.subTest(command=command):
                    self.assertNotEqual(core.classify(command)[0], "R0")
        for command in ["echo $'text'", 'echo $"text"', "docker inspect --format 'unclosed app",
                        'grep "unfinished|pattern compose.yml']:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")

    def test_quoted_redirection_targets_cannot_borrow_a_later_dev_null(self):
        for command in ['echo data 2>"/tmp/unused" /dev/null',
                        "docker inspect app 2>'/tmp/unused' /dev/null",
                        'echo data 2>/dev/null".other"', r"echo data 2>/dev/null\x"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")

    def test_non_shell_whitespace_does_not_turn_hash_into_a_comment(self):
        for space in ("\v", "\r", "\u00a0", "\u2003"):
            with self.subTest(space=repr(space)):
                self.assertNotEqual(core.classify("echo " + space + "#literal; touch /tmp/unused")[0], "R0")

    def test_non_shell_whitespace_is_part_of_redirection_filename(self):
        for space in ("\v", "\r", "\u00a0", "\u2003"):
            for target in ("/dev/null" + space + ".other", space + "/tmp/unused /dev/null"):
                with self.subTest(target=repr(target)):
                    self.assertNotEqual(core.classify("echo data 2>" + target)[0], "R0")

    def test_ssh_checks_local_expansion_before_unwrapping_remote_command(self):
        for command in ['ssh host "echo \'$(touch /tmp/unused)\'"',
                        'ssh host "docker inspect --format \'$HOME\' app"',
                        'ssh host "echo \'`touch /tmp/unused`\'"']:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
        self.assertEqual(core.classify('ssh host "docker inspect app | grep \'Name|RestartPolicy\'"')[0], "R0")

    def test_docker_readonly_aliases(self):
        for command in ["docker container inspect app", "docker container ls -a",
                        "docker container logs --tail 20 app", "docker container top app",
                        "docker image inspect app:latest", "docker image ls",
                        "docker image history app:latest", "docker network inspect app-net",
                        "docker network ls", "docker volume inspect app-data", "docker volume ls",
                        "docker system df", "docker compose images", "docker compose ls"]:
            with self.subTest(command=command):
                self.assertEqual(core.classify(command)[0], "R0")

    def test_compose_output_and_docker_mutations_remain_guarded(self):
        for command in ["docker compose config -o /tmp/unused",
                        "docker compose config -''o/tmp/unused",
                        "docker compose config --output=/tmp/unused",
                        "docker compose config --lock-image-digests",
                        "docker restart app", "docker container exec app sh",
                        "docker container rm app", "docker image rm app:latest",
                        "docker network create app-net", "docker volume rm app-data",
                        "docker system prune", "docker compose down"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")

    def test_bounded_xargs_inspect_and_numeric_sed_reads(self):
        for command in [
            "docker ps -aq | xargs -r -n1 docker inspect --format '{{.Name}} => {{.HostConfig.RestartPolicy.Name}}'",
            "docker ps -aq | xargs -n 1 docker inspect app",
            "sed -n '590,660p' /opt/app/compose.yml", 'sed -n "1,40p" compose.yml',
        ]:
            with self.subTest(command=command):
                self.assertEqual(core.classify(command)[0], "R0")
        for command in ["xargs -I{} docker {} app", "xargs sh -c echo",
                        "xargs docker restart app", "xargs -a /tmp/commands docker inspect",
                        "sed -i '1,40p' compose.yml", "sed -n '1e touch /tmp/unused' compose.yml",
                        "sed -n '1,40p' -e '1e touch /tmp/unused' compose.yml",
                        "sed -n '1,40p' *", "awk 'BEGIN{system(\"id\")}'",
                        "docker inspect $(docker ps -aq)", "while read name; do docker inspect name; done"]:
            with self.subTest(command=command):
                self.assertNotEqual(core.classify(command)[0], "R0")
        self.assertEqual(core.check("sed -n '1,40p' /opt/app/.env")[0], 2)

    def test_kubectl_scope_uses_real_segments_and_comments(self):
        command = "echo 'a|b; # --context=wrong'; kubectl --context=test -n ops apply -f app.yml # --context=wrong"
        self.assertEqual(core.k8s_scope(command), {"context": "test", "namespace": "ops"})
        with self.assertRaises(core.OpsxError):
            core._validate_k8s_change_scope("kubectl apply -f app.yml # --context=test")

    def test_shared_agent_mcp_and_snapshot_guards(self):
        command = "docker inspect --format '{{.Name}} => {{.HostConfig.RestartPolicy.Name}}' app | grep -E 'app|worker'"
        self.assertEqual(core.check_snapshot_cmd(command)[0], 0)
        payload = json.loads(mcp_server.call_tool("ops_check", {"command": command})["content"][0]["text"])
        self.assertEqual(payload["level"], "R0")
        self.assertTrue(payload["allowed"])
        with mock.patch.object(core, "run_shell", return_value=subprocess.CompletedProcess(command, 0, "app => always", "")) as run:
            self.assertIn("app => always", tools._bash({"command": command}, tools.ToolContext()))
            run.assert_called_once()
        with mock.patch.object(tools.remote, "run", return_value=(0, "app => always")) as run:
            self.assertIn("app => always", tools._bash({"command": command, "target": "test"}, tools.ToolContext()))
            run.assert_called_once()

    def test_cli_checks_fixed_commands_without_contacting_docker(self):
        command = "docker inspect --format '{{.Name}} => {{.HostConfig.RestartPolicy.Name}}' app"
        env = dict(os.environ, OPSX_STATE_DIR=str(self.state))
        result = subprocess.run([sys.executable, str(core.REPO_DIR / "opsx"), "check", command],
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
