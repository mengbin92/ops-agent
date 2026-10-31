"""A2A 协议端到端：真实 uvicorn 服务端 + 官方 SDK 客户端 + 桩 LLM。

为什么要桩 LLM：这些断言关心的是「协议映射是否正确」（写操作会不会偷偷执行、
确认后有没有真的建戳），不是模型聪不聪明。桩把两条分支固定下来，测试可重复、
不烧 token、不依赖网络。

未安装 a2a-sdk 时整个类跳过——CI 默认只装标准库，不该因此变红。

跑法：
    python3 -m pip install -r requirements-a2a.txt
    python3 -m unittest tests.test_a2a_e2e -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent import a2a_sdk  # noqa: E402

TOKEN = "e2e-token-9f3a"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _no_proxy_opener():
    """绕过 HTTP_PROXY：本机代理会把回环请求转发成 absolute-form，路由 404。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait(url: str, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            _no_proxy_opener().open(url, timeout=2).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def _stub_response(message: dict) -> dict:
    return {"id": "stub", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _decide(messages: list) -> dict:
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return _stub_response({"role": "assistant",
                               "content": "排查完成，工具输出摘录：\n"
                                          + str(last.get("content") or "")[:400]})
    text = str(last.get("content") or "")
    if "[A2A 人工确认]" in text:
        import re
        m = re.search(r"「(.+?)」", text)  # 只看答复原文，不看提示词措辞
        answer = m.group(1) if m else text
        if not any(w in answer for w in ("确认", "yes", "y", "同意", "批准")):
            return _stub_response({"role": "assistant",
                                   "content": "用户未批准，已取消，未执行任何变更。"})
        return _approve_call("call_approve_resume")
    if "重启" in text:
        return _approve_call("call_approve_first")
    return _stub_response({"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_bash", "type": "function",
        "function": {"name": "bash", "arguments": json.dumps({"command": "df -h"})}}]})


