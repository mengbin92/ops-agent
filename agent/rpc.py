"""RPC 前端：stdio JSON-RPC（每行一个 JSON），供 Web UI / IM 机器人嵌入。

方法：
  prompt  {text, stream?}
                        -> {"text": 最终回复, "events": [...]}；ask_user 经
                           {"method":"ask_user","id":"srv-..."} 反向请求客户端回答。
                           stream=true 时边跑边推通知 {"method":"event",
                           "params":{"kind":"delta"|"tool"|"stream_end"|...,"payload":...}}，
                           结果里不再重复 events
  reset   {}            -> 开新会话
  ping    {}            -> {}

线程模型：stdin 由独立读线程消费（客户端响应经 resolve() 回填），主线程处理
业务——否则 ask_user 反向请求会死锁（读循环阻塞在等待回答上，无法读取回答）。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import uuid

from . import loop, session as sess_mod, tools

ASK_USER_TIMEOUT = 600


class RpcPeer:
    def __init__(self):
        self._pending: dict[str, str | None] = {}
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()

    def send(self, obj: dict) -> None:
        with self._send_lock:  # 读线程（错误响应）与主线程（正常响应）都会写 stdout
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def request(self, method: str, params: dict) -> str | None:
        """反向请求客户端并阻塞等回答（响应由读线程 resolve() 回填）。"""
        rid = "srv-" + uuid.uuid4().hex[:8]
        with self._cond:
            self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            answered = self._cond.wait_for(lambda: rid in self._pending,
                                           timeout=ASK_USER_TIMEOUT)
            if answered:
                return self._pending.pop(rid)
            return None  # 超时：视为客户端未回答

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
    inbox: queue.Queue = queue.Queue()

    def reader() -> None:
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
            if not peer.resolve(msg):  # 反向请求的响应不入业务队列
                inbox.put(msg)
        inbox.put(None)  # EOF

    threading.Thread(target=reader, daemon=True).start()

    def ask_user(question: str) -> str:
        answer = peer.request("ask_user", {"question": question})
        return answer if answer is not None else "（客户端未回答，视为拒绝）"

    while True:
        msg = inbox.get()
        if msg is None:
            return
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
                params = msg.get("params") or {}
                stream = bool(params.get("stream"))
                events: list = []

                def push(kind, payload) -> None:
                    if stream:
                        peer.send({"jsonrpc": "2.0", "method": "event",
                                   "params": {"kind": kind, "payload": payload}})
                    else:
                        events.append({"kind": kind, "payload": payload})

                ctx = tools.ToolContext(ask_user=ask_user, model=model)
                text = loop.run_turn(params.get("text", ""), session, ctx, on_event=push,
                                     on_delta=(lambda delta: push("delta", delta)) if stream else None,
                                     model=model)
                result = {"text": text, "session_id": session.id}
                if not stream:
                    result["events"] = events
            else:
                raise ValueError(f"未知方法: {method}")
            peer.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except Exception as e:  # noqa: BLE001 —— 单条消息异常不崩循环
            peer.send({"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": -32603, "message": str(e)}})
