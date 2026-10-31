"""A2A 服务端：AgentCard 发布 + JSON-RPC 路由 + 本地优先的鉴权。

暴露面就是攻击面：A2A 一旦监听就不是「本机 REPL」，而是网络入口。
因此默认只绑回环，且**非回环地址必须配 token**（fail closed）——没有 token 的公开
ops-agent 等于把审批闸门拆掉扔到网上。

AgentCard（`/.well-known/agent-card.json`）不强制鉴权：它只是能力声明，不含主机名、
凭据或路径；真正的 RPC 端点必须带 Bearer token。这是 A2A 部署的常见口径，也让
标准客户端能先发现能力、再带凭据调用。
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402,F401  与 tools.py 同一约定
from .a2a_sdk import A2AUnavailable, sdk  # noqa: E402
from . import skills as skills_mod  # noqa: E402

TOKEN_ENV = "OPSAGENT_A2A_TOKEN"
HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 8080
CARD_PATH = "/.well-known/agent-card.json"
SKILL_LIMIT = 20

AGENT_NAME = "ops-agent"
AGENT_DESC = ("只读优先的运维智能体：本机/SSH/K8s/Docker 排查、确定性巡检、"
              "受控变更（写操作必须由调用方二次确认）。")


class StartupRefused(RuntimeError):
    """配置不安全或依赖缺失，拒绝启动。"""


def repo_version() -> str:
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def base_url(host: str, port: int) -> str:
    """IPv6 字面量必须加方括号，否则 AgentCard 里的 URL 客户端无法解析。"""
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def is_loopback(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_token(explicit: str | None = None) -> str:
    """token 只从参数或环境变量来。

    优先环境变量：命令行里的 `--token xxx` 会出现在 `ps` 输出与 shell 历史里。
    """
    return (explicit or os.environ.get(TOKEN_ENV) or "").strip()


def require_token(host: str, token: str) -> None:
    """非回环监听强制要求 token；不满足就拒绝启动，而不是打印一句警告继续跑。"""
    if is_loopback(host):
        return
    if not token:
        raise StartupRefused(
            f"拒绝在 {host} 上无鉴权启动：A2A 是网络入口，写操作审批不能裸奔。\n"
            f"请设置环境变量 {TOKEN_ENV}（或 --token），例如：\n"
            f"  export {TOKEN_ENV}=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')\n"
            f"或只监听本机：--host {HOST_DEFAULT}")


def build_agent_card(base_url: str, streaming: bool = False):
    """构造 AgentCard。skills 从 skills/ 目录生成，与实际可加载的 playbook 对齐。"""
    s = sdk()
    pb = s.pb
    skills = []
    for item in skills_mod.catalog()[:SKILL_LIMIT]:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        skills.append(pb.AgentSkill(
            id=name[:64],
            name=name[:64],
            description=(str(item.get("description") or "").strip() or name)[:512],
            tags=["ops"],
        ))
    modes = ["text/plain"]
    return pb.AgentCard(
        name=AGENT_NAME,
        description=AGENT_DESC,
        version=repo_version(),
        supported_interfaces=[pb.AgentInterface(
            url=base_url.rstrip("/") + "/",
            protocol_binding=s.TransportProtocol.JSONRPC.value,
            protocol_version=s.PROTOCOL_VERSION_1_0,
        )],
        default_input_modes=modes,
        default_output_modes=modes,
        capabilities=pb.AgentCapabilities(streaming=streaming),
        skills=skills,
    )


def _bearer_from_scope(scope) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == b"authorization":
            raw = value.decode("latin-1").strip()
            if raw[:7].lower() == "bearer ":
                return raw[7:].strip()
            return ""
    return ""


class BearerAuthMiddleware:
    """纯 ASGI 中间件（不引 starlette 的 BaseHTTPMiddleware，少一层抽象）。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        if (scope.get("path") or "").startswith(CARD_PATH):
            return await self.app(scope, receive, send)
        supplied = _bearer_from_scope(scope)
        # 定长比较，避免按字节早退泄露 token 前缀。
        if not supplied or not hmac.compare_digest(supplied, self.token):
            body = json.dumps({"error": "unauthorized",
                               "hint": f"需要 Authorization: Bearer <{TOKEN_ENV}>"},
                              ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


def create_app(executor, card, token: str = ""):
    """组装 ASGI app。token 非空时启用 Bearer 校验。"""
    s = sdk()
    handler = s.DefaultRequestHandler(
        agent_executor=executor, task_store=s.InMemoryTaskStore(), agent_card=card)
    routes = (s.create_agent_card_routes(card)
              + s.create_jsonrpc_routes(handler, rpc_url="/"))
    app = s.Starlette(routes=routes)
    return BearerAuthMiddleware(app, token) if token else app


def serve(host: str = HOST_DEFAULT, port: int = PORT_DEFAULT, token: str | None = None,
          model: str | None = None, timeout: int = 600, max_concurrent: int = 2) -> int:
    """启动服务。返回进程退出码（0 正常，2 配置不安全/依赖缺失）。"""
    from .a2a_executor import build_executor

    tok = resolve_token(token)
    try:
        require_token(host, tok)
    except StartupRefused as e:
        print(f"[a2a] {e}", file=sys.stderr)
        return 2
    if not tok:
        print(f"[a2a] 提示：未设置 {TOKEN_ENV}，仅本机（{host}）可访问，"
              "其它本机进程仍可调用——需要隔离时请配 token。", file=sys.stderr)

    try:
        executor = build_executor(model=model, timeout=timeout,
                                  max_concurrent=max_concurrent)
        url = base_url(host, port)
        card = build_agent_card(url)
        app = create_app(executor, card, tok)
        import uvicorn
    except A2AUnavailable as e:
        print(f"[a2a] {e}", file=sys.stderr)
        return 2
    except ImportError as e:
        # a2a-sdk 的 http-server extra 不含 uvicorn，只有 requirements-a2a.txt 才带。
        print(f"[a2a] 缺少 ASGI 服务器依赖（uvicorn）：{e}\n"
              "  安装：python3 -m pip install -r requirements-a2a.txt", file=sys.stderr)
        return 2

    print(f"[a2a] AgentCard: {url}{CARD_PATH}", file=sys.stderr)
    print(f"[a2a] JSON-RPC : {url}/  (auth={'bearer' if tok else 'none, loopback only'})",
          file=sys.stderr)
    print(f"[a2a] 模型={model or os.environ.get('OPSAGENT_MODEL') or '默认'} "
          f"超时={timeout}s 并发={max_concurrent}", file=sys.stderr)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except OSError as e:
        # 端口占用/权限不足：给一句人话，不要把 uvicorn 的栈甩给用户
        print(f"[a2a] 启动失败（{host}:{port}）：{e}\n"
              "  端口被占用请换 --port，低端口需要特权。", file=sys.stderr)
        return 2
    return 0


def card_json(host: str = HOST_DEFAULT, port: int = PORT_DEFAULT) -> str:
    """打印 AgentCard JSON（不起服务，便于对接方先看能力声明）。"""
    from google.protobuf.json_format import MessageToDict
    card = build_agent_card(base_url(host, port))
    return json.dumps(MessageToDict(card), ensure_ascii=False, indent=2)