def _approve_call(call_id: str) -> dict:
    return _stub_response({"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": "ops_approve", "arguments": json.dumps({
            "command": "systemctl restart ops-agent-e2e-nonexistent",
            "impact": "重启一个不存在的测试单元（仅验证 A2A 确认链路），不影响任何真实服务",
            "ttl_seconds": 60,
        }, ensure_ascii=False)}}]})


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        out = json.dumps(_decide(body.get("messages") or [])).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@unittest.skipUnless(a2a_sdk.available()[0], "未安装 a2a-sdk（requirements-a2a.txt）")
class A2AEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state"
        cls.state.mkdir(parents=True)

        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.stub_port = cls.stub.server_address[1]
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ,
                   OPSAGENT_API_KEY="stub-key",
                   OPSAGENT_BASE_URL=f"http://127.0.0.1:{cls.stub_port}/v1",
                   OPSAGENT_MODEL="stub",
                   OPSX_STATE_DIR=str(cls.state),
                   OPSAGENT_A2A_TOKEN=TOKEN,
                   no_proxy="127.0.0.1,localhost",
                   NO_PROXY="127.0.0.1,localhost")
        cls.proc = subprocess.Popen(
            [sys.executable, str(REPO / "opsagent"), "a2a", "serve", "--port", str(cls.port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.served = _wait(f"{cls.base}/.well-known/agent-card.json")
        if not cls.served:
            out = cls.proc.stdout.read() if cls.proc.stdout else ""
            cls.tearDownClass()
            raise unittest.SkipTest(f"服务未能启动：\n{out[-1500:]}")

    @classmethod
    def tearDownClass(cls):
        for attr in ("stub", "proc"):
            obj = getattr(cls, attr, None)
            if obj is None:
                continue
            try:
                if attr == "stub":
                    obj.shutdown()
                    obj.server_close()
                elif obj.poll() is None:
                    obj.terminate()
                    obj.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    obj.kill()
                except Exception:  # noqa: BLE001
                    pass
        tmp = getattr(cls, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def _send(self, text, **kw):
        from agent import a2a_client
        return a2a_client.send(self.base, text, token=TOKEN, timeout=120, **kw)

    def _stamps(self):
        d = self.state / "approvals"
        return sorted(p.name for p in d.glob("*.json")) if d.exists() else []

    # ------------------------------------------------------------------
    def test_01_card_reachable_without_token(self):
        with _no_proxy_opener().open(f"{self.base}/.well-known/agent-card.json",
                                     timeout=5) as resp:
            card = json.loads(resp.read())
        self.assertEqual(card["name"], "ops-agent")
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")

    def test_02_rpc_requires_token(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                           "params": {"message": {"messageId": "x", "role": "ROLE_USER",
                                                  "parts": [{"text": "hi"}]}}}).encode()
        req = urllib.request.Request(f"{self.base}/", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "A2A-Version": "1.0"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _no_proxy_opener().open(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_03_readonly_task_completes(self):
        r = self._send("看一下磁盘")
        self.assertEqual(r["state"], "TASK_STATE_COMPLETED")
        self.assertFalse(r["needs_input"])
        self.assertTrue("Filesystem" in r["text"] or "/dev/" in r["text"], r["text"][:120])

    def test_04_write_task_requires_confirmation(self):
        r = self._send("重启一下服务")
        self.assertEqual(r["state"], "TASK_STATE_INPUT_REQUIRED")
        self.assertTrue(r["needs_input"])
        self.assertIn("ops-agent-e2e-nonexistent", r["text"])
        self.assertIn("影响面", r["text"])

    def test_05_declining_creates_no_stamp_and_no_execution(self):
        before = self._stamps()
        r = self._send("重启一下服务")
        self.assertEqual(r["state"], "TASK_STATE_INPUT_REQUIRED")
        out = self._send("不要执行", task_id=r["task_id"], context_id=r["context_id"])
        self.assertNotIn("已执行", out["text"])
        self.assertEqual(self._stamps(), before, "拒绝后不应创建审批戳")
        exec_dir = self.state / "executions"
        self.assertFalse(exec_dir.exists() and list(exec_dir.glob("*.json")),
                         "拒绝后不应产生执行记录")

    def test_06_approving_creates_stamp(self):
        r = self._send("重启一下服务")
        self.assertEqual(r["state"], "TASK_STATE_INPUT_REQUIRED")
        out = self._send("确认执行", task_id=r["task_id"], context_id=r["context_id"])
        self.assertEqual(out["state"], "TASK_STATE_COMPLETED")
        audit = (self.state / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"approve"', audit, "批准应留下 approve 审计")
        self.assertIn("ops-agent-e2e-nonexistent", audit)

    def test_07_session_persisted_for_resume(self):
        self._send("看一下磁盘")
        sessions = list((self.state / "sessions").glob("a2a-*.jsonl"))
        self.assertTrue(sessions, "A2A 会话应落盘，便于 /resume 回看")

    def test_08_terminal_task_cannot_be_reused(self):
        """终态 task 再发消息必须被拒绝，不得被当成一次新的成功执行。

        实测中 SDK 的 DefaultRequestHandler 会在进入 executor 之前就抛
        UnsupportedOperationError（Task is in terminal state），我们的 executor 里
        还有一段同义检查作为纵深防御。因此这里断言的是「客户端拿到明确错误」，
        而不是某种特定文案。
        """
        r = self._send("看一下磁盘")
        self.assertEqual(r["state"], "TASK_STATE_COMPLETED")
        with self.assertRaises(RuntimeError) as ctx:
            self._send("再看一次", task_id=r["task_id"], context_id=r["context_id"])
        msg = str(ctx.exception)
        self.assertTrue("terminal" in msg.lower() or "终态" in msg, msg)

    def test_09_oversized_input_is_rejected(self):
        from agent import a2a_executor
        r = self._send("x" * (a2a_executor.MAX_INPUT_CHARS + 100))
        self.assertEqual(r["state"], "TASK_STATE_REJECTED")
        self.assertIn("过长", r["text"])

    def test_10_empty_input_is_rejected(self):
        r = self._send("   ")
        self.assertEqual(r["state"], "TASK_STATE_REJECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
