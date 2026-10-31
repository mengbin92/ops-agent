"""`opsagent a2a` 子命令：serve / card / call / doctor。

CLI 层只做参数解析与呈现，业务在 a2a_server / a2a_client 里。
所有对 SDK 的调用都包了 A2AUnavailable，缺依赖时打印安装命令并退出 2，
不把 ImportError 堆栈抛给用户。
"""
from __future__ import annotations

import argparse
import sys

from .a2a_sdk import A2AUnavailable, available

EXIT_UNAVAILABLE = 2
EXIT_FAIL = 1  # 本地常量：异常分支不能引用仅在 call 分支里 import 的 a2a_client


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opsagent a2a",
        description="A2A 协议形态（Agent-to-Agent，官方 a2a-sdk 1.x / 协议 1.0）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="以 A2A 服务端运行（发布 AgentCard + JSON-RPC）")
    s.add_argument("--host", default="127.0.0.1",
                   help="监听地址（默认 127.0.0.1；非回环必须配 token）")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--token", default=None,
                   help=f"Bearer token（建议改用环境变量 OPSAGENT_A2A_TOKEN）")
    s.add_argument("--model", default=None, help="覆盖默认模型")
    s.add_argument("--timeout", type=int, default=600, help="单任务墙钟预算秒（默认 600）")
    s.add_argument("--max-concurrent", type=int, default=2,
                   help="同时执行的 A2A 任务上限（默认 2）")

    c = sub.add_parser("card", help="打印 AgentCard（JSON）")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8080)

    a = sub.add_parser("call", help="调用一个 A2A agent")
    a.add_argument("url", help="对端地址，如 http://127.0.0.1:8080")
    a.add_argument("text", help="发给对端的文本")
    a.add_argument("--token", default=None, help="对端要求的 Bearer token")
    a.add_argument("--task-id", default=None, help="续跑已有 task（用于确认变更单）")
    a.add_argument("--context-id", default=None, help="指定会话 context")
    a.add_argument("--timeout", type=int, default=300)
    a.add_argument("--proxy", default=None, help="显式走代理（默认忽略 HTTP_PROXY）")

    sub.add_parser("doctor", help="检查 A2A 依赖与运行环境")
    return p


def _doctor() -> int:
    ok, reason = available()
    print("A2A 依赖检查")
    print(f"  Python: {sys.version.split()[0]}（要求 >= 3.10）")
    if not ok:
        print("  状态: ✗ 不可用")
        print(reason)
        return EXIT_UNAVAILABLE
    import importlib.metadata as md
    for pkg in ("a2a-sdk", "starlette", "uvicorn", "httpx", "protobuf"):
        try:
            print(f"  {pkg}: {md.version(pkg)}")
        except md.PackageNotFoundError:
            print(f"  {pkg}: 未安装")
    print("  状态: ✓ 可用")
    print("\n提示：")
    print("  - 非回环监听必须配 OPSAGENT_A2A_TOKEN，否则拒绝启动")
    print("  - 客户端默认忽略 HTTP_PROXY；需要代理时用 --proxy")
    return 0


def cli(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "doctor":
            return _doctor()

        if args.cmd == "card":
            from . import a2a_server
            print(a2a_server.card_json(host=args.host, port=args.port))
            return 0

        if args.cmd == "serve":
            from . import a2a_server
            if args.port <= 0 or args.port > 65535:
                print("[a2a] 错误: --port 必须在 1-65535", file=sys.stderr)
                return EXIT_UNAVAILABLE
            if args.timeout <= 0:
                print("[a2a] 错误: --timeout 必须为正数", file=sys.stderr)
                return EXIT_UNAVAILABLE
            if args.max_concurrent <= 0:
                print("[a2a] 错误: --max-concurrent 必须为正数", file=sys.stderr)
                return EXIT_UNAVAILABLE
            return a2a_server.serve(host=args.host, port=args.port, token=args.token,
                                    model=args.model, timeout=args.timeout,
                                    max_concurrent=args.max_concurrent)

        # call
        from . import a2a_client
        result = a2a_client.send(args.url, args.text, task_id=args.task_id,
                                 context_id=args.context_id, token=args.token,
                                 timeout=args.timeout, proxy=args.proxy)
        print(a2a_client.format_result(result))
        if not result["needs_input"]:
            return a2a_client.EXIT_OK if "COMPLETED" in result["state"] else a2a_client.EXIT_FAIL
        # 需要人工确认：TTY 下就地续跑，非 TTY 交给调用方脚本决定（带 --task-id 重入）
        if not sys.stdin.isatty():
            print("\n[需要确认] 非交互环境不会自动批准。"
                  f"核对上面的变更单后重跑：\n"
                  f"  opsagent a2a call {args.url} '<答复>' --task-id {result['task_id']}",
                  file=sys.stderr)
            return a2a_client.EXIT_NEEDS_INPUT
        try:
            answer = input("\n答复（批准请输入「确认执行」或 yes，其它回答均视为拒绝）> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消（未发送答复）。", file=sys.stderr)
            return a2a_client.EXIT_NEEDS_INPUT
        if not answer:
            print("空答复，未发送。", file=sys.stderr)
            return a2a_client.EXIT_NEEDS_INPUT
        final = a2a_client.send(args.url, answer, task_id=result["task_id"],
                                context_id=result["context_id"], token=args.token,
                                timeout=args.timeout, proxy=args.proxy)
        print(a2a_client.format_result(final))
        return a2a_client.EXIT_OK if "COMPLETED" in final["state"] else a2a_client.EXIT_FAIL
    except A2AUnavailable as e:
        print(f"[a2a] {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except RuntimeError as e:
        print(f"[a2a] {e}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as e:
        print(f"[a2a] 错误: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE
