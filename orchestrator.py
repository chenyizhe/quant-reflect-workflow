#!/usr/bin/env python3
"""
orchestrator.py — builtin 运行时（WorkBuddy 内置模型驱动）

设计：Python 不直接调 LLM。它只负责把 DAG 拆成「节点任务书」，由编排层
（对话内的子代理）按任务书执行并把产物写回磁盘，脚本再回收产物、推进下一个节点。

这样模型额度走平台内置通道，不消耗自有 API key，同时保留完整的 DAG 校验、
变量绑定与运行记录。

命令:
    python orchestrator.py init -t "策略需求"      # 建 run，生成节点 1 任务书
    python orchestrator.py status <run_dir>        # 查看进度
    python orchestrator.py step <run_dir> <node>   # 回收该节点产物，生成下一节点任务书
    python orchestrator.py final <run_dir>         # 输出最终结果
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from workflow_engine import ValidationError, Workflow, WorkflowError, ast_hash

TASK_DIR = "tasks"
PROFILE_RE = re.compile(r"模型档位: \*\*(.+?)\*\*（来源: (.+?)，")

C_END, C_OK, C_BAD, C_WARN = "\033[0m", "\033[32m", "\033[31m", "\033[33m"

# Best-of-N 正交范式：多样性来自「实现路线不同」，不是「同一 prompt 抖 N 次」。
# 同 prompt 重复采样在低温度下差异主要是命名与注释，收益远小于换范式。
ORTHOGONAL = {
    1: ("范式 A · 向量化优先",
        "能用 pandas/numpy 原生运算解决的，一律不写 Python 循环；追求执行效率与代码简洁。"),
    # v1.9：n 降到 2 后，把原「B 防御」与「C 分层解耦」合并成一条，
    # 保证两条候选仍然覆盖「重校验」与「重结构」两种不同取舍
    2: ("范式 B · 防御与分层解耦",
        "优先写输入校验与边界处理（非法参数、缺失值、空表、停牌、异常极值），"
        "并把配置、纯计算、数据适配拆成独立函数便于单测。"
        "宁可多写 30 行校验，也不让脏数据静默流过去。"),
    3: ("范式 C · 分层解耦",
        "拆成配置对象 + 纯计算函数 + 数据适配层，便于单测与替换。不追求最短，追求可验证。"),
    4: ("范式 D · 极简",
        "只实现核心逻辑，代码尽量短，注释克制。牺牲扩展性换可读性，但不牺牲正确性。"),
}


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _ensure_dir(path: Path) -> None:
    """沙箱对已存在目录的 mkdir(exist_ok=True) 会抛 EEXIST，先判存在。"""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)


def pad(text: str, w: int) -> str:
    text = str(text)
    return text + " " * max(0, w - width(text))


# --------------------------------------------------------------------------
def _ctx_path(run_dir: Path) -> Path:
    return run_dir / "variables.json"


def _load_ctx(run_dir: Path) -> dict:
    p = _ctx_path(run_dir)
    if not p.exists():
        raise WorkflowError(f"run 目录不完整，缺少 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_ctx(run_dir: Path, ctx: dict) -> None:
    _ctx_path(run_dir).write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 自检状态（轮次 / 验证结果）
# --------------------------------------------------------------------------
def _state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def _load_state(run_dir: Path) -> dict:
    p = _state_path(run_dir)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"rounds": {}, "verify": {}}


def _save_state(run_dir: Path, state: dict) -> None:
    _state_path(run_dir).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _artifact_path(wf: Workflow, run_dir: Path, node) -> tuple[Path, str]:
    var = node.outputs[0]
    rel = wf.artifacts.get(var, {}).get("path", f"{var}.txt")
    return run_dir / rel, var


def _archive_artifact(wf: Workflow, run_dir: Path, node, round_idx: int) -> str | None:
    """把当前产物归档到 history/。幂等：目标已存在则跳过，返回归档路径。"""
    src, var = _artifact_path(wf, run_dir, node)
    if not src.exists():
        return None
    hist = run_dir / "history"
    _ensure_dir(hist)
    dst = hist / f"{node.id}.{var}.r{round_idx}{src.suffix}"
    if dst.exists():
        return str(dst)
    shutil.copy2(src, dst)
    return str(dst)


def _run_verify(wf: Workflow, run_dir: Path, node, round_idx: int) -> dict:
    """执行节点自检命令。返回 {passed, exit_code, cmd, log_file, tail, elapsed_s}。"""
    cfg = node.verify or {}
    art_path, _ = _artifact_path(wf, run_dir, node)
    cmd = str(cfg.get("command", "{python} {artifact}")).format(
        python=sys.executable,
        artifact=str(art_path),
        run_dir=str(run_dir),
        node=node.id,
        workflow_dir=str(wf.path.parent),
    )
    timeout = int(cfg.get("timeout", 300))

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(run_dir), capture_output=True, text=True, timeout=timeout
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        code, passed = proc.returncode, proc.returncode == 0
    except subprocess.TimeoutExpired as exc:
        out = f"[verify 超时 {timeout}s]\n{exc.stdout or ''}{exc.stderr or ''}"
        code, passed = -9, False
    except Exception as exc:  # noqa: BLE001
        out, code, passed = f"[verify 执行失败] {exc}", -1, False

    vdir = run_dir / "verify"
    _ensure_dir(vdir)
    log_file = vdir / f"{node.id}.r{round_idx}.log"
    log_file.write_text(f"$ {cmd}\n\n{out}", encoding="utf-8")

    tail_chars = int(cfg.get("log_tail_chars", 4000))
    return {
        "passed": passed,
        "exit_code": code,
        "cmd": cmd,
        "log_file": str(log_file.relative_to(run_dir)),
        "tail": out[-tail_chars:] if len(out) > tail_chars else out,
        "elapsed_s": round(time.time() - t0, 2),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def _write_repair_task(wf: Workflow, run_dir: Path, node, ctx: dict,
                       result: dict, round_no: int) -> Path:
    """生成返修任务书：原任务 + 上一轮产物路径 + 自检失败日志 + 修复要求。"""
    _run_precheck(wf, run_dir, node, ctx)
    profile, prof_src = wf.resolve_profile(node)
    system = wf.render(node.system_prompt, ctx, f"{node.id}.system")
    user = wf.render(node.user_prompt, ctx, f"{node.id}.user")
    art_path, var = _artifact_path(wf, run_dir, node)
    repair_prompt = str((node.verify or {}).get("repair_prompt") or "").strip()
    lines = art_path.read_text(encoding="utf-8").count("\n") + 1 if art_path.exists() else 0

    md = f"""# 节点任务书 · {node.name} ({node.id}) · 返修第 {round_no} 轮

