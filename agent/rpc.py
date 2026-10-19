"""RPC 前端：stdio JSON-RPC（每行一个 JSON），供 Web UI / IM 机器人嵌入。

方法：
  prompt  {text}        -> {"text": 最终回复, "events": [...]}；ask_user 经
                           {"method":"ask_user","id":...} 反向请求客户端回答
  reset   {}            -> 开新会话
  ping    {}            -> {}
"""
from __future__ import annotations

import json
import sys
import threading
import uuid

from . import loop, session as sess_mod, tools


class RpcPeer:
    def __init__(self):
        self._pending: dict[str, str] = {}
        self._cond = threading.Condition()

    def send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def request(self, method: str, params: dict) -> str | None:
        """反向请求客户端并阻塞等回答（通知/响应由 read 循环回填）。"""
        rid = "srv-" + uuid.uuid4().hex[:8]
        with self._cond:
            self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            self._cond.wait_for(lambda: rid in self._pending, timeout=600)
            return self._pending.pop(rid, None)

    def resolve(self, msg: dict) -> bool:
        """若 msg 是对反向请求的响应则回填并返回 True。"""
        rid = msg.get("id")
        if isinstance(rid, str) and rid.startswith("srv-"):
            result = msg.get("result")
            with self._cond:
                self._pending[rid] = result if isinstance(result, str) \
                    else json.dumps(result, ensure_ascii=False)
                self._cond.notify_all()
            return True
        return False


def serve(model: str | None = None) -> None:
    peer = RpcPeer()
    session = sess_mod.Session()

    def ask_user(question: str) -> str:
        answer = peer.request("ask_user", {"question": question})
        return answer if answer is not None else "（客户端未回答，视为拒绝）"

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            peer.send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32700, "message": "Parse error"}})
            continue
        if peer.resolve(msg):
            continue
        msg_id = msg.get("id")
        method = msg.get("method")
        if "id" not in msg:
            continue  # 通知忽略
        try:
            if method == "ping":
                result = {}
            elif method == "reset":
                session = sess_mod.Session()
                result = {"session_id": session.id}
            elif method == "prompt":
                events: list = []
                ctx = tools.ToolContext(ask_user=ask_user)
                text = loop.run_turn(msg.get("params", {}).get("text", ""), session, ctx,
                                     on_event=lambda k, p: events.append({"kind": k, "payload": p}),
                                     model=model)
                result = {"text": text, "events": events, "session_id": session.id}
            else:
                raise ValueError(f"未知方法: {method}")
            peer.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except Exception as e:  # noqa: BLE001 —— 单条消息异常不崩循环
            peer.send({"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": -32603, "message": str(e)}})
