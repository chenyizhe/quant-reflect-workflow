#!/usr/bin/env python3
"""
validate_spec.py — 契约结构校验（v1.8）

用法:
    python validate_spec.py <spec.md> [--max-assumptions 3]

spec.md 要求在正文末尾附一个 ```json 契约块，含：
    signal_timing     信号的截面口径（如 "t_close"）
    execution_price   成交价口径（如 "t1_open"）
    warmup_period     预热期（整数）
    nan_policy        缺失值策略（如 "ffill_limit20"）
    entry_signature   入口签名（如 "run_strategy(df, window=20) -> DataFrame")
    assumed_defaults  模型自行拍板的默认值列表

退出码: 0 通过, 1 未通过
assumed_defaults 超过阈值只告警不阻断（模型不可能零假设），但会打 [SPEC_WARN]。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED = ("signal_timing", "execution_price", "warmup_period",
            "nan_policy", "entry_signature", "assumed_defaults")
JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.S)

# 防前视的硬编码红线：这些取值组合必须自洽
FORBIDDEN_EXEC = {"t_close", "same_bar", "t_open_same_bar", "close_same_bar"}


def emit(tag: str, detail: str) -> None:
    print(f"[{tag}]")
    print(detail.rstrip())
    print(f"[/{tag}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--max-assumptions", type=int, default=5)
    args = ap.parse_args()

    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        emit("SPEC", f"契约文件不存在: {path}")
        return 1

    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = JSON_BLOCK.findall(text)
    if not blocks:
        emit("SPEC", "契约缺少 ```json 结构化块。契约必须是可机器校验的，"
                     "纯 Markdown 叙述无法保证下游一致")
        return 1

    try:
        spec = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        emit("SPEC", f"契约 JSON 解析失败: {exc}")
        return 1

    missing = [k for k in REQUIRED if k not in spec]
    if missing:
        emit("SPEC", f"契约缺字段: {missing}。必须包含 {list(REQUIRED)}")
        return 1

    timing = str(spec["signal_timing"]).lower()
    price = str(spec["execution_price"]).lower()

    # 硬红线：信号用 t 收盘，成交却用 t 收盘/同根 → 前视
    if any(k in price for k in FORBIDDEN_EXEC):
        emit("SPEC", f"契约自相矛盾: signal_timing={timing!r} 但 execution_price={price!r}。"
                     f"t 日收盘生成的信号不得在 t 日成交，必须延到 t+1")
        return 1

    try:
        warmup = int(spec["warmup_period"])
        if warmup < 0:
            raise ValueError
    except (TypeError, ValueError):
        emit("SPEC", f"warmup_period 必须是非负整数，收到 {spec['warmup_period']!r}")
        return 1

    defaults = spec["assumed_defaults"]
    if not isinstance(defaults, list):
        emit("SPEC", f"assumed_defaults 必须是数组，收到 {type(defaults).__name__}")
        return 1

    if len(defaults) > args.max_assumptions:
        emit("SPEC_WARN",
             f"契约含 {len(defaults)} 条自拟默认值（阈值 {args.max_assumptions}）:\n  "
             + "\n  ".join(f"- {d}" for d in defaults[:6])
             + "\n这些默认值会约束后续所有节点。需求方应确认它们是否成立")
        # 只告警，不阻断：模型不可能零假设
        print(f"[OK] 契约结构通过（含 {len(defaults)} 条默认值，已告警）")
        return 0

    emit("OK", f"契约结构通过：timing={timing}, exec={price}, warmup={warmup}, "
               f"默认值 {len(defaults)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
