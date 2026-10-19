"""workflow 编排：JSON 定义的只读多步编排（后一步用 {prev} 引用前一步结论）。

位置：仓库 `workflows/*.json` + `~/.ops-agent/workflows/*.json`（同名后者覆盖）。
格式：

    {"description": "一句话说明",
     "steps": [{"name": "资源水位", "prompt": "只读检查……"},
               {"name": "汇总", "prompt": "基于前序结论……：{prev}"}]}

每步由 tools._run_isolated 起一个只读子代理；步骤顺序执行，最多 MAX_STEPS 步。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

MAX_STEPS = 8
MAX_FILE_BYTES = 1_000_000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DIRS = [core.REPO_DIR / "workflows", core.STATE_DIR / "workflows"]


class WorkflowError(Exception):
    pass


def _load_json(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkflowError(f"workflow 文件过大: {path}（上限 {MAX_FILE_BYTES} 字节）")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WorkflowError(f"workflow 文件损坏: {path}（{e}）") from e
    if not isinstance(data, dict):
        raise WorkflowError(f"workflow 文件格式异常: {path}")
    return data


def catalog() -> list[dict]:
    """按名字排序的 workflow 目录；用户目录同名覆盖仓库。"""
    found: dict[str, dict] = {}
    for d in DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            if not _NAME_RE.fullmatch(p.stem):
                continue
            try:
                data = _load_json(p)
            except WorkflowError:
                continue
            found[p.stem] = {"name": p.stem, "description": data.get("description") or ""}
    return [found[name] for name in sorted(found)]


def load(name: str) -> dict:
    if isinstance(name, str) and name.endswith(".json"):
        name = name[:-5]
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise WorkflowError(f"非法 workflow 名: {name!r}（只允许字母数字与 ._-）")
    for d in reversed(DIRS):  # 用户目录优先
        p = d / f"{name}.json"
        if not p.is_file():
            continue
        data = _load_json(p)
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise WorkflowError(f"workflow {name} 没有 steps")
        if len(steps) > MAX_STEPS:
            raise WorkflowError(f"workflow {name} 步骤过多（上限 {MAX_STEPS}）")
        normalized = []
        for i, step in enumerate(steps, 1):
            prompt = step.get("prompt") if isinstance(step, dict) else None
            if not isinstance(prompt, str) or not prompt.strip():
                raise WorkflowError(f"workflow {name} 第 {i} 步缺少 prompt")
            normalized.append({"name": str(step.get("name") or f"步骤 {i}"),
                               "prompt": prompt.strip()})
        return {"name": name, "description": data.get("description") or "",
                "steps": normalized}
    names = ", ".join(m["name"] for m in catalog()) or "（无）"
    raise WorkflowError(f"未找到 workflow: {name}（可用: {names}）")
