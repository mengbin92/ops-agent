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
_WAITING = object()


def _invalid_json_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


class RpcPeer:
    def __init__(self):
        self._pending: dict[str, object] = {}
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._closed = False

    def send(self, obj: dict) -> None:
        with self._send_lock:  # 读线程（错误响应）与主线程（正常响应）都会写 stdout
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def request(self, method: str, params: dict) -> str | None:
        """反向请求客户端并阻塞等回答（响应由读线程 resolve() 回填）。"""
        rid = "srv-" + uuid.uuid4().hex[:8]
        with self._cond:
            if self._closed:
                return None
            self._pending[rid] = _WAITING
            try:
                self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
                answered = self._cond.wait_for(
                    lambda: self._closed or self._pending[rid] is not _WAITING,
                    timeout=ASK_USER_TIMEOUT)
                answer = self._pending[rid]
                return answer if answered and isinstance(answer, str) else None
            finally:
                self._pending.pop(rid, None)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def resolve(self, msg: dict) -> bool:
        """若 msg 是对反向请求的响应则回填并返回 True。"""
        rid = msg.get("id")
        with self._cond:
            if isinstance(rid, str) and self._pending.get(rid) is _WAITING \
                    and "method" not in msg and msg.get("jsonrpc") == "2.0" \
                    and ("result" in msg or "error" in msg):
                result = msg.get("result")
                self._pending[rid] = result if "error" not in msg and isinstance(result, str) else None
                self._cond.notify_all()
                return True
        return False


def serve(model: str | None = None) -> None:
    peer = RpcPeer()
    session = sess_mod.Session()
    inbox: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line, parse_constant=_invalid_json_constant)
                except ValueError:
                    peer.send({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "Parse error"}})
                    continue
                valid = (isinstance(msg, dict) and msg.get("jsonrpc") == "2.0"
                         and type(msg.get("id")) in (str, int, float, type(None)))
                if not valid or ("method" not in msg and "result" not in msg and "error" not in msg) \
                        or ("method" in msg and not isinstance(msg["method"], str)):
                    error_id = msg.get("id") if isinstance(msg, dict) \
                        and type(msg.get("id")) in (str, int, float, type(None)) else None
                    peer.send({"jsonrpc": "2.0", "id": error_id,
                               "error": {"code": -32600, "message": "Invalid Request"}})
                    continue
                if "method" not in msg:
                    peer.resolve(msg)  # 只接收在途反向请求的响应，其余响应忽略。
                    continue
                inbox.put(msg)
        finally:
            peer.close()  # EOF/读失败时立即拒绝未完成的确认，避免等待十分钟。
            inbox.put(None)

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
            if method not in {"ping", "reset", "prompt"}:
                peer.send({"jsonrpc": "2.0", "id": msg_id,
                           "error": {"code": -32601, "message": f"未知方法: {method}"}})
                continue
            params = msg.get("params", {})
            if not isinstance(params, dict) or (method == "prompt" and (
                    not isinstance(params.get("text"), str) or not params["text"].strip()
                    or ("stream" in params and not isinstance(params["stream"], bool)))):
                peer.send({"jsonrpc": "2.0", "id": msg_id,
                           "error": {"code": -32602, "message": "Invalid params"}})
                continue
            if method == "ping":
                result = {}
            elif method == "reset":
                session = sess_mod.Session()
                result = {"session_id": session.id}
            elif method == "prompt":
                stream = params.get("stream", False)
                events: list = []

                def push(kind, payload) -> None:
                    if stream:
                        peer.send({"jsonrpc": "2.0", "method": "event",
                                   "params": {"kind": kind, "payload": payload}})
                    else:
                        events.append({"kind": kind, "payload": payload})

                ctx = tools.ToolContext(ask_user=ask_user, model=model,
                                        session_id=session.id)
                text = loop.run_turn(params.get("text", ""), session, ctx, on_event=push,
                                     on_delta=(lambda delta: push("delta", delta)) if stream else None,
                                     model=model)
                result = {"text": text, "session_id": session.id}
                if not stream:
                    result["events"] = events
            peer.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except Exception as e:  # noqa: BLE001 —— 单条消息异常不崩循环
            peer.send({"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": -32603, "message": str(e)}})