- run: `{run_dir.name}`
- 模型档位: **{profile}**（来源: {prof_src}，调用子代理时必须传 model="{profile}"）
- 触发原因: 自检未通过（退出码 {result['exit_code']}，命令 `{result['cmd']}`）

## 系统指令

{system}

## 用户输入（原始，未变）

{user}

## 上一轮产物

`{art_path}` — 共 {lines} 行，已归档到 `history/`。
**先读这个文件**，在它的基础上定点修复，不要凭记忆重写。

## 自检失败日志（末 {len(result['tail'])} 字符）

```
{result['tail']}
```

## 修复要求

{repair_prompt or '只修复日志中的失败项，保持已通过部分不变。'}

- 写回同一个文件 `{art_path}`，保持完整内容，不允许省略或占位
- 不要删除或弱化已有的自检用例来“让它通过”
- 不要执行代码，不要跑测试，脚本会自己跑自检
- 完成后一句话说明改了什么，不要回贴全文
"""
    tdir = run_dir / TASK_DIR
    _ensure_dir(tdir)
    idx = wf.order.index(node.id) + 1
    path = tdir / f"{idx:02d}_{node.id}.repair{round_no}.md"
    path.write_text(md, encoding="utf-8")
    return path


def _pending_nodes(wf: Workflow, ctx: dict, state: dict | None = None) -> list[str]:
    """未完成 = 产物缺失，或配了 verify 但尚未通过。"""
    state = state or {}
    verified = state.get("verify") or {}
    pending: list[str] = []
    for nid in wf.order:
        node = next(n for n in wf.nodes if n.id == nid)
        if not all(v in ctx for v in node.outputs):
            pending.append(nid)
            continue
        if node.verify and not (verified.get(nid) or {}).get("passed", False):
            pending.append(nid)
    return pending


def _write_task(wf: Workflow, run_dir: Path, node_id: str, ctx: dict,
                candidate: tuple[int, int, str] | None = None) -> Path:
    """
    candidate=(序号, 总数, diversity) 时生成 Best-of-N 候选任务书：
    产物写到 candidates/<node>.c<i><ext>，prompt 追加该候选的实现范式。
    """
    node = next(n for n in wf.nodes if n.id == node_id)
    _run_precheck(wf, run_dir, node, ctx)
    profile, prof_src = wf.resolve_profile(node)
    upstream = [u for u in wf.order if u in wf._deps[node.id]]  # noqa: SLF001

    system = wf.render(node.system_prompt, ctx, f"{node.id}.system")
    user = wf.render(node.user_prompt, ctx, f"{node.id}.user")

    art_path, var = _artifact_path(wf, run_dir, node)
    banner = ""
    out_lines = []

    if candidate:
        idx, total, diversity = candidate
        suffix = art_path.suffix or ".txt"
        cand_path = run_dir / "candidates" / f"{node.id}.c{idx}{suffix}"
        if diversity == "orthogonal":
            title, guidance = ORTHOGONAL[(idx - 1) % len(ORTHOGONAL) + 1]
            banner = (f"\n## 本条候选的实现范式（第 {idx}/{total} 条）\n\n"
                      f"**{title}**：{guidance}\n\n"
                      f"严格按这个范式写，不要写成通用版；其余候选会覆盖别的范式。\n")
        else:
            banner = (f"\n## 本条候选（第 {idx}/{total} 条）\n\n"
                      f"与其他候选使用完全相同的指令，独立生成一份，不必考虑差异化。\n")
        out_lines.append(f"- `{var}` → 写入 `{cand_path}`")
    else:
        for v in node.outputs:
            art = wf.artifacts.get(v, {})
            rel = art.get("path", f"{v}.txt")
            out_lines.append(f"- `{v}` → 写入 `{run_dir / rel}`")

    md = f"""# 节点任务书 · {node.name} ({node.id}){' · 候选 ' + str(candidate[0]) if candidate else ''}

- run: `{run_dir.name}`
- 模型档位: **{profile}**（来源: {prof_src}，调用子代理时必须传 model="{profile}"）
- 上游节点: {', '.join(upstream) or '无（直接消费全局输入）'}
{banner}
## 系统指令

{system}

## 用户输入

{user}

## 交付要求

{chr(10).join(out_lines)}

