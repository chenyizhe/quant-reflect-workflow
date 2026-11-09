#!/usr/bin/env python3
"""
validate_critique.py — 审查报告结构校验（v1.8）

用法:
    python validate_critique.py <critique_report.md>

v1.8 起 critic 输出紧凑 JSON，不再用 Markdown 段落截取（那个会静默退化成全文）。
本校验器是 critic 节点的 verify：结构不合规就触发返修，绝不静默放行。

要求 Schema:
    {
      "summary": "一句话总评",
      "verdict": "block" | "pass",
      "actionable_fixes": [
        {"id": "P01", "severity": "阻断|高危|中危|低危",
         "target_function": "run_strategy", "line_hint": "L123",
         "root_cause": "为什么错", "exact_code_change": "改成什么"}
      ]
    }

退出码: 0 通过, 1 未通过
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

JSON_BLOCK = re.compile(r"```json\s*", re.S)


def extract_json(text: str) -> str | None:
    """
    取第一个 ```json 到**最后一个** ``` 之间的内容。
    不能用非贪婪匹配：exact_code_change 字段里常嵌 ```python 片段，
    非贪婪会在第一个内层围栏处截断，把合法 JSON 误判为语法错。
    """
    m = JSON_BLOCK.search(text)
    if m:
        start = m.end()
        end = text.rfind("```")
        if end > start:
            return text[start:end]
        return text[start:]
    return None
REQUIRED_FIX_KEYS = ("id", "severity", "target_function", "line_hint",
                     "root_cause", "exact_code_change")
SEVERITIES = {"阻断", "高危", "中危", "低危"}


def emit(tag: str, detail: str) -> None:
    print(f"[{tag}]")
    print(detail.rstrip())
    print(f"[/{tag}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    args = ap.parse_args()

    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        emit("CRITIQUE", f"报告不存在: {path}")
        return 1

    text = path.read_text(encoding="utf-8", errors="replace")
    raw = extract_json(text)

    payload: dict | None = None
    if raw is not None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            emit("CRITIQUE", f"JSON 块解析失败: {exc}\n"
                             f"常见原因: 字段内嵌了未转义的引号或多余逗号")
            return 1
    else:
        # 容忍"裸 JSON"（模型没加围栏但整体是 JSON）
        try:
            payload = json.loads(text.strip())
        except json.JSONDecodeError:
            emit("CRITIQUE",
                 "报告里没有合法 JSON。v1.8 要求 critic 只输出 JSON（可放 ```json 块内），"
                 "字段: summary / verdict / actionable_fixes[]。"
                 "Markdown 叙述会被直接拒绝——它无法保证下游结构一致")
            return 1

    if not isinstance(payload, dict):
        emit("CRITIQUE", f"顶层必须是对象，收到 {type(payload).__name__}")
        return 1

    missing = [k for k in ("summary", "actionable_fixes") if k not in payload]
    if missing:
        emit("CRITIQUE", f"缺字段: {missing}")
        return 1

    fixes = payload["actionable_fixes"]
    if not isinstance(fixes, list):
        emit("CRITIQUE", f"actionable_fixes 必须是数组，收到 {type(fixes).__name__}")
        return 1
    if not fixes:
        if str(payload.get("verdict", "")).strip().lower() == "pass":
            emit("OK", "审查通过：verdict=pass，无阻断或待修复项")
            return 0
        emit("CRITIQUE", "actionable_fixes 为空且 verdict 不是 pass。"
                         "要么给 verdict=pass，要么必须给出至少一条可执行修复")
        return 1

    bad = []
    for i, fix in enumerate(fixes, 1):
        if not isinstance(fix, dict):
            bad.append(f"第 {i} 条不是对象")
            continue
        miss = [k for k in REQUIRED_FIX_KEYS if not str(fix.get(k, "")).strip()]
        if miss:
            bad.append(f"第 {i} 条缺 {miss}")
        sev = str(fix.get("severity", "")).strip()
        if sev and sev not in SEVERITIES:
            bad.append(f"第 {i} 条 severity={sev!r} 不在 {sorted(SEVERITIES)}")

    if bad:
        emit("CRITIQUE", "\n".join(bad[:10]))
        return 1

    blocking = [f for f in fixes if str(f.get("severity")) == "阻断"]
    verdict = str(payload.get("verdict", "")).strip()
    if verdict == "block" and not blocking:
        print("[WARN] verdict=block 但没有任何『阻断』级 fix —— 判断不自洽，"
              "要么降 verdict 为 pass，要么把最严重的一条标成阻断")
    emit("OK", f"审查结构通过：{len(fixes)} 条可执行修复，其中阻断 {len(blocking)} 条；"
               f"verdict={payload.get('verdict', '未标注')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
