"""REPL 前端：交互式终端。

命令：/new 新会话 · /resume 恢复历史会话 · /copy [all] 复制回复 · /quit 退出
流式输出：SSE 增量到达时整段重绘（Markdown 实时渲染）。
"""
from __future__ import annotations

import datetime
import math
import os
import shutil
import subprocess
import sys

from . import goal as goal_mod
from . import loop, markdown, session as sess_mod, tools

# ---- 语义配色：按信息类型/重要程度区分，NO_COLOR 或非 TTY 自动禁色 ----
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

C_RED = "\033[31m"
C_YELLOW = "\033[33m"
C_GREEN = "\033[32m"
C_CYAN = "\033[36m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{C_RESET}" if _USE_COLOR else text


def info(msg: str) -> None:
    """系统提示（次要信息）：暗色。"""
    print(_c(C_DIM, msg))


def ok(msg: str) -> None:
    """成功确认：绿色。"""
    print(_c(C_GREEN, msg))


def warn(msg: str) -> None:
    """警告/降级/中断：黄色。"""
    print(_c(C_YELLOW, msg))


def error(msg: str) -> None:
    """错误/拒绝：红色。"""
    print(_c(C_RED, msg), file=sys.stderr)


def result_color(result: str) -> str:
    """工具结果按内容嗅探定色：拒绝/错误=红，成功=绿，降级=黄，其余=暗。"""
    head = result[:300]
    if any(k in head for k in ("[opsx 拒绝]", "[错误]", "[超时]", "[失败]", "[opsx 错误]")):
        return C_RED
    if any(k in head for k in ("审批戳已创建", "执行成功", "已恢复文件", "[完成]")):
        return C_GREEN
    if any(k in head for k in ("不可用", "降级", "视为用户拒绝")):
        return C_YELLOW
    return C_DIM

try:
    import readline  # noqa: F401 —— 导入即给 input() 加上行编辑与方向键历史
except ImportError:  # Windows 无 readline
    readline = None  # type: ignore[assignment]

HISTORY_FILE = tools.core.STATE_DIR / "repl_history"
HISTORY_LIMIT = 1000


def _load_history() -> None:
    if readline and HISTORY_FILE.exists():
        try:
            readline.read_history_file(str(HISTORY_FILE))
        except OSError:
            pass


def _save_history() -> None:
    if not readline:
        return
    try:
        readline.set_history_length(HISTORY_LIMIT)
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(HISTORY_FILE))
    except OSError:
        pass


def _ask_style(question: str) -> tuple[str, str]:
    """确认框样式：R3/不可逆 → 红粗警示；其余需确认 → 黄色醒目。"""
    danger = any(k in question for k in ("R3", "不可逆", "无回滚"))
    if danger:
        return C_RED + C_BOLD, "⚠ 不可逆操作确认"
    return C_YELLOW, "agent 请求确认"


def _is_libedit() -> bool:
    return readline is not None and "libedit" in (getattr(readline, "__doc__", None) or "")


def make_prompt(text: str, color: str = C_CYAN + C_BOLD) -> str:
    """input() 提示符。

    GNU readline：\\001/\\002 包裹 ANSI 序列，告诉它这些字符不占显示宽度，
    否则 ↑/↓ 历史重绘时会按错误宽度擦除、留下残影。
    libedit（macOS 自带）：不支持忽略标记 → 退化为无色提示符，根治残影。
    """
    if not _USE_COLOR or _is_libedit():
        return text
    return f"\001{color}\002{text}\001{C_RESET}\002"


def _terminal_ask(question: str) -> str:
    border, title = _ask_style(question)
    line = "─" * 40
    print()
    print(_c(border, f"╭─ {title} {line}"))
    for ln in question.splitlines():
        print(_c(border, "│ ") + ln)
    print(_c(border, f"╰{line}{'─' * len(title)}"))
    try:
        return input(make_prompt("你的回答> ", border)).strip()
    except (EOFError, KeyboardInterrupt):
        return "（用户中断，视为拒绝）"


def copy_to_clipboard(text: str) -> str:
    """复制到系统剪贴板：macOS pbcopy，Linux wl-copy/xclip。返回结果说明。"""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True,
                               capture_output=True, timeout=5)
                return f"已复制到剪贴板（{cmd[0]}，{len(text)} 字符）"
            except (subprocess.SubprocessError, OSError) as e:
                return f"[复制失败] {cmd[0]}: {e}"
    return "[复制失败] 未找到 pbcopy / wl-copy / xclip，请手动复制"


class StreamPrinter:
    """流式增量渲染：每次更新清除已打印区域并重绘完整文本。

    内容超出屏幕可重绘行数后，光标最多只能回到屏幕顶（块起点已滚出屏幕），
    继续整段重绘会把同一内容反复打印——此时停止重绘，只追加增量。
    """

    def __init__(self):
        self._buf = ""
        self._printed_rows = 0
        self._overflow = False

    @staticmethod
    def _max_rows() -> int:
        """可重绘的行数上限：留两行余量，避免顶到屏幕底。"""
        return max(4, shutil.get_terminal_size().lines - 2)

    @staticmethod
    def _rows_of(text: str) -> int:
        """估算终端占用行数（含自动换行；ANSI 不计宽度）。"""
        width = max(shutil.get_terminal_size().columns, 20)
        rows = 0
        for line in text.split("\n"):
            w = markdown._width(line)
            rows += max(1, math.ceil(w / width)) if w else 1
        return rows

    def _repaint(self) -> None:
        rendered = markdown.render(self._buf)
        if self._printed_rows:
            # 光标上移已打印行数并清到屏幕尾，再重绘
            sys.stdout.write(f"\033[{self._printed_rows}A\033[J")
        sys.stdout.write(rendered + "\n")
        sys.stdout.flush()
        self._printed_rows = self._rows_of(rendered)

    def feed(self, delta: str) -> None:
        self._buf += delta
        if self._overflow:
            sys.stdout.write(delta)
            sys.stdout.flush()
            return
        if self._rows_of(markdown.render(self._buf)) > self._max_rows():
            # 超出屏幕：旧块保持不动，之后只追加增量（尾部为未渲染原文）
            self._overflow = True
            sys.stdout.write(delta)
            sys.stdout.flush()
            return
        self._repaint()

    def finish(self) -> str:
        if self._overflow:
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            self._repaint()
        text, self._buf, self._printed_rows, self._overflow = self._buf, "", 0, False
        return text