- 只写上面列出的产物文件，不创建其他文件，不修改已有文件
- 产物必须是可直接使用的完整内容，不允许 `...` 或“省略”占位
- 不要执行代码，不要跑测试
- 完成后返回一句话说明，不要回贴全文
"""
    tdir = run_dir / TASK_DIR
    _ensure_dir(tdir)
    idx = wf.order.index(node_id) + 1
    name = f"{idx:02d}_{node.id}.c{candidate[0]}.md" if candidate else f"{idx:02d}_{node.id}.md"
    path = tdir / name
    path.write_text(md, encoding="utf-8")
    return path


def _run_precheck(wf: Workflow, run_dir: Path, node, ctx: dict) -> None:
    """
    渲染前对上游产物跑一次 check_code，把真实运行时结果注入上下文。
    v1.7 的教训：critic 没有运行时信息，审了 9 条也没发现 pandas 3.0 的 read-only。
    """
    cfg = node.precheck
    if not cfg:
        return
    src_var = str(cfg.get("input"))
    dst_var = str(cfg.get("inject_as"))
    level = str(cfg.get("level", "runtime"))

    # 缓存键带上被测代码的 AST 哈希：初稿返修后必须重跑，否则 critic 看到的是过时结论
    cache_key = f"__precheck_src_hash__{dst_var}"
    cur_hash = ast_hash(str(ctx.get(src_var, "")))
    if dst_var in ctx and str(ctx.get(dst_var, "")).strip() and ctx.get(cache_key) == cur_hash:
        return

    src = str(ctx.get(src_var, "")).strip()
    if not src:
        ctx[dst_var] = f"（前置检查跳过：上游变量 {src_var} 尚未产出）"
        return

    suffix = ".py" if "def " in src or "import " in src else ".txt"
    tmp = run_dir / f"_precheck_{src_var}{suffix}"
    tmp.write_text(src, encoding="utf-8")
    tool = Path(__file__).parent / "tools" / "check_code.py"
    cmd = [sys.executable, str(tool), str(tmp), "--level", level]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        body = (proc.stdout + proc.stderr).strip()
        verdict = "通过" if proc.returncode == 0 else "未通过"
        ctx[cache_key] = cur_hash
        ctx[dst_var] = (f"命令: check_code --level {level}\n"
                        f"退出码: {proc.returncode}（{verdict}）\n\n{body[-4000:]}")
    except subprocess.TimeoutExpired:
        ctx[dst_var] = f"命令超时（>300s）"
    finally:
        tmp.unlink(missing_ok=True)


def _write_tasks(wf: Workflow, run_dir: Path, node, ctx: dict) -> list[Path]:
    """有 best_of_n 就铺 N 份候选任务书，否则单份。"""
    bon = node.best_of_n
    if not bon:
        return [_write_task(wf, run_dir, node.id, ctx)]
    count = int(bon.get("n", 1))
    diversity = str(bon.get("diversity", "orthogonal"))
    return [
        _write_task(wf, run_dir, node.id, ctx, candidate=(i, count, diversity))
        for i in range(1, count + 1)
    ]


def _print_binding(wf: Workflow, report: dict) -> None:
    bi = report.get("builtin") or {}
    print(f"工作流: {report['workflow']} v{report['version']}  runtime={report.get('runtime', 'api')}")
    if report.get("runtime") == "builtin":
        profiles, uniform = wf.profile_summary(report)
        flag = "✓ 一致" if uniform else "✗ 不一致"
        print(f"内置档位: {bi.get('profile')}  lock={bi.get('lock')}  "
              f"生效档位={sorted(profiles)}  {flag}")
    print(f"执行链: {' → '.join(report['order'])}")
    for b in report["bindings"]:
        reads = ", ".join(b["reads"]) or "—"
        col = b["profile"] if report.get("runtime") == "builtin" else b["model"]
        print(f"  {b['node']:<10} [{col:<12}] 读: {reads:<45} 写: {', '.join(b['writes'])}")
    print()


# --------------------------------------------------------------------------
def cmd_init(wf: Workflow, task: str, base: Path) -> int:
    report = wf.validate(require_inputs=True, provided={"user_task": task})
    _print_binding(wf, report)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base / run_id
    _ensure_dir(run_dir)

    ctx = {"user_task": task}
    _save_ctx(run_dir, ctx)
    (run_dir / "inputs.json").write_text(
        json.dumps({"user_task": task}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    first = _pending_nodes(wf, ctx)[0]
    first_node = next(n for n in wf.nodes if n.id == first)
    tasks = _write_tasks(wf, run_dir, first_node, ctx)
    print(f"run 目录: {run_dir}")
    if len(tasks) > 1:
        print(f"节点 {first} 启用 Best-of-N：{len(tasks)} 份候选任务书")
        for t in tasks:
            print(f"  {t}")
        print(f"\n全部候选写完后选优：")
        print(f"  python orchestrator.py pick {run_dir} {first}")
    else:
        print(f"第一个节点任务书: {tasks[0]}")
        print(f"\n下一步: 按任务书执行 {first}，产物落盘后运行")
        print(f"  python orchestrator.py step {run_dir} {first}")
    return 0


def _record_trace(wf: Workflow, run_dir: Path, node_id: str, ctx: dict) -> None:
    """把节点实际生效的档位写进 trace.json，供 audit 复核。"""
    node = next(n for n in wf.nodes if n.id == node_id)
    profile, src = wf.resolve_profile(node)

    tp = run_dir / "trace.json"
    data: dict = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}
    data.update({
        "workflow": wf.name,
        "version": wf.version,
        "run_id": run_dir.name,
        "runtime": wf.runtime,
        "builtin": {"profile": wf.builtin_profile, "lock": wf.builtin_lock},
    })

    nodes = {n.get("node_id"): n for n in data.get("nodes", []) if isinstance(n, dict)}
    prev = nodes.get(node_id, {})
    nodes[node_id] = {
        "node_id": node_id,
        "name": node.name,
        "mode": "builtin",
        "profile": profile,
        "profile_source": src,
        "model": prev.get("model"),          # api 模式遗留字段，保留可追溯
        "artifacts": {v: wf.artifacts.get(v, {}).get("path", f"{v}.txt") for v in node.outputs},
        "chars": {v: len(str(ctx.get(v, ""))) for v in node.outputs},
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ok",
    }
    data["nodes"] = [nodes[k] for k in wf.order if k in nodes]
    tp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_json_block(text: str) -> dict | None:
    """取 ```json 到最后一个 ``` 之间的内容；容忍字段内嵌代码围栏。"""
    m = re.search(r"```json\s*", text or "")
    if m:
        start = m.end()
        end = (text or "").rfind("```")
        raw = text[start:end] if end > start else text[start:]
    else:
        raw = (text or "").strip()
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _try_critic_short_circuit(wf: Workflow, run_dir: Path, ctx: dict,
                              state: dict, node) -> bool:
    """
    critic 判定 pass 且没有阻断/高危级问题时，跳过 refiner。
    初稿本身就是终稿时，refiner 的 800 行重写纯属浪费——这是 v1.9 省时的主要来源之一。
    """
    if node.id != "critic":
        return False
    payload = _extract_json_block(str(ctx.get("critique_report", "")))
    if not payload:
        return False
    if str(payload.get("verdict", "")).strip().lower() != "pass":
        return False

    fixes = payload.get("actionable_fixes") or []
    severe = [f for f in fixes
              if isinstance(f, dict) and str(f.get("severity", "")).strip() in ("阻断", "高危")]
    if severe:
        return False

    gen = next((n for n in wf.nodes if "draft_code" in n.outputs), None)
    ref = next((n for n in wf.nodes if "final_code" in n.outputs), None)
    if not gen or not ref:
        return False

    src, _ = _artifact_path(wf, run_dir, gen)
    dst, _ = _artifact_path(wf, run_dir, ref)
    if not src.exists():
        return False

    shutil.copy2(src, dst)
    ctx["final_code"] = str(ctx.get("draft_code", ""))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.setdefault("verify", {})[ref.id] = {
        "passed": True, "exit_code": 0, "cmd": "short_circuit_clean_critic",
        "log_file": "", "elapsed_s": 0.0, "at": now,
    }
    state.setdefault("short_circuit", {})[ref.id] = {
        "reason": f"critic verdict=pass，且 {len(fixes)} 条 fix 中无阻断/高危",
        "at": now,
    }
    _save_ctx(run_dir, ctx)
    return True


def cmd_step(wf: Workflow, run_dir: Path, node_id: str) -> int:
    ctx = _load_ctx(run_dir)
    state = _load_state(run_dir)
    node = next((n for n in wf.nodes if n.id == node_id), None)
    if node is None:
        raise WorkflowError(f"未知节点: {node_id}")

    round_no = int(state["rounds"].get(node_id, 0))
    prev_verify = state["verify"].get(node_id) or {}
    art_path, _ = _artifact_path(wf, run_dir, node)

    # 需要回收：ctx 缺变量，或上一轮自检未通过（产物已被返修覆盖）
    need_recollect = (not all(v in ctx for v in node.outputs)) or not prev_verify.get("passed", True)

    if need_recollect:
        missing = []
        for var in node.outputs:
            art = wf.artifacts.get(var, {})
            p = run_dir / art.get("path", f"{var}.txt")
            if not p.exists():
                missing.append(str(p))
                continue
            ctx[var] = p.read_text(encoding="utf-8").strip()
            print(f"  ✓ 回收 {var} ← {p.name} ({len(ctx[var]):,} 字符)")
        if missing:
            raise WorkflowError("产物缺失，无法推进:\n  " + "\n  ".join(missing))
        _save_ctx(run_dir, ctx)
    else:
        print(f"{node_id} 的产物已回收，跳过。")

    _record_trace(wf, run_dir, node_id, ctx)
    profile, src = wf.resolve_profile(node)
    print(f"  档位: {profile} (来源 {src})")

    # ---------- AST 早停：返修震荡检测 ----------
    if node.outputs and node.verify:
        cur_hash = ast_hash(str(ctx.get(node.outputs[0], "")))
        prev_hash = (state.get("ast_hash") or {}).get(node_id)
        if prev_hash and cur_hash == prev_hash and prev_verify and not prev_verify.get("passed", True):
            print(f"  {C_BAD}AST 结构未变（{cur_hash[:8]}），返修无进展 → 早停{C_END}")
            print(f"  说明: 上轮自检未通过，本轮代码结构与上轮一致（已忽略注释/空行/格式差异）")
            print(f"  建议: 换修复策略（扩大重写范围）或人工介入，继续跑只会重复烧 token")
            return 1
        state.setdefault("ast_hash", {})[node_id] = cur_hash
        _save_state(run_dir, state)

    # ---------- 自检 + 失败自动返修 ----------
    if node.verify:
        vround = round_no + 1
        # 被验证的版本必须留档；幂等，重复 step 不会刷出重复文件
        archived = _archive_artifact(wf, run_dir, node, round_no)
        if archived:
            print(f"  归档第 {round_no} 轮产物 → history/{Path(archived).name}")

        cached = state.get("verify", {}).get(node_id) or {}
        reuse = (
            cached.get("cmd") == "cached_from_pick_gate"
            and cached.get("passed") is True
            and round_no == 0
            and state.get("ast_hash", {}).get(node_id) == ast_hash(str(ctx.get(node.outputs[0], "")))
        )
        if reuse:
            result = {"passed": True, "exit_code": 0, "elapsed_s": 0.0,
                      "cmd": cached.get("cmd"), "log_file": cached.get("log_file", ""),
                      "tail": "", "reused": True}
            print(f"  {C_OK}✓ 复用 pick 阶段探针结果{C_END}（未重复执行 check_code）")
        else:
            result = _run_verify(wf, run_dir, node, vround)
        # state.json 只留摘要，完整日志在 verify/*.log，避免状态文件被日志撑大
        state["verify"][node_id] = {k: v for k, v in result.items() if k != "tail"}
        max_rounds = int(node.verify.get("max_rounds", 0))

        if result["passed"]:
            print(f"  {C_OK}✓ 自检通过{C_END}（{result['elapsed_s']}s，退出码 0，"
                  f"共 {round_no} 次返修）")
            if _try_critic_short_circuit(wf, run_dir, ctx, state, node):
                ref_id = next(n.id for n in wf.nodes if "final_code" in n.outputs)
                print(f"  {C_OK}⚡ critic 零阻断 → 跳过 refiner{C_END}，"
                      f"draft_code 直接落为 final_code（{ref_id} 标记为短路完成）")
            _save_state(run_dir, state)
        else:
            print(f"  {C_BAD}✗ 自检失败{C_END}（退出码 {result['exit_code']}，"
                  f"{result['elapsed_s']}s）→ verify/{Path(result['log_file']).name}")
            if round_no >= max_rounds:
                _save_state(run_dir, state)
                print(f"\n{C_BAD}已达返修上限 {max_rounds} 轮，未收敛。{C_END}")
                print(f"失败日志: {run_dir / result['log_file']}")
                print(f"最后一轮产物: {art_path}")
                print("可人工修复后重跑 step，或调大节点 verify.max_rounds。")
                return 1
            state["rounds"][node_id] = round_no + 1
            _save_state(run_dir, state)
            task = _write_repair_task(wf, run_dir, node, ctx, result, round_no + 1)
            print(f"\n{C_WARN}自动进入返修第 {round_no + 1}/{max_rounds} 轮{C_END}")
            print(f"返修任务书: {task}")
            print(f"完成后: python orchestrator.py step {run_dir} {node_id}")
            return 0
    else:
        _save_state(run_dir, state)

    pending = _pending_nodes(wf, ctx, state)
    if not pending:
        print("\n所有节点已完成。运行 final 查看结果：")
        print(f"  python orchestrator.py final {run_dir}")
        return 0

    nxt = pending[0]
    nxt_node = next(n for n in wf.nodes if n.id == nxt)
    tasks = _write_tasks(wf, run_dir, nxt_node, ctx)
    print(f"\n下一个节点: {nxt}")
    if nxt_node.verify and all(v in ctx for v in nxt_node.outputs):
        print(f"  注: {nxt} 产物已存在但缺少自检记录（多为新加的 verify），"
              f"需要补跑一次检查，通过后即继续。")
    if len(tasks) > 1:
        print(f"  启用 Best-of-N：{len(tasks)} 份候选任务书")
        for t in tasks:
            print(f"    {t}")
        print(f"  全部写完后选优: python orchestrator.py pick {run_dir} {nxt}")
    else:
        print(f"任务书: {tasks[0]}")
        print(f"完成后: python orchestrator.py step {run_dir} {nxt}")
    return 0


def cmd_status(wf: Workflow, run_dir: Path) -> int:
    ctx = _load_ctx(run_dir)
    state = _load_state(run_dir)
    print(f"run: {run_dir.name}\n")
    for i, nid in enumerate(wf.order, 1):
        node = next(n for n in wf.nodes if n.id == nid)
        profile, _ = wf.resolve_profile(node)
        has_out = all(v in ctx for v in node.outputs)
        rounds = int(state.get("rounds", {}).get(nid, 0))
        vinfo = (state.get("verify") or {}).get(nid) or {}

        if node.verify:
            if vinfo.get("passed"):
                vmark = f"自检✓ 返修{rounds}轮"
            elif vinfo:
                vmark = f"自检✗ 第{rounds}轮"
            else:
                vmark = "待自检"
        else:
            vmark = "—"

        sizes = ", ".join(f"{v}={len(ctx[v]):,}字" for v in node.outputs if v in ctx) or "—"
        print(f"  [{i}/{len(wf.order)}] {'✓' if has_out else '·'} {pad(node.name, 8)} "
              f"({pad(nid, 10)}) {pad(profile, 10)} {pad(vmark, 12)} {sizes}")

    pending = _pending_nodes(wf, ctx, state)
    print(f"\n{'全部完成' if not pending else '待执行: ' + pending[0]}")
    return 0


def _differential_audit(wf: Workflow, run_dir: Path, ctx: dict) -> list[str]:
    """
    实质差分审计：证明 refiner 真的改了，且改得更好。
    v1.7 的 audit 只能证明记录自洽，无法证明修复落实。
    """
    issues: list[str] = []
    draft = str(ctx.get("draft_code", "")).strip()
    final = str(ctx.get("final_code", "")).strip()
    if not draft or not final:
        return ["差分审计跳过：draft_code 或 final_code 缺失"]

    h_draft, h_final = ast_hash(draft), ast_hash(final)
    if h_draft == h_final:
        issues.append(f"final 与 draft 的 AST 完全一致（{h_draft[:8]}），refiner 未做实质修改")
    else:
        issues.append(f"AST 已变更: {h_draft[:8]} → {h_final[:8]}（结构层面确有修改）")

    tool = Path(__file__).parent / "tools" / "check_code.py"
    results = {}
    for tag, src in (("draft", draft), ("final", final)):
        tmp = run_dir / f"_audit_{tag}.py"
        tmp.write_text(src, encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, str(tool), str(tmp), "--level", "runtime"],
                                  capture_output=True, text=True, timeout=300)
            results[tag] = (proc.returncode, proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired:
            results[tag] = (1, "超时")
        finally:
            tmp.unlink(missing_ok=True)

    d_code, d_out = results["draft"]
    f_code, f_out = results["final"]
    issues.append(f"探针判定: draft={'通过' if d_code == 0 else '未通过'} → "
                  f"final={'通过' if f_code == 0 else '未通过'}")
    if f_code != 0:
        bad = [ln for ln in f_out.splitlines() if ln.startswith("[") and "]" in ln][:3]
        issues.append(f"final 仍未通过探针: {' '.join(bad) or '见 check_code 输出'}")

    # Critic 提出的每条 fix 是否有对应落地（按 id 是否出现在 final 注释中做弱匹配）
    report = str(ctx.get("critique_report", ""))
    try:
        m = re.findall(r'"id"\s*:\s*"([^"]+)"', report)
        ids = list(dict.fromkeys(m))
    except Exception:  # noqa: BLE001
        ids = []
    if ids:
        landed = [i for i in ids if i in final]
        issues.append(f"修复指令 mapping: {len(landed)}/{len(ids)} 条 id 在 final 中出现 "
                      f"({', '.join(ids[:6])}{'…' if len(ids) > 6 else ''})")
        if len(landed) < len(ids):
            missing = [i for i in ids if i not in final]
            issues.append(f"未在 final 中体现的 id: {', '.join(missing[:8])}（弱匹配，仅供参考）")

    return issues


def cmd_audit(wf: Workflow, run_dir: Path) -> int:
    """三路比对：配置解析 / 任务书声明 / trace 记录，确认全节点同一档位。"""
    ctx = _load_ctx(run_dir)
    report = wf.validate()
    bi = report.get("builtin") or {}
    cfg = {b["node"]: (b["profile"], b["profile_source"]) for b in report["bindings"]}
    cfg_profiles, cfg_uniform = wf.profile_summary(report)

    trace_nodes: dict[str, dict] = {}
    tp = run_dir / "trace.json"
    if tp.exists():
        for rec in json.loads(tp.read_text(encoding="utf-8")).get("nodes", []):
            if isinstance(rec, dict) and rec.get("node_id"):
                trace_nodes[rec["node_id"]] = rec

    tdir = run_dir / TASK_DIR
    print(f"run: {run_dir.name}")
    print(f"runtime: {report.get('runtime')}   锁定档位: {bi.get('profile')} (lock={bi.get('lock')})")
    print(f"配置侧生效档位: {sorted(cfg_profiles)} → {'一致' if cfg_uniform else '不一致'}\n")

    rows, verdicts = [], []
    for nid in wf.order:
        cprof, csrc = cfg.get(nid, ("?", "?"))

        tprof = "—"
        if tdir.exists():
            # 候选任务书形如 02_generator.c1.md，先剥掉 .c<N> 再比节点名
            def _node_of(p: Path) -> str:
                return re.sub(r"\.c\d+$", "", p.stem.split("_", 1)[-1])

            tfile = next((p for p in sorted(tdir.glob("*.md")) if _node_of(p) == nid), None)
            if tfile:
                m = PROFILE_RE.search(tfile.read_text(encoding="utf-8"))
                tprof = m.group(1) if m else "未声明"

        rec = trace_nodes.get(nid, {})
        rprof = rec.get("profile", "—")
        rmode = rec.get("mode", "api" if rec.get("model") else "—")

        seen = {p for p in (cprof, tprof, rprof) if p not in ("—", "?", "未声明")}
        # 严格判定：配置必须等于锁定值，且任务书与 trace 都必须有记录。
        # 缺失记录不能算“一致”—— 那只是“未审计”。
        # lock=true：配置必须等于锁定值；lock=false：只要求「配置 = 任务书 = trace」自洽，
        # 不要求等于 builtin.profile（混合路由是 v1.9 的设计意图）
        if bi.get("lock"):
            ok = (
                len(seen) == 1
                and cprof == bi.get("profile")
                and tprof not in ("—", "?", "未声明")
                and rprof not in ("—", "?")
            )
        else:
            ok = (
                len(seen) == 1
                and tprof not in ("—", "?", "未声明")
                and rprof not in ("—", "?")
            )
        verdicts.append(ok)
        rows.append([nid, cprof, csrc, tprof, rprof, rmode, "✓" if ok else "✗"])

    headers = ["节点", "配置", "来源", "任务书", "trace", "模式", "判定"]
    widths = [max(width(h), *(width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("─" * widths[i] for i in range(len(headers))))
    for r in rows:
        print("  " + "  ".join(pad(c, widths[i]) for i, c in enumerate(r)))

    diff_lines = _differential_audit(wf, run_dir, ctx)
    print("\n差分审计:")
    for line in diff_lines:
        print(f"  · {line}")

    print()
    if all(verdicts):
        if bi.get("lock"):
            print(f"结论: ✓ 全部 {len(verdicts)} 个节点生效档位均为 {bi.get('profile')}，锁定有效。")
        else:
            used = sorted({r[1] for r in rows})
            print(f"结论: ✓ {len(verdicts)} 个节点的「配置 = 任务书 = trace」全部自洽"
                  f"（混合路由: {', '.join(used)}，lock=false 允许逐点覆盖）。")
        return 0

    missing = [wf.order[i] for i, ok in enumerate(verdicts) if not ok]
    print(f"结论: ✗ 档位不一致或未记录: {', '.join(missing)}")
    if any(r[4] == "—" for r in rows):
        print("  提示: trace 缺少 profile 字段，说明该节点由 api 模式执行，未走内置档位。")
    return 1


def _write_verdict_task(wf: Workflow, run_dir: Path, node, cands: list[Path],
                        payload: dict, ctx: dict) -> Path:
    """生成语义评选任务书：让模型核对候选是否符合契约，输出 verdict.json。"""
    survivors = [c for c in payload["candidates"] if c["passed"]]
    blocks = []
    for c in survivors:
        src = Path(c["path"]).read_text(encoding="utf-8", errors="replace")
        if len(src) > 6000:
            src = src[:6000] + "\n# …（超出 6000 字符已截断）"
        blocks.append(f"### 候选 {c['id']}（规则分 {c['score']}）\n```python\n{src}\n```")

    ids = [c["id"] for c in survivors] or ["generator.c1"]
    sid = ids[0]
    bias = ", ".join(f'"{i}": false' for i in ids)
    spec = str(ctx.get("spec", "")).strip() or "（本次 run 无契约，按原始需求判断）"
    md = f"""# 语义评选任务 · {node.name} ({node.id})

