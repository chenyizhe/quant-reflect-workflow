#!/usr/bin/env python3
"""
score_candidates.py — Best-of-N 候选评分器 v2（确定性为主，LLM 为辅）

用法:
    python score_candidates.py <cand1.py> <cand2.py> ... [--python PATH]
    python score_candidates.py --explain <cand.py>

评分口径：
    客观分（0-100，本脚本负责）= 静态合规 30 + 向量化 20 + 边界处理 30 + 契约匹配 20
    语义分（0-100，交由 LLM 核对）由 orchestrator.py pick --with-llm 另行询问，
    最终分 = 0.7 * 客观分 + 0.3 * 语义分。本脚本只输出客观分。

硬性门槛（一票否决）：
    调用 check_code.py --level smoke（语法 + 依赖 + 量化探针 + 合成数据冒烟）。
    不过者 score=0、passed=false，不参与排名。

它筛的是「是否考虑到了该考虑的东西」，不是「策略逻辑对不对」。
逻辑正确性由 critic 负责。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOL = Path(__file__).parent / "check_code.py"
ENTRY_LIKE = ("strategy", "run", "signal", "backtest", "compute", "calc", "generate")

PLACEHOLDERS = [
    (re.compile(r"\bTODO\b"), "TODO"),
    (re.compile(r"\bFIXME\b"), "FIXME"),
    (re.compile(r"NotImplementedError"), "NotImplementedError"),
    (re.compile(r"^\s*\.\.\.\s*$", re.M), "省略号"),
]

WINDOWING = {"rolling", "expanding", "ewm", "resample", "groupby"}
VECTOR_HINTS = {"to_numpy", "values", "np", "numpy", "vectorize", "apply", "where", "clip"}
GUARD_CALLS = {"replace", "clip", "where", "maximum", "minimum", "fillna", "dropna", "mask"}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _collect_ids(tree: ast.Module) -> set[str]:
    ids: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            ids.add(n.id)
        elif isinstance(n, ast.Attribute):
            ids.add(n.attr)
        elif isinstance(n, ast.arg):
            ids.add(n.arg)
        elif (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
              and isinstance(n.slice.value, str)):
            ids.add(n.slice.value)
    return ids


def _funcs(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


# ---------------------------------------------------------------- 四个维度
def dim_convention(tree: ast.Module, src: str) -> tuple[int, list[str]]:
    """静态合规 30 分。"""
    score, notes = 0, []
    fns = _funcs(tree)
    if fns:
        documented = sum(1 for f in fns if ast.get_docstring(f))
        add = round(10 * documented / len(fns))
        score += add
        if add:
            notes.append(f"规范 +{add} docstring {documented}/{len(fns)}")

        named = sum(1 for f in fns if len(f.name) > 2 and not re.search(r"\d$", f.name))
        add = round(10 * named / len(fns))
        score += add
        if add:
            notes.append(f"规范 +{add} 函数命名 {named}/{len(fns)}")

    hits = [(pat, label) for pat, label in PLACEHOLDERS if pat.search(src)]
    if not hits:
        score += 10
        notes.append("规范 +10 无占位符")
    else:
        notes.append(f"规范 +0 含占位符 {[h[1] for h in hits]}")
    return score, notes


def dim_vectorized(tree: ast.Module, src: str) -> tuple[int, list[str]]:
    """向量化 20 分。"""
    score, notes = 0, []
    names = {_call_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    ids = _collect_ids(tree)

    if names & {"iterrows", "itertuples"}:
        notes.append("向量化 +0 使用 iterrows/itertuples")
    else:
        score += 10
        notes.append("向量化 +10 无逐行遍历")

    if names & WINDOWING or ids & VECTOR_HINTS:
        score += 10
        used = sorted((names & WINDOWING) | (ids & {"to_numpy", "np", "numpy"}))[:3]
        notes.append(f"向量化 +10 使用 {used}")
    else:
        notes.append("向量化 +0 未见窗口/向量化算子")
    return score, notes


def dim_robust(tree: ast.Module, src: str) -> tuple[int, list[str]]:
    """边界与数值稳健 30 分。"""
    score, notes = 0, []
    names = {_call_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    raises = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Raise))

    if raises:
        add = min(12, 4 * raises)
        score += add
        notes.append(f"稳健 +{add} 参数/输入校验 raise ×{raises}")
    else:
        notes.append("稳健 +0 无任何 raise 校验")

    guards = len([n for n in names if n in GUARD_CALLS])
    if guards:
        add = min(12, 4 * guards)
        score += add
        notes.append(f"稳健 +{add} replace/where/clip 防护 ×{guards}")
    elif "isfinite" in src or ".isna(" in src or ".notna(" in src:
        score += 6
        notes.append("稳健 +6 有缺失值判断")
    else:
        notes.append("稳健 +0 无缺失值/除零防护")

    if re.search(r"reset_index\s*\(\s*drop\s*=\s*True", src):
        notes.append("稳健 +0 使用 reset_index(drop=True)（破坏 index 对齐）")
    else:
        score += 6
        notes.append("稳健 +6 保留调用方 index")
    return score, notes


def dim_contract(tree: ast.Module, src: str) -> tuple[int, list[str]]:
    """接口契约 20 分。"""
    score, notes = 0, []
    fns = _funcs(tree)
    # 只评强制入口；辅助函数（_run_one 等）不参与契约评分，
    # 否则 run_strategy(df, **params) 会被兄弟函数拉低分母
    entry = ([f for f in fns if f.name == "run_strategy"]
             or [f for f in fns if any(f.name.startswith(h) for h in ENTRY_LIKE)]
             or fns)
    if not entry:
        return 0, ["契约 +0 无公开函数"]

    annotated_ret = sum(1 for f in entry if f.returns is not None)
    add = round(8 * annotated_ret / len(entry))
    score += add
    if add:
        notes.append(f"契约 +{add} 返回注解 {annotated_ret}/{len(entry)}")

    # 有 **params / **kwargs 说明接口可配置，即使没有显式默认值也应给分
    with_defaults = sum(1 for f in entry
                        if f.args.defaults or f.args.kw_defaults or f.args.kwarg is not None)
    add = round(6 * with_defaults / len(entry))
    score += add
    if add:
        notes.append(f"契约 +{add} 可配置默认值 {with_defaults}/{len(entry)}")

    typed_args = total_args = 0
    for f in entry:
        for a in list(f.args.args) + list(f.args.kwonlyargs):
            if a.arg in ("self", "cls", "df", "data", "frame"):
                continue
            total_args += 1
            typed_args += a.annotation is not None
    if total_args:
        add = round(6 * typed_args / total_args)
        score += add
        if add:
            notes.append(f"契约 +{add} 参数类型标注 {typed_args}/{total_args}")
    else:
        # 仅 df + **params：没有需要逐个标注的业务参数，接口已由 **params 收敛，给满分
        score += 6
        notes.append("契约 +6 仅数据入参 + **params（无需逐项标注）")
    return score, notes


# ---------------------------------------------------------------- 硬门槛
VETO_TAGS = ("SECURITY", "CONTRACT", "SMOKE", "CAUSAL", "TIMING", "PANEL", "SHAPE", "SUITE")


def gate(path: Path, python: str) -> tuple[bool, str, list[str]]:
    """硬门禁：跑 check_code 的 runtime 级（含因果/时序/CoW 探针）。"""
    try:
        proc = subprocess.run(
            [python, str(TOOL), str(path), "--level", "runtime"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "[GATE] check_code 超时", ["TIMEOUT"]
    detail = (proc.stdout + proc.stderr).strip()
    hits = [tag for tag in VETO_TAGS if f"[{tag}]" in detail]
    return proc.returncode == 0, detail, hits


def score_one(path: Path, python: str) -> dict:
    base = {"id": path.stem, "path": str(path)}
    if not path.exists() or path.stat().st_size == 0:
        return {**base, "score": 0, "passed": False, "gate": "文件缺失或为空",
                "breakdown": {}, "reasons": ["文件缺失或为空"]}

    ok, detail, veto = gate(path, python)
    if not ok:
        head = "\n".join(detail.splitlines()[:12])
        return {**base, "score": 0, "passed": False, "status": "VETOED",
                "gate": "check_code(runtime) 未通过", "veto_reasons": veto or ["GATE_FAIL"],
                "breakdown": {}, "reasons": [head]}

    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {**base, "score": 0, "passed": False, "gate": "语法错误",
                "breakdown": {}, "reasons": ["语法错误"]}

    s1, n1 = dim_convention(tree, src)
    s2, n2 = dim_vectorized(tree, src)
    s3, n3 = dim_robust(tree, src)
    s4, n4 = dim_contract(tree, src)
    total = s1 + s2 + s3 + s4

    return {
        **base,
        "score": max(0, min(100, total)),
        "passed": True,
        "gate": "pass",
        "status": "PASS",
        "veto_reasons": [],
        "breakdown": {"规范": s1, "向量化": s2, "稳健": s3, "契约": s4},
        "reasons": n1 + n2 + n3 + n4,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--explain", help="只评一份并输出完整明细")
    args = ap.parse_args()

    files = [args.explain] if args.explain else args.files
    if not files:
        ap.error("需要至少一个候选文件")

    # 并行跑门禁：每份候选都是一次 check_code 子进程（含 5 个探针），串行会线性叠加
    resolved = [Path(f).expanduser().resolve() for f in files]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(resolved)))) as pool:
        results = list(pool.map(lambda q: score_one(q, args.python), resolved))
    ranked = sorted(results, key=lambda r: (not r["passed"], -r["score"], r["id"]))
    payload = {
        "candidates": ranked,
        "best": next((r["id"] for r in ranked if r["status"] == "PASS"), None),
        "weights": {"客观": 1.0, "说明": "语义分由 pick --with-llm 另行加权 0.7/0.3"},
        "rule": "gate(check_code runtime: 因果+时序+CoW) → 规范30+向量化20+稳健30+契约20",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["best"] else 1


if __name__ == "__main__":
    sys.exit(main())
