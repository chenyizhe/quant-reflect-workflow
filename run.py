#!/usr/bin/env python3
"""
run.py — 量化代码自我反思流 CLI

用法:
    python run.py                     # 交互式：提示输入 user_task
    python run.py --task "..."        # 直接执行
    python run.py --validate          # 只做 DAG / 变量绑定静态校验（不调模型）
    python run.py --task "..." --dry-run   # 空跑全链路，不消耗 token
    python run.py --config other.yaml # 指定配置文件
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

from workflow_engine import ValidationError, Workflow, WorkflowError

C_RESET, C_DIM, C_BOLD = "\033[0m", "\033[2m", "\033[1m"
C_CYAN, C_GREEN, C_YELLOW, C_RED = "\033[36m", "\033[32m", "\033[33m", "\033[31m"


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def pad(text: str, w: int, align: str = "left") -> str:
    text = str(text)
    gap = max(0, w - width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], width(cell))
    line = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    bottom = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"
    out = [top, "│" + "│".join(f" {pad(h, widths[i], 'center')} " for i, h in enumerate(headers)) + "│", line]
    for row in rows:
        out.append("│" + "│".join(f" {pad(c, widths[i])} " for i, c in enumerate(row)) + "│")
    out.append(bottom)
    return "\n".join(out)


def print_binding(wf: Workflow, report: dict) -> None:
    runtime = report.get("runtime", "api")
    print(f"\n{C_BOLD}工作流{C_RESET}: {C_CYAN}{report['workflow']}{C_RESET} v{report['version']}")
    print(f"{C_BOLD}运行时{C_RESET}: {runtime}")
    if runtime == "builtin":
        bi = report.get("builtin") or {}
        profiles, uniform = wf.profile_summary(report)
        lock = "锁定" if bi.get("lock") else "可覆盖"
        if bi.get("lock"):
            mark = f"{C_GREEN}✓ 全节点一致{C_RESET}" if uniform else f"{C_RED}✗ 档位不一致{C_RESET}"
        else:
            # lock=false 时混合档位是设计意图（Flash 主导 + critic 升级），不是错误
            mark = (f"{C_GREEN}✓ 混合路由{C_RESET}" if not uniform
                    else f"{C_GREEN}✓ 全节点一致{C_RESET}")
        print(f"{C_BOLD}内置档位{C_RESET}: {C_CYAN}{bi.get('profile')}{C_RESET} "
              f"[{lock}] · {', '.join(sorted(profiles))} · {mark}")
    print(f"{C_BOLD}执行链{C_RESET}: {C_DIM}{' → '.join(report['order'])}{C_RESET}")
    print(f"{C_BOLD}输入变量{C_RESET}: {', '.join(report['inputs'])}")
    print(f"{C_BOLD}最终输出{C_RESET}: {', '.join(report['outputs'])}")

    rows = []
    for b in report["bindings"]:
        reads = ", ".join(f"{{{{{v}}}}}" for v in b["reads"]) or "—"
        col = b["model"]
        if runtime == "builtin":
            col = f"{b['profile']} ({b['profile_source']})"
        rows.append([
            b["name"], b["node"], col, reads,
            ", ".join(b["writes"]), ", ".join(b["upstream"]) or "—",
        ])
    print(f"\n{C_BOLD}节点 / 变量绑定表{C_RESET}")
    print(table(["节点", "id", "模型" if runtime != "builtin" else "档位", "读取", "写入", "上游"], rows))


def make_printer():
    def on_event(event: str, payload: dict) -> None:
        if event == "start":
            tag = f"{C_YELLOW}[DRY-RUN]{C_RESET} " if payload["dry_run"] else ""
            print(f"\n{tag}{C_DIM}产物目录: {payload['run_dir']}{C_RESET}")
            print(f"{C_BOLD}开始执行{C_RESET} ({len(payload['order'])} 个节点)\n")
        elif event == "node_cached":
            print(f"{C_DIM}[{payload['idx']}/{payload['total']}] {payload['node'].name} "
                  f"· 复用已有产物，跳过{C_RESET}")
        elif event == "resume":
            print(f"{C_DIM}续跑自 {payload['from']}，复用变量: {', '.join(payload['reused']) or '无'}{C_RESET}")
        elif event == "node_start":
            node, spec = payload["node"], payload["model"]
            print(f"{C_CYAN}[{payload['idx']}/{payload['total']}]{C_RESET} "
                  f"{C_BOLD}{node.name}{C_RESET} {C_DIM}({node.id}){C_RESET} "
                  f"· {spec.model} · temp={spec.temperature}")
        elif event == "node_done":
            t = payload["trace"]
            tok = f"{t.prompt_tokens:,}→{t.completion_tokens:,} tok" if t.prompt_tokens else "no-token"
            warn = ""
            if t.finish_reason == "length":
                warn = f" {C_YELLOW}[预算打满，输出可能被截断，已用 {t.token_budget:,}]{C_RESET}"
            print(f"    {C_GREEN}✓{C_RESET} {t.elapsed_s:.1f}s · {tok} · {t.output_chars:,} 字符{warn}")
        elif event == "artifact":
            print(f"    {C_DIM}→ {payload['path']}{C_RESET}")
        elif event == "node_error":
            print(f"    {C_RED}✗ {payload['error']}{C_RESET}")
        elif event == "done":
            print(f"\n{C_GREEN}完成{C_RESET} · 总耗时 {payload['elapsed_s']:.1f}s")
    return on_event


def resolve_python_hint() -> str:
    return "python3"


def main() -> int:
    # 重定向到文件时也实时输出进度
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="量化代码自我反思流")
    ap.add_argument("--task", "-t", help="user_task：具体策略开发需求")
    ap.add_argument("--config", "-c", default=str(Path(__file__).parent / "workflow.yaml"))
    ap.add_argument("--outdir", "-o", help="产物目录（默认为 runs/<时间戳>）")
    ap.add_argument("--validate", action="store_true", help="只做静态校验，不执行")
    ap.add_argument("--dry-run", action="store_true", help="空跑全链路，不调用模型")
    ap.add_argument("--resume", "-r", help="从指定 run 目录续跑（复用已产出的变量，不重烧 token）")
    args = ap.parse_args()

    try:
        wf = Workflow.load(args.config)
    except ValidationError as exc:
        print(f"{C_RED}配置校验失败{C_RESET}")
        for p in exc.problems:
            print(f"  · {p}")
        return 2
    except WorkflowError as exc:
        print(f"{C_RED}{exc}{C_RESET}")
        return 2

    if args.validate:
        try:
            report = wf.validate()
        except ValidationError as exc:
            print(f"{C_RED}DAG / 变量绑定校验失败{C_RESET}")
            for p in exc.problems:
                print(f"  · {p}")
            return 2
        print_binding(wf, report)
        print(f"\n{C_GREEN}✓ 校验通过{C_RESET}：{len(wf.nodes)} 个节点，"
              f"{len(wf.order)} 层执行链，无循环依赖，所有变量引用可解析。")
        return 0

    task = (args.task or "").strip()
    if not task:
        if not sys.stdin.isatty():
            print(f"{C_RED}未提供 user_task。请用 --task \"...\" 传入需求。{C_RESET}")
            return 2
        print_binding(wf, wf.validate())
        print(f"\n{C_BOLD}请输入 user_task{C_RESET}（策略开发需求，输入后回车执行；Ctrl+C 退出）")
        try:
            task = input("user_task > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 130
        if not task:
            print(f"{C_RED}user_task 为空，已退出。{C_RESET}")
            return 2

    try:
        report = wf.validate(require_inputs=True, provided={"user_task": task})
    except ValidationError as exc:
        print(f"{C_RED}输入校验失败{C_RESET}")
        for p in exc.problems:
            print(f"  · {p}")
        return 2

    print_binding(wf, report)
    try:
        result = wf.run(
            inputs={"user_task": task},
            run_dir=args.outdir,
            dry_run=args.dry_run,
            on_event=make_printer(),
            resume_from=args.resume,
        )
    except WorkflowError as exc:
        run_dir = getattr(wf, "last_run_dir", None)
        print(f"\n{C_RED}执行中断{C_RESET}: {exc}")
        if run_dir:
            print(f"{C_DIM}已完成节点的产物保留在: {run_dir}{C_RESET}")
            print(f"{C_DIM}修复后从断点继续: python run.py --resume {run_dir}{C_RESET}")
        return 1

    final_code = result["final"].get("final", "")
    preview = "\n".join(final_code.splitlines()[:15])
    print(f"\n{C_BOLD}final_code 预览{C_RESET}（前 15 行，完整文件见产物目录）")
    print(f"{C_DIM}{'-' * 60}{C_RESET}")
    print(preview)
    print(f"{C_DIM}{'-' * 60}{C_RESET}")
    print(f"{C_DIM}变量与运行轨迹: {result['run_dir']}/variables.json, trace.json{C_RESET}")

    if args.dry_run:
        print(f"\n{C_YELLOW}这是 DRY-RUN 结果（占位内容），去掉 --dry-run 才是真实模型输出。{C_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
