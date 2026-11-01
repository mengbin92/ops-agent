"""A2A AgentExecutor：把 A2A 的 task 生命周期映射到 ops-agent 的 agent loop。

三条映射关系（其余都是细节）：

1. **会话**：A2A `context_id` → ops-agent `Session(a2a-<context_id>)`。
   落在同一个 `~/.ops-agent/sessions/` 目录，所以 A2A 跑的每一轮都能 `/resume` 回看；
   会话 id 带 `a2a-` 前缀，避免与人手动开的 REPL 会话混淆。

2. **只读直通 / 写操作二次确认**：不新增任何放行逻辑。R0 只读命令由 bash 工具内部
   `core.check()` 直接放行；模型要变更时只能走 `ops_approve`，而该工具内部会调
   `ctx.ask_user()` 求确认——我们在这里把问题抛成 `NeedUserInput` 中断，任务转
   `TASK_STATE_INPUT_REQUIRED` 并带出整张变更单。调用方带同一个 `task_id` 回一条消息，
   框架会再次调用 `execute()`，此时 `current_task.state == INPUT_REQUIRED`，走续跑分支。

3. **同步内核 / 异步协议**：`loop.run_turn()` 是同步阻塞的，一律 `asyncio.to_thread` 执行，
   否则一次几十秒的排查就能把整个 event loop（连同其它任务）堵死。

安全边界（必须知道）：
- 一次用户答复只批准**一个**待确认项：`ask_user` 第一次返回答复，之后再调用就重新中断。
  否则模型在续跑轮里发起第二个变更会被同一个「确认」顺带批准。
- 取消只标记状态，不保证工具停下：Python 线程无法中断，已发出的命令可能仍在目标机上执行。
  因此取消消息明确写「请核实执行记录」，与项目既有的 unknown 语义保持一致。
- A2A 通道没有 REPL 那样的实时确认框，所以「无人值守语义」不适用于此形态：
  ask_user 永远可用（由调用方承担答复责任），这一点与 schedule/subagent 相反。
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402  与 tools.py 同一约定
from . import loop, session as sess_mod, tools  # noqa: E402
from .a2a_sdk import sdk  # noqa: E402

TIMEOUT_DEFAULT = 600
MAX_CONCURRENT_DEFAULT = 2
MAX_INPUT_CHARS = 20_000     # 入口上限：超长消息直接拒绝，不把整本日志塞进上下文
SEMAPHORE_WAIT = 30.0        # 等不到执行槽就明确报忙，而不是让调用方无限挂着
MAX_SESSIONS = 200           # 常驻会话上限：Session 会缓存全部 messages，不设限就是内存泄漏

# context_id 由调用方提供，会拼进会话文件名（Session 校验 [A-Za-z0-9_-]{1,128}）。
# 这里先按更严格的上界拒掉异常输入，避免拿它当路径或超长文件名。
_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,100}$")
_SESSION_PREFIX = "a2a-"

SETTLE_NOTE = ("[A2A] 该工具调用未执行：当时在等待人工确认，审批戳尚未创建。"
               "不要把它当成已执行的结果。")


class NeedUserInput(BaseException):
    """要求人工确认的中断信号。

    必须继承 BaseException：`tools._dispatch()` 用 `except Exception` 兜底所有工具异常，
    用普通异常会被它变成一条工具结果字符串，中断永远到不了协议层。
    代价是 `tools.dispatch()` 的脱敏也被跳过，所以捕获方必须自己 `core.redact`。
    """

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


def _log(msg: str) -> None:
    """服务端日志走 stderr：stdout 留给 uvicorn 的 access log。"""
    print(f"[a2a] {msg}", file=sys.stderr, flush=True)


def _settle_pending(sess: sess_mod.Session, note: str) -> int:
    """给「已发出但没拿到结果」的 tool_call 补一条说明。

    中断发生在 run_turn 的 `tools.dispatch()` 内部——那时 session 里已有 assistant 的
    tool_calls，但 tool 结果还没写。不补的话，下一轮 run_turn 开头的 pending 逻辑会把它
    标成「可能已执行」，与我们实际没执行的事实相反，模型会去做无谓核实甚至重放。
    """
    answered = {m.get("tool_call_id") for m in sess.messages if m.get("role") == "tool"}
    pending = [call
               for m in sess.messages if m.get("role") == "assistant"
               for call in (m.get("tool_calls") or [])
               if call.get("id") not in answered]
    for call in pending:
        sess.append({"role": "tool", "tool_call_id": call["id"],
                     "name": call["function"]["name"], "content": note})
    return len(pending)


def _resume_prompt(answer: str) -> str:
    """续跑时喂给模型的显式指令。

    模型上一轮停在「等确认」，需要明确告诉它：答复到了、当时没执行、现在该走哪一步。
    措辞里强调「重新 ops_approve」——上一轮的中断发生在建戳之前，直接调 ops_exec
    会被守门拒绝（审批戳不存在）。
    """
    return (
        f"[A2A 人工确认] 上一轮变更单的确认答复是：「{answer}」。\n"
        "（那次工具调用当时未执行，审批戳尚未创建。）\n"
        "如果这是批准：按原变更单重新调用 ops_approve（本次确认会自动通过），"
        "再用 ops_exec 执行，快照参数必须与变更单一致。\n"
        "如果不是批准：不要执行任何写操作，简要说明已取消即可。"
    )


def build_executor(model: str | None = None, timeout: int = TIMEOUT_DEFAULT,
                   max_concurrent: int = MAX_CONCURRENT_DEFAULT):
    """构造执行器。

    基类在函数内取：模块导入时不要求 a2a-sdk 已安装（内核保持零依赖）。
    """
    s = sdk()
    pb, ph = s.pb, s.ph

    class OpsAgentExecutor(s.AgentExecutor):
        def __init__(self, model=None, timeout=TIMEOUT_DEFAULT,
                     max_concurrent=MAX_CONCURRENT_DEFAULT):
            self.model = model
            self.timeout = timeout
            self._sem = asyncio.Semaphore(max_concurrent)
            self._sessions: "OrderedDict[str, sess_mod.Session]" = OrderedDict()
            self._locks: dict[str, threading.Lock] = {}
            self._guard = threading.Lock()

        # ---- 会话 ----------------------------------------------------
        def _session(self, context_id: str) -> sess_mod.Session:
            sid = (_SESSION_PREFIX + context_id)[:128]
            with self._guard:
                sess = self._sessions.get(sid)
                if sess is not None:
                    self._sessions.move_to_end(sid)  # LRU：活跃会话不会被优先淘汰
                    return sess
                sess = sess_mod.Session(session_id=sid)
                self._sessions[sid] = sess
                self._locks[sid] = threading.Lock()
                self._evict_locked()
                return sess

        def _evict_locked(self) -> None:
            """控制常驻会话数（调用方需已持有 self._guard）。

            只淘汰「当前没人持有锁」的会话：会话正文仍在磁盘，下次访问会重新加载，
            上下文不丢。持有锁的跳过——否则同一个 sid 会同时存在两把锁，
            串行写入的保证就失效了。全都在忙时本轮不淘汰，宁可短暂超限。
            """
            while len(self._sessions) > MAX_SESSIONS:
                for sid in list(self._sessions):
                    lock = self._locks.get(sid)
                    if lock is None:
                        self._sessions.pop(sid, None)
                        break
                    if lock.acquire(blocking=False):
                        try:
                            self._sessions.pop(sid, None)
                            self._locks.pop(sid, None)
                        finally:
                            lock.release()
                        break
                else:
                    return

        # ---- 同步执行体（在线程里跑）----------------------------------
        def _run(self, sess, prompt: str, resuming: bool, answer: str) -> str:
            answered = [False]

            def ask_user(question: str) -> str:
                if resuming and not answered[0]:
                    answered[0] = True  # 一次答复只批准一个待确认项
                    return answer
                raise NeedUserInput(question)

            ctx = tools.ToolContext(
                ask_user=ask_user, model=self.model, session_id=sess.id,
                deadline=time.monotonic() + self.timeout)

            def on_event(kind, payload):
                if kind == "tool" and isinstance(payload, dict):
                    _log(f"session={sess.id} tool={payload.get('name')} "
                         f"phase={payload.get('phase')}")

            return loop.run_turn(prompt, sess, ctx, model=self.model, on_event=on_event)

        def _locked_run(self, sess, prompt, resuming, answer) -> str:
            """同一 context 串行：并发 task 共用一个 Session 会把消息交错写坏。

            会话修补（settle）必须也在锁内：中断后锁已释放，若在锁外补写，
            同 context 的下一个任务可能同时读到「悬空 tool_call」并重复补写。
            """
            sid = sess.id
            with self._guard:
                lock = self._locks[sid]
            if not lock.acquire(timeout=self.timeout):
                raise TimeoutError(
                    f"该会话（{sid}）上一个任务仍在执行，排队超过 {self.timeout}s")
            try:
                if resuming:
                    # 第 1 轮中断时已 settle；这里兜一次「进程重启后从磁盘恢复」的情况。
                    settled = _settle_pending(sess, SETTLE_NOTE)
                    if settled:
                        _log(f"session={sid} settled {settled} dangling tool_call(s)")
                try:
                    return self._run(sess, prompt, resuming, answer)
                except NeedUserInput:
                    # 中断点留下的悬空调用在锁内就地标记为「未执行」，再交还控制权。
                    _settle_pending(sess, SETTLE_NOTE)
                    raise
            finally:
                lock.release()

        # ---- 协议入口 -------------------------------------------------
        async def _reject(self, event_queue, task_id, context_id, reason) -> None:
            """拒绝也要先入队 Task：协议不允许以状态事件开场。"""
            await event_queue.enqueue_event(ph.new_task(
                task_id=task_id, context_id=context_id,
                state=pb.TASK_STATE_SUBMITTED))
            updater = s.TaskUpdater(event_queue, task_id, context_id)
            await updater.reject(ph.new_text_message(reason, task_id=task_id,
                                                     context_id=context_id))

        async def execute(self, context, event_queue) -> None:
            task_id = context.task_id or ""
            context_id = context.context_id or ""
            updater = s.TaskUpdater(event_queue, task_id, context_id)

            if not _CONTEXT_RE.match(context_id) or not task_id:
                # task_id 缺失时生成一次性 id：固定占位符会让多次非法请求撞同一个 task。
                await self._reject(event_queue, task_id or f"invalid-{uuid.uuid4().hex[:12]}",
                                   context_id or "invalid-context",
                                   "context_id/task_id 不合法：只允许字母数字与 _-，最长 101 字符。")
                return

            text = (context.get_user_input() or "").strip()
            if not text:
                await self._reject(event_queue, task_id, context_id,
                                   "空消息：请在 message.parts 里提供一个 text part。")
                return
            if len(text) > MAX_INPUT_CHARS:
                # 入口收口：超长粘贴（整本日志/转储）应先由调用方切片或用 logscan，
                # 而不是整段塞进上下文把预算和轮次一次烧光。
                await self._reject(
                    event_queue, task_id, context_id,
                    f"消息过长：{len(text)} 字符，上限 {MAX_INPUT_CHARS}。"
                    "请先裁剪、分段，或让 agent 用 logscan 做确定性粗筛。")
                return

            task = context.current_task
            terminal = (pb.TASK_STATE_COMPLETED, pb.TASK_STATE_FAILED,
                        pb.TASK_STATE_CANCELED, pb.TASK_STATE_REJECTED)
            if task is not None and task.status.state in terminal:
                # 纵深防御：SDK 的 DefaultRequestHandler 通常已经在进 executor 之前
                # 抛 UnsupportedOperationError（Task is in terminal state），这里是
                # 万一将来校验放宽时的最后一道——绝不能把终态 task 重新 start_work
                # 激活成一次新执行。用即时 Message 回应（A2A immediate-response 形态），
                # 不去动旧 task。
                await event_queue.enqueue_event(ph.new_text_message(
                    f"该 task 已处于终态（{pb.TaskState.Name(task.status.state)}），不能复用。"
                    "请不带 task_id 重新发起；要查旧结果请用 GetTask。",
                    task_id=task_id, context_id=context_id))
                return

            resuming = (task is not None
                        and task.status.state == pb.TASK_STATE_INPUT_REQUIRED)
            _log(f"task={task_id} context={context_id} "
                 f"{'resume' if resuming else 'new'} chars={len(text)}")

            if task is None:
                # 协议要求：状态/产物事件之前必须已有 Task 入队。
                await event_queue.enqueue_event(ph.new_task(
                    task_id=task_id, context_id=context_id,
                    state=pb.TASK_STATE_SUBMITTED,
                    history=[context.message] if context.message is not None else None))
            await updater.start_work()
            prompt = _resume_prompt(text) if resuming else text

            try:
                sess = self._session(context_id)
            except Exception as e:  # noqa: BLE001 —— 会话文件损坏等，落成 FAILED 而非悬挂
                _log(f"task={task_id} session error: {e}")
                await updater.failed(ph.new_text_message(
                    core.redact(f"会话初始化失败：{e}"), task_id=task_id, context_id=context_id))
                return

            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=SEMAPHORE_WAIT)
            except asyncio.TimeoutError:
                # 背压：宁可明确报忙，也不让调用方在 WORKING 上无限等待。
                await updater.failed(ph.new_text_message(
                    f"服务繁忙：{SEMAPHORE_WAIT:.0f}s 内未获得执行槽，请稍后重试或降低并发。",
                    task_id=task_id, context_id=context_id))
                return

            try:
                out = await asyncio.to_thread(
                    self._locked_run, sess, prompt, resuming, text)
            except NeedUserInput as e:
                question = core.redact(str(e.question or "需要人工确认"))
                _log(f"task={task_id} -> INPUT_REQUIRED")
                await updater.update_status(
                    pb.TASK_STATE_INPUT_REQUIRED,
                    ph.new_text_message(question, task_id=task_id, context_id=context_id))
                return
            except Exception as e:  # noqa: BLE001 —— 未预期异常必须落 FAILED，不能挂住
                _log(f"task={task_id} crashed: {type(e).__name__}: {e}")
                await updater.failed(ph.new_text_message(
                    core.redact(f"执行异常：{type(e).__name__}: {e}"),
                    task_id=task_id, context_id=context_id))
                return
            finally:
                self._sem.release()

            body = core.redact(out or "")
            timed_out = loop.DEADLINE_MARK in body
            await updater.add_artifact([ph.new_text_part(body)], name="reply")
            if timed_out:
                # 超时是「没做完」，不能报 completed——否则调用方会当成功处理。
                await updater.failed(ph.new_text_message(
                    body[:2000], task_id=task_id, context_id=context_id))
            else:
                await updater.complete(ph.new_text_message(
                    body[:2000], task_id=task_id, context_id=context_id))
            _log(f"task={task_id} -> {'FAILED(timeout)' if timed_out else 'COMPLETED'}")

        async def cancel(self, context, event_queue) -> None:
            task_id = context.task_id or ""
            context_id = context.context_id or ""
            updater = s.TaskUpdater(event_queue, task_id, context_id)
            _log(f"task={task_id} cancel requested")
            await updater.update_status(
                pb.TASK_STATE_CANCELED,
                ph.new_text_message(
                    "已标记为取消。注意：已在执行的命令无法从运行中的线程中断，"
                    "可能仍会在目标机上生效——请用执行记录核实实际状态。",
                    task_id=task_id, context_id=context_id))

    return OpsAgentExecutor(model=model, timeout=timeout, max_concurrent=max_concurrent)
