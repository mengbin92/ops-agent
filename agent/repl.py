"""REPL 前端：交互式终端。/new 新会话，/quit 退出，其余输入交给 agent loop。"""
from __future__ import annotations

import sys

from . import loop, session as sess_mod, tools


def _terminal_ask(question: str) -> str:
    print("\n╭─ agent 请求确认 ─────────────────────────")
    for line in question.splitlines():
        print(f"│ {line}")
    print("╰──────────────────────────────────────────")
    try:
        return input("你的回答> ").strip()
    except (EOFError, KeyboardInterrupt):
        return "（用户中断，视为拒绝）"


def _make_emit():
    def emit(kind: str, payload):
        if kind == "text":
            print(f"\n{payload}\n")
        elif kind == "tool":
            if payload["phase"] == "call":
                args_preview = str(payload["args"])[:200]
                print(f"  ▸ 调用 {payload['name']}({args_preview})")
            else:
                preview = payload["result"].replace("\n", " ")[:200]
                print(f"  ◂ {payload['name']}: {preview}")
    return emit


def main(model: str | None = None) -> int:
    session = sess_mod.Session()
    ctx = tools.ToolContext(ask_user=_terminal_ask)
    emit = _make_emit()
    print(f"ops-agent REPL（会话 {session.id}，模型 {model or '默认'}）。/new 新会话，/quit 退出。")
    while True:
        try:
            line = input("ops> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            return 0
        if line == "/new":
            session = sess_mod.Session()
            print(f"已开启新会话 {session.id}")
            continue
        try:
            loop.run_turn(line, session, ctx, on_event=emit, model=model)
        except loop.llm.LLMError as e:
            print(f"[LLM 错误] {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("[已打断当前轮次]")