class ReplState:
    def __init__(self, model: str | None):
        self.model = model
        self.session = sess_mod.Session()
        self.last_reply = ""  # 原始 markdown，供 /copy 使用
        self.printer = StreamPrinter()

    def emit(self, kind: str, payload) -> None:
        if kind == "text":
            self.last_reply = payload
            print(f"\n{markdown.render(payload)}\n")
        elif kind == "stream_end":
            self.last_reply = payload
            self.printer.finish()
        elif kind == "tool":
            if payload["phase"] == "call":
                args_preview = str(payload["args"])[:200]
                print(_c(C_CYAN, f"  ▸ 调用 {payload['name']}({args_preview})"))
            else:
                preview = payload["result"].replace("\n", " ")[:200]
                print(_c(result_color(payload["result"]),
                         f"  ◂ {payload['name']}: {preview}"))


def _continue_goal(state: "ReplState", ctx: tools.ToolContext) -> None:
    """一轮结束后按目标状态自动续跑，直到完成/受阻/轮次用尽。"""
    while True:
        try:
            msg = goal_mod.continuation(state.session.id)
        except goal_mod.GoalError as e:
            warn(f"[目标] {e}")
            return
        if not msg:
            return
        info("↻ 目标续跑中…")
        state.printer = StreamPrinter()
        loop.run_turn(msg, state.session, ctx, on_event=state.emit,
                      model=state.model, on_delta=state.printer.feed)


def _pick_session() -> sess_mod.Session | None:
    """列出历史会话并让用户选择恢复。"""
    sessions = sess_mod.list_sessions()
    if not sessions:
        info("(无历史会话)")
        return None
    for i, s in enumerate(sessions[:20], 1):
        ts = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%m-%d %H:%M")
        print(f"  {_c(C_CYAN, str(i).rjust(2) + '.')} [{ts}] {s['id']}（{s['turns']} 轮）{s['preview']}")
    try:
        choice = input(make_prompt("恢复哪个会话（编号，回车取消）> ", C_CYAN)).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice.isdigit() or not (1 <= int(choice) <= len(sessions[:20])):
        return None
    return sess_mod.Session(sessions[int(choice) - 1]["id"])


def main(model: str | None = None) -> int:
    state = ReplState(model)
    ctx = tools.ToolContext(ask_user=_terminal_ask, model=model,
                            session_id=state.session.id, goal_continuation=True)
    _load_history()
    info(f"ops-agent REPL（会话 {state.session.id}，模型 {model or '默认'}）。")
    info("/new 新会话 · /resume 恢复会话 · /goal 查看/暂停/恢复目标 · "
         "/copy [all] 复制回复 · /quit 退出 · ↑/↓ 翻历史")
    prompt = make_prompt("ops> ")
    while True:
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _save_history()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            _save_history()
            return 0
        if line == "/new":
            state.session = sess_mod.Session()
            ctx.session_id = state.session.id
            state.last_reply = ""
            ok(f"已开启新会话 {state.session.id}")
            continue
        if line == "/resume":
            picked = _pick_session()
            if picked:
                state.session = picked
                ctx.session_id = picked.id
                state.last_reply = ""
                ok(f"已恢复会话 {picked.id}（{len(picked.messages)} 条消息）")
            continue
        if line == "/goal" or line.startswith("/goal "):
            try:
                sub = line[len("/goal"):].strip()
                if sub in ("stop", "pause"):
                    goal_mod.update(state.session.id, "pause")
                    ok("目标已暂停（/goal resume 可恢复）")
                elif sub == "resume":
                    goal_mod.update(state.session.id, "resume")
                    ok("目标已恢复（续跑预算已重置）")
                else:
                    info(goal_mod.format_one(goal_mod.get(state.session.id)))
            except goal_mod.GoalError as e:
                warn(f"[目标] {e}")
            continue

        if line.startswith("/copy"):
            if line.strip() == "/copy all":
                text = "\n\n".join(m["content"] for m in state.session.messages
                                   if m.get("role") == "assistant" and m.get("content"))
            else:
                text = state.last_reply
            if text:
                result = copy_to_clipboard(text)
                (ok if result.startswith("已复制") else error)(result)
            else:
                info("(暂无可复制的回复)")
            continue
        state.printer = StreamPrinter()
        try:
            loop.run_turn(line, state.session, ctx, on_event=state.emit,
                          model=state.model, on_delta=state.printer.feed)
            _continue_goal(state, ctx)
        except loop.llm.LLMError as e:
            error(f"[LLM 错误] {e}")
        except KeyboardInterrupt:
            warn("\n[已打断当前轮次]")