- run: `{run_dir.name}`
- 候选数: {len(survivors)}（已通过 check_code 硬门槛的）
- 你的任务: 只核对**语义正确性**，不要重复代码规范类的评审

## 契约

{spec}

## 候选代码

{chr(10).join(blocks)}

## 评分要求

对每份候选打 0-100 分，只按这三条判断：
1. 是否严格实现契约里的信号时序（用什么截面算、何时成交、收益口径）
2. 指标公式与契约的数学表达是否一致（含预热期与边界值）
3. 交易约束（停牌/涨跌停/成本）是否按契约落地

不评代码风格、不评性能、不评注释——那些规则分已经算过。

## 输出

写入 `{run_dir / 'candidates' / 'verdict.json'}`，格式严格如下：

```json
{{
  "scores": {{"{sid}": 88}},
  "has_lookahead_bias": {{{bias}}},
  "reason": "一句话说明各候选的主要差异"
}}
```

要求：
- `scores` 必须覆盖全部 {len(survivors)} 份候选，键名就用 `{sid}` 这种完整 id，不要再拼 `.c1`
- `has_lookahead_bias` 同样覆盖全部候选，含未来函数/同期成交写 `true`，否则 `false`
- 两个字段缺一个，pick 就无法正确加权

写完回复一句话，不要回贴代码。
"""
    tdir = run_dir / TASK_DIR
    _ensure_dir(tdir)
    path = tdir / f"pick_{node.id}_verdict.md"
    path.write_text(md, encoding="utf-8")
    return path


def cmd_pick(wf: Workflow, run_dir: Path, node_id: str, emit: bool = False,
             degraded: bool = False) -> int:
    """Best-of-N 选优：规则分占 70%，candidates/verdict.json 存在时叠加语义分 30%。"""
    ctx = _load_ctx(run_dir)
    state = _load_state(run_dir)
    node = next((n for n in wf.nodes if n.id == node_id), None)
    if node is None:
        raise WorkflowError(f"未知节点: {node_id}")
    if not node.best_of_n:
        raise WorkflowError(f"节点 {node_id} 未配置 best_of_n，无需 pick")

    count = int(node.best_of_n.get("n", 1))
    art_path, _ = _artifact_path(wf, run_dir, node)
    suffix = art_path.suffix or ".txt"
    cdir = run_dir / "candidates"
    cands = [cdir / f"{node.id}.c{i}{suffix}" for i in range(1, count + 1)]

    missing = [c.name for c in cands if not c.exists()]
    if missing:
        raise WorkflowError(
            f"候选不齐，缺少 {len(missing)}/{count} 份: {', '.join(missing)}。"
            f"先把 tasks/*.c*.md 全部执行完"
        )

    tool = Path(__file__).parent / "tools" / "score_candidates.py"
    proc = subprocess.run(
        [sys.executable, str(tool), *[str(c) for c in cands]],
        capture_output=True, text=True, timeout=240,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"评分器输出无法解析: {exc}\n{proc.stderr[:500]}") from exc

    _ensure_dir(cdir)
    (cdir / "score.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 语义分（可选）----
    verdict_path = cdir / "verdict.json"
    # 注意：这里必须保留顶层字典。
    # v1.8 的 bug 是先 .get("scores") 再 .get("scores")，第二次必然拿到 {}，
    # 导致语义分永远是 None、has_lookahead_bias 永远读不到。
    verdict_raw: dict = {}
    if verdict_path.exists():
        try:
            verdict_raw = json.loads(verdict_path.read_text(encoding="utf-8"))
            if not isinstance(verdict_raw, dict):
                raise ValueError("顶层不是对象")
        except (json.JSONDecodeError, ValueError):
            print(f"{C_WARN}verdict.json 结构非法，本次只用规则分{C_END}")
            verdict_raw = {}

    # ---- 硬门禁 + 几何加权 ----
    # 一票否决：探针失败 / 语义分 < 65 / 语义层标记 has_lookahead_bias
    # 未被否决者用 sqrt(rule * semantic)，任一项拉胯都会显著压低总分（几何平均惩罚短板）
    sem_map = verdict_raw.get("scores", {}) or {}
    bias_map = verdict_raw.get("has_lookahead_bias", {}) or {}
    verdict = verdict_raw

    for c in payload["candidates"]:
        sem = sem_map.get(c["id"])
        c["semantic"] = sem if isinstance(sem, (int, float)) else None
        vetoes = list(c.get("veto_reasons") or [])
        if not c.get("passed", True):
            c["status"] = "VETOED"
        elif c["semantic"] is not None and (c["semantic"] < 65 or bool(bias_map.get(c["id"]))):
            c["status"] = "VETOED"
            vetoes.append("SEMANTIC" if c["semantic"] < 65 else "LLM_BIAS_FLAG")
        else:
            c["status"] = "PASS"

        c["veto_reasons"] = vetoes
        if c["status"] == "VETOED":
            c["final"] = 0.0
        elif c["semantic"] is not None:
            c["final"] = round((max(c["score"], 0) ** 0.5) * (max(c["semantic"], 0) ** 0.5), 1)
        else:
            c["final"] = float(c["score"])

    ranked = sorted(payload["candidates"],
                    key=lambda c: (c["status"] != "PASS", -c["final"], c["id"]))
    best = next((c["id"] for c in ranked if c["status"] == "PASS"), None)

    if emit:
        task = _write_verdict_task(wf, run_dir, node, cands, payload, ctx)
        print(f"语义评选任务书: {task}")
        print(f"执行后把结果写入 {cdir / 'verdict.json'}，再运行 pick 即自动加权：")
        print(f"  python orchestrator.py pick {run_dir} {node_id}")
        return 0

    mode = "几何加权 sqrt(rule*semantic)" if verdict else "仅规则分（无 verdict.json）"
    print(f"Best-of-N 评分（{node_id}，{count} 份候选，{mode}）\n")
    rows = []
    for c in ranked:
        mark = "★" if c["id"] == best else " "
        bd = c.get("breakdown") or {}
        dims = (f"{bd.get('规范', 0)}/{bd.get('向量化', 0)}/"
                f"{bd.get('稳健', 0)}/{bd.get('契约', 0)}") if bd else "—"
        rows.append([mark, c["id"], str(c["score"]),
                     "—" if c["semantic"] is None else str(c["semantic"]),
                     str(c["final"]), c["status"], ",".join(c["veto_reasons"]) or "—", dims])
    headers = ["选", "候选", "规则", "语义", "终分", "状态", "否决项", "规范/向量/稳健/契约"]
    widths = [max(width(h), *(width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("─" * widths[i] for i in range(len(headers))))
    for r in rows:
        print("  " + "  ".join(pad(c, widths[i]) for i, c in enumerate(r)))

    print("\n加减分明细:")
    for c in ranked[:3]:
        brief = "; ".join(r.splitlines()[0][:90] for r in c["reasons"][:4])
        print(f"  [{c['id']}] {brief}")

    if not best:
        if not degraded:
            print(f"\n{C_BAD}全部候选未通过硬门槛，无法选优{C_END}")
            print(f"逐份日志: {cdir / 'score.json'}")
            print(f"{C_WARN}如需带缺陷继续（把问题交给 critic 修），加 --degraded{C_END}")
            return 1

        # 兜底：按「否决项越少越好 + 缺陷等级越低越好」挑一份，明确标记 DEGRADED
        order = ["SECURITY", "CONTRACT", "SMOKE", "CAUSAL", "TIMING", "PANEL", "SHAPE", "SUITE"]
        def sev(c):
            rs = [r for r in (c.get("veto_reasons") or []) if r in order]
            return (len(rs), order.index(rs[0]) if rs else 99)
        pick = sorted(ranked, key=sev)[0]
        pick["status"] = "DEGRADED"
        pick["final"] = 0.0
        best = pick["id"]
        print(f"\n{C_WARN}全部候选被否决，--degraded 降级选中 {best}{C_END}")
        print(f"  否决项: {', '.join(pick.get('veto_reasons') or []) or '未知'}")
        print(f"  这是兜底，问题会随 precheck 注入 critic，必须人工复核最终产物")

    src = next(c for c in cands if c.stem == best)
    shutil.copy2(src, art_path)
    picked = next(c for c in ranked if c["id"] == best)

    # pick 的硬门禁刚对这份代码跑过 check_code --level runtime 且全绿，
    # 把结论缓存进 state/ctx，避免 step 阶段再跑一遍相同探针（v1.8 共跑了 3 次）
    ok_pick = picked.get("status") != "DEGRADED"
    state.setdefault("verify", {})[node_id] = {
        "passed": ok_pick,
        "exit_code": 0 if ok_pick else 1,
        "cmd": "cached_from_pick_gate" if ok_pick else "cached_from_pick_degraded",
        "log_file": "candidates/score.json",
        "elapsed_s": 0.0,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if picked.get("status") == "DEGRADED":
        detail = "\n".join(str(r)[:600] for r in (picked.get("reasons") or ["无详情"]))
        ctx["precheck_log"] = (
            f"命令: check_code --level runtime（pick 降级选中 {best}）\n"
            f"退出码: 1（未通过）\n"
            f"否决项: {', '.join(picked.get('veto_reasons') or [])}\n\n"
            f"{detail}\n\n"
            f"注意: 这份初稿带着上述缺陷被选中，请务必逐条审查并给出修复指令。"
        )
    else:
        ctx["precheck_log"] = (
            "命令: check_code --level runtime（由 pick 硬门禁缓存复用，未重复执行）\n"
            "退出码: 0（通过）\n\n"
            "[OK]\n动态探针全部通过：smoke=ok / causal=ok / timing=ok\n[/OK]"
        )
    state.setdefault("bon", {})[node_id] = {
        "n": count,
        "picked": best,
        "score": next(c["final"] for c in ranked if c["id"] == best),
        "scores": {c["id"]: c["final"] for c in ranked},
        "semantic_used": bool(verdict),
    }
    _save_ctx(run_dir, ctx)
    _save_state(run_dir, state)

    picked_score = next(c["final"] for c in ranked if c["id"] == best)
    print(f"\n{C_OK}选定 {best}{C_END}（终分 {picked_score}）→ {art_path.name}")
    print(f"评分表: {cdir / 'score.json'}")
    if not verdict:
        print(f"提示: 加语义分可先运行 `pick --emit`，让模型核对契约一致性后再 pick")
    print(f"下一步: python orchestrator.py step {run_dir} {node_id}")
    return 0


def cmd_final(wf: Workflow, run_dir: Path) -> int:
    ctx = _load_ctx(run_dir)
    state = _load_state(run_dir)
    pending = _pending_nodes(wf, ctx, state)
    if pending:
        raise WorkflowError(f"还有节点未完成: {', '.join(pending)}")

    for key, tpl in wf.output_map.items():
        value = wf.render(str(tpl), ctx, f"outputs.{key}")
        out_path = run_dir / f"{key}.txt"
        out_path.write_text(value, encoding="utf-8")
        print(f"{key} → {out_path} ({len(value):,} 字符)")
        print("-" * 60)
        print("\n".join(value.splitlines()[:12]))
        print("-" * 60)
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="量化代码自我反思流 · builtin 运行时")
    ap.add_argument("--config", "-c", default=str(Path(__file__).parent / "workflow.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="新建 run 并生成首个节点任务书")
    p_init.add_argument("--task", "-t", required=True)
    p_init.add_argument("--base", default=str(Path(__file__).parent / "runs"))

    p_status = sub.add_parser("status", help="查看 run 进度")
    p_status.add_argument("run_dir")

    p_step = sub.add_parser("step", help="回收节点产物并推进")
    p_step.add_argument("run_dir")
    p_step.add_argument("node_id")

    p_final = sub.add_parser("final", help="输出最终结果")
    p_final.add_argument("run_dir")

    p_audit = sub.add_parser("audit", help="三路比对，确认全节点同一档位")
    p_audit.add_argument("run_dir")

    p_pick = sub.add_parser("pick", help="Best-of-N 选优：规则打分后落为正式产物")
    p_pick.add_argument("run_dir")
    p_pick.add_argument("node_id")
    p_pick.add_argument("--degraded", action="store_true",
                        help="全部候选被否决时仍选一份继续（问题交给 critic 修，需人工复核）")
    p_pick.add_argument("--emit", action="store_true",
                        help="只生成语义评选任务书（结果写入 verdict.json 后重跑 pick 自动加权）")

    args = ap.parse_args()

    try:
        wf = Workflow.load(args.config)
    except (WorkflowError, ValidationError) as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "init":
            return cmd_init(wf, args.task.strip(), Path(args.base).expanduser().resolve())
        if args.cmd == "status":
            return cmd_status(wf, Path(args.run_dir).expanduser().resolve())
        if args.cmd == "step":
            return cmd_step(wf, Path(args.run_dir).expanduser().resolve(), args.node_id)
        if args.cmd == "final":
            return cmd_final(wf, Path(args.run_dir).expanduser().resolve())
        if args.cmd == "audit":
            return cmd_audit(wf, Path(args.run_dir).expanduser().resolve())
        if args.cmd == "pick":
            return cmd_pick(wf, Path(args.run_dir).expanduser().resolve(),
                            args.node_id, emit=getattr(args, "emit", False),
                            degraded=getattr(args, "degraded", False))
    except ValidationError as exc:
        print("校验失败:", file=sys.stderr)
        for p in exc.problems:
            print(f"  · {p}", file=sys.stderr)
        return 2
    except WorkflowError as exc:
        print(f"中断: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
