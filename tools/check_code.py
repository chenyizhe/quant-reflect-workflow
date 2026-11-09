#!/usr/bin/env python3
"""
check_code.py — 量化产物检查器 v4（v1.9）

用法:
    python check_code.py <path> [--level syntax|import|runtime|run]

分级（逐级叠加）:
    syntax    ast 解析
    import    依赖可导入
    runtime   ★ 五探针：smoke / causal / timing(脉冲扰动) / panel / 形状契约
    run       额外跑内置契约验收套件 + 产物自身 __main__（若存在）

为什么用脉冲扰动替代"变化点求交集"（v1.8 的 timing 探针）:
    v1.8 靠 `signal` 与 `position` 变化点是否有交集判断同根成交。
    这在信号连续翻转、或停牌顺延造成 position 晚于 signal 变化时会误报/漏报。
    v1.9 改成因果测试：把第 45 根 bar 的 close 乘以 1.5 / 0.5，
    若 position[:46] 有任何变化，说明 position[t] 用到了当日 close[t] —— 精确无歧义。

输出标记: [SYNTAX] [IMPORT] [SECURITY] [CONTRACT]
          [SMOKE] [CAUSAL] [TIMING] [PANEL] [SHAPE] [SUITE] [RUNTIME] [ASSERT] [OK]
退出码: 0 通过, 1 未通过
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

TAIL = 3000

ENTRY_NAME = "run_strategy"
REQUIRED_COLS = ("signal", "position")
IMPULSE_IDX = 45          # 脉冲扰动落点（需在预热期之后、样本末尾之前）
IMPULSE_PREFIX = 46       # 比对 position[:46]，即 index 0..45

DANGEROUS_MODULES = {
    "os", "subprocess", "shutil", "socket", "pathlib", "pty",
    "multiprocessing", "ctypes", "pickle", "urllib", "http",
    "requests", "ftplib", "smtplib", "telnetlib",
}
DANGEROUS_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}


def emit(tag: str, detail: str) -> None:
    print(f"[{tag}]")
    print(detail.rstrip())
    print(f"[/{tag}]")


# ---------------------------------------------------------------- 静态层
def check_syntax(src: str, path: Path) -> ast.Module | None:
    try:
        return ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        emit("SYNTAX", f"第 {exc.lineno or 0} 行第 {exc.offset or 0} 列: {exc.msg}\n"
                       f"  >>> {(exc.text or '').rstrip()}")
        return None


def collect_imports(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {m for m in mods if m}


def check_imports(mods: set[str]) -> list[str]:
    missing = []
    for name in sorted(mods):
        if name == "__future__":
            continue
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError, ModuleNotFoundError):
            missing.append(name)
    return missing


def security_scan(tree: ast.Module) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in DANGEROUS_MODULES:
                    hits.append(f"L{node.lineno}: import {a.name}（禁止：不得访问文件/网络/进程）")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in DANGEROUS_MODULES:
                hits.append(f"L{node.lineno}: from {node.module} import ...（禁止）")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in DANGEROUS_CALLS:
                hits.append(f"L{node.lineno}: 调用 {node.func.id}()（禁止：不得读写文件或动态执行）")
    return hits


def check_entry_contract(tree: ast.Module) -> tuple[bool, str]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == ENTRY_NAME:
            args = [a for a in node.args.args if a.arg not in ("self", "cls")]
            if not args:
                return False, f"{ENTRY_NAME}() 必须接受至少一个位置参数（df），当前零参数"
            return True, f"入口 {ENTRY_NAME}({', '.join(a.arg for a in args[:3])}…) 存在"
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    return False, (
        f"缺少强制入口 {ENTRY_NAME}(df, ...) -> DataFrame[signal, position]。"
        f"当前模块级函数: {', '.join(names) or '无'}"
    )


# ---------------------------------------------------------------- 动态探针
RUNTIME_RUNNER = r'''
import importlib.util, json, sys, traceback, warnings
import numpy as np
import pandas as pd

path, entry, required, do_suite = sys.argv[1], sys.argv[2], sys.argv[3].split(","), sys.argv[4] == "1"
result = {"status": "ok", "probes": {}, "errors": []}

try:
    pd.options.mode.copy_on_write = True
except Exception:
    pass
try:
    pd.set_option("mode.chained_assignment", "raise")
except Exception:
    pass
warnings.simplefilter("error", FutureWarning)

IDX = 45
PREFIX = 46


def synth(n, seed=7, scale=1.0):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.01, n)
    if n > 41:
        ret[40] = 0.15
    if n > 71:
        ret[70] = -0.16
    close = 10.0 * scale * np.exp(np.cumsum(ret))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=n),
        "open": open_, "high": np.maximum(open_, close) * 1.004,
        "low": np.minimum(open_, close) * 0.996, "close": close,
        "volume": rng.integers(1000, 1_000_000, n).astype(float),
    })
    if n > 51:
        df.loc[50:51, ["open", "high", "low", "close"]] = df.loc[49, ["open", "high", "low", "close"]].values
        df.loc[50:51, "volume"] = 0.0
    if n > 37:
        df.loc[35:37, "volume"] = 0.0
    df.index = pd.DatetimeIndex(df["date"])
    return df


def synth_panel(n=60):
    """双标的长表面板：重复日期 + 打乱顺序 + 字符串 suspended（脏数据三连）。"""
    a = synth(n, seed=11, scale=1.0); a["symbol"] = "AAA"
    b = synth(n, seed=23, scale=1.4); b["symbol"] = "BBB"
    panel = pd.concat([a, b]).sample(frac=1.0, random_state=42)
    panel["suspended"] = "False"
    return panel, a, b


def load():
    spec = importlib.util.spec_from_file_location("cand", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cand"] = mod
    spec.loader.exec_module(mod)
    return getattr(mod, entry, None)


def norm(out):
    if not isinstance(out, pd.DataFrame):
        return None
    return {c: out[c].to_numpy(dtype="float64", copy=True) for c in out.columns
            if out[c].dtype.kind in "fiu"}


def run(fn, df, tag):
    """返回 (out_df, cols, err)。err 非空表示该次调用失败。"""
    try:
        out = fn(df)
    except Exception:
        return None, None, tag + ": " + traceback.format_exc()[-1800:]
    if not isinstance(out, pd.DataFrame):
        return None, None, f"{tag}: 返回 {type(out).__name__}，契约要求 DataFrame"
    cols = norm(out)
    if cols is None:
        return out, None, f"{tag}: 返回不可数值化的 DataFrame"
    return out, cols, None


def impulse(df, factor):
    """只扰动第 IDX 根的 close，并同步撑开 high/low 保证 OHLC 自洽。"""
    p = df.copy(deep=True)
    ci = p.columns.get_loc("close")
    new = float(p.iat[IDX, ci]) * factor
    p.iat[IDX, ci] = new
    if "high" in p.columns:
        hi = p.columns.get_loc("high")
        p.iat[IDX, hi] = max(float(p.iat[IDX, hi]), new)
    if "low" in p.columns:
        li = p.columns.get_loc("low")
        p.iat[IDX, li] = min(float(p.iat[IDX, li]), new)
    return p


def fail(probe, msg):
    result["probes"][probe] = "fail"
    result["errors"].append(f"{probe}: {msg}")


def finish():
    if any(v == "fail" for v in result["probes"].values()):
        result["status"] = "fail"
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)


try:
    fn = load()
    if not callable(fn):
        result.update(status="fail", errors=[f"入口 {entry} 不存在或不可调用"])
        finish()

    full = synth(100)

    # ---------- 1. smoke ----------
    out_full, cols, err = run(fn, full, "smoke")
    if err:
        fail("smoke", err); finish()
    result["probes"]["smoke"] = "ok"
    miss = [c for c in required if c not in cols]
    if miss:
        fail("smoke", f"返回缺列 {miss}，契约要求 {list(required)}")
        finish()

    # ---------- 2. causal（前缀不变性）----------
    _, c_trunc, err = run(fn, synth(70), "causal")
    if err:
        fail("causal", err)
    else:
        bad = []
        for c in required:
            if c not in c_trunc:
                bad.append(f"{c}: 截断输入下缺失"); continue
            a, b = cols[c][:70], c_trunc[c]
            if not np.allclose(a, b, equal_nan=True, atol=1e-9):
                n_diff = int(np.sum(~np.isclose(a, b, equal_nan=True, atol=1e-9)))
                bad.append(f"{c}: 前 70 行有 {n_diff} 处不一致")
        if bad:
            fail("causal", "前缀截断后输出改变 → 未来数据泄漏\n  " + "\n  ".join(bad)
                 + "\n  常见成因: shift(-n) / 无 rolling 的全样本统计 / bfill")
        else:
            result["probes"]["causal"] = "ok"

    # ---------- 3. timing（脉冲扰动）----------
    timing_bad = []
    for factor in (1.5, 0.5):
        _, p_cols, err = run(fn, impulse(full, factor), f"timing(×{factor})")
        if err:
            timing_bad.append(f"扰动 ×{factor} 调用失败: " + err.splitlines()[-1][:160])
            continue
        # 只对 position 做因果断言。
        # signal[t] 本来就该由 close[t] 算出，改 close 当然会改 signal——那不是前视。
        # 前视的判据只有一条：position[t] 不得依赖 close[t]。
        for c in ("position",):
            if c not in p_cols:
                timing_bad.append(f"{c}: 扰动输入下缺失"); continue
            orig = cols[c][:PREFIX]
            pert = p_cols[c][:PREFIX]
            if not np.allclose(orig, pert, equal_nan=True, atol=1e-9):
                diff = np.flatnonzero(~np.isclose(orig, pert, equal_nan=True, atol=1e-9))[:4]
                timing_bad.append(
                    f"{c}: 把第 {IDX} 根 close 乘 {factor} 后，前 {PREFIX} 行有 "
                    f"{int((~np.isclose(orig, pert, equal_nan=True, atol=1e-9)).sum())} 处变化，"
                    f"首个落点 i={int(diff[0]) if diff.size else '?'} "
                    f"（{orig[diff[0]] if diff.size else '?'} → {pert[diff[0]] if diff.size else '?'}）")
    if not timing_bad:
        # 正向确认：扰动应当让 signal[IDX] 变化，否则说明策略根本不读 close，探针无意义
        _, p_cols0, _e = run(fn, impulse(full, 1.5), "timing(sanity)")
        if p_cols0 and "signal" in p_cols0 and np.allclose(
                cols["signal"][:PREFIX], p_cols0["signal"][:PREFIX], equal_nan=True, atol=1e-9):
            result.setdefault("notes", []).append(
                f"timing: 扰动第 {IDX} 根 close 后 signal 未变化，"
                f"该策略可能不依赖 close（探针区分度下降，仅作提示）")

    if timing_bad:
        fail("timing", "扰动当日收盘价影响了当日及之前的输出 → position[t] 用到了 close[t]（同根成交/前视）\n  "
             + "\n  ".join(timing_bad)
             + f"\n  正确做法: position = target.shift(1)，t 收盘出信号、t+1 开盘才可能建仓")
    else:
        result["probes"]["timing"] = "ok"

    # ---------- 4. panel（多标的 + 脏数据契约）----------
    panel, a_alone, b_alone = synth_panel()
    out_p, pcols, err = run(fn, panel, "panel")
    if err:
        low = err.lower()
        explicit_reject = ("ValueError" in err and
                           any(k in err or k in low for k in
                               ("重复", "面板", "多标的", "symbol", "multiindex", "multi-index")))
        if explicit_reject:
            # 单标的策略显式拒绝面板输入是合法契约行为；静默算错或崩溃才不可接受
            result["probes"]["panel"] = "skip"
            result.setdefault("notes", []).append(
                "panel: 产物显式拒绝多标的面板输入（仅支持单标的），跳过一致性比对")
        else:
            fail("panel", "长表面板（含重复日期 + 乱序 + 字符串 suspended）调用失败\n" + err
                 + "\n  若策略只支持单标的，应在入口显式 raise ValueError 说明，而不是崩溃或静默算错")
    else:
        problems = []
        if len(out_p) != len(panel):
            problems.append(f"返回 {len(out_p)} 行，输入 {len(panel)} 行")
        if not np.array_equal(np.asarray(out_p.index), np.asarray(panel.index)):
            problems.append("返回的 index 顺序与输入不一致（乱序面板未按位置回填）")
        sym = np.asarray(panel["symbol"])
        for tag, sub_a, sub_b in (("AAA", a_alone, None), ("BBB", b_alone, None)):
            m = sym == tag
            if not m.any() or "position" not in pcols:
                continue
            got = pd.Series(pcols["position"][m], index=panel.index[m]).sort_index()
            _, alone_cols, aerr = run(fn, sub_a.drop(columns=["symbol"]), f"panel({tag})")
            if aerr or alone_cols is None or "position" not in alone_cols:
                problems.append(f"{tag} 单标的基线无法计算"); continue
            if not np.allclose(got.to_numpy(), alone_cols["position"], equal_nan=True, atol=1e-9):
                n_bad = int((~np.isclose(got.to_numpy(), alone_cols["position"],
                                         equal_nan=True, atol=1e-9)).sum())
                problems.append(f"{tag} 子集有 {n_bad} 行与单独运行不一致（跨标的混算）")
        # MultiIndex 形态
        mi = panel.copy(deep=True)
        mi.index = pd.MultiIndex.from_arrays(
            [np.asarray(panel.index), sym], names=["date", "symbol"])
        mi = mi.drop(columns=["symbol"])
        out_mi, mi_cols, merr = run(fn, mi, "panel(MultiIndex)")
        if merr:
            problems.append("MultiIndex 面板调用失败: " + merr.splitlines()[-1][:160])
        elif not np.allclose(out_mi["position"].to_numpy(), out_p["position"].to_numpy(),
                             equal_nan=True, atol=1e-9):
            problems.append("MultiIndex 面板结果与 symbol 列模式不一致")
        if problems:
            fail("panel", "\n  ".join(problems))
        else:
            result["probes"]["panel"] = "ok"

    # ---------- 5. shape（取值集合 / NaN / 不改入参）----------
    shape_bad = []
    sig = np.unique(cols["signal"])
    if not set(np.round(sig, 9).tolist()) <= {0.0, 1.0}:
        shape_bad.append(f"signal 取值 {sig[:5].tolist()} 不在 {{0,1}} 内")
    pos = np.unique(cols["position"])
    if not set(np.round(pos, 9).tolist()) <= {0.0, 1.0}:
        shape_bad.append(f"position 取值 {pos[:5].tolist()} 不在 {{0.0,1.0}} 内")
    if "ret" in cols and np.isnan(cols["ret"]).any():
        shape_bad.append(f"ret 含 {int(np.isnan(cols['ret']).sum())} 个 NaN")
    before = full.copy(deep=True)
    run(fn, full, "shape(immutability)")
    if not full.equals(before):
        shape_bad.append("修改了传入的 df（必须纯函数，不得原地改入参）")
    if shape_bad:
        fail("shape", "\n  ".join(shape_bad))
    else:
        result["probes"]["shape"] = "ok"

    # ---------- 6. suite（内置契约验收，仅 run 级）----------
    if do_suite:
        suite_bad = []

        # 6.1 非法 window 必须报错（若产物不接受该参数则跳过）
        try:
            fn(full, window=20)
            has_window = True
        except TypeError:
            has_window = False
        except Exception:
            has_window = False
        if has_window:
            for bad in (True, "20", 20.5, None, 0, -3):
                try:
                    fn(full, window=bad)
                    suite_bad.append(f"window={bad!r} 未报错")
                except (TypeError, ValueError):
                    pass
                except Exception as exc:
                    suite_bad.append(f"window={bad!r} 抛 {type(exc).__name__}（应为 TypeError/ValueError）")

        # 6.2 空表：返回空 DataFrame 或显式拒绝都算合格
        try:
            empty_out = fn(full.iloc[:0])
            if not isinstance(empty_out, pd.DataFrame):
                suite_bad.append("空表输入未返回 DataFrame")
            elif len(empty_out) != 0:
                suite_bad.append(f"空表输入返回 {len(empty_out)} 行")
        except ValueError:
            pass
        except Exception as exc:
            suite_bad.append(f"空表输入抛 {type(exc).__name__}: {exc}")

        # 6.3 一字板顺延：signal 首次为 1 的次日做成 open==high==low==close
        try:
            sig1 = np.flatnonzero(cols["signal"] > 0.5)
            if sig1.size:
                k = int(sig1[0]) + 1
                if k < len(full):
                    board = full.copy(deep=True)
                    cval = float(board.iat[k, board.columns.get_loc("close")])
                    for col in ("open", "high", "low"):
                        if col in board.columns:
                            board.iat[k, board.columns.get_loc(col)] = cval
                    _, b_cols, berr = run(fn, board, "suite(board)")
                    if berr:
                        suite_bad.append("一字板输入调用失败: " + berr.splitlines()[-1][:160])
                    elif float(cols["position"][k]) > 0.5 and float(b_cols["position"][k]) > 0.5:
                        suite_bad.append(
                            f"第 {k} 根为一字板（open==high==low==close）却仍建仓，未顺延")
        except Exception as exc:
            suite_bad.append(f"一字板检查异常: {type(exc).__name__}: {exc}")

        if suite_bad:
            fail("suite", "\n  ".join(suite_bad))
        else:
            result["probes"]["suite"] = "ok"

except Exception:
    result.update(status="fail", errors=["runner: " + traceback.format_exc()[-2200:]])

finish()
'''


def run_runtime_probe(path: Path, python: str, timeout: int, suite: bool) -> tuple[bool, dict]:
    with tempfile.TemporaryDirectory() as td:
        runner = Path(td) / "_probe.py"
        runner.write_text(RUNTIME_RUNNER, encoding="utf-8")
        try:
            proc = subprocess.run(
                [python, str(runner), str(path), ENTRY_NAME, ",".join(REQUIRED_COLS),
                 "1" if suite else "0"],
                capture_output=True, text=True, timeout=timeout, cwd=str(path.parent),
            )
        except subprocess.TimeoutExpired:
            return False, {"status": "fail", "probes": {},
                           "errors": [f"runtime probe 超时（>{timeout}s），疑似死循环"]}

    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, {"status": "fail", "probes": {},
                       "errors": [f"probe 输出无法解析\n{(proc.stdout + proc.stderr)[-TAIL:]}"]}
    return payload.get("status") == "ok", payload


def report_probe(payload: dict) -> bool:
    probes = payload.get("probes") or {}
    errors = payload.get("errors") or []
    labels = {"smoke": "SMOKE", "causal": "CAUSAL", "timing": "TIMING",
              "panel": "PANEL", "shape": "SHAPE", "suite": "SUITE"}

    for name, tag in labels.items():
        if probes.get(name) == "fail":
            related = [e for e in errors if e.startswith(name + ":")]
            emit(tag, "\n".join(related) or f"{name} 探针失败")

    if payload.get("status") == "ok":
        summary = " / ".join(f"{k}=ok" for k in probes) or "无探针"
        emit("OK", f"动态探针全部通过：{summary}")
        return True

    other = [e for e in errors if not any(e.startswith(n + ":") for n in labels)]
    if other:
        emit("RUNTIME", "\n".join(other))
    return False


# ---------------------------------------------------------------- 执行级
def classify_run_error(text: str) -> str:
    return "ASSERT" if "AssertionError" in text else "RUNTIME"


def check_main(python: str, path: Path, timeout: int) -> bool:
    """产物自带 __main__ 就执行；没有则跳过（契约验收已由 suite 探针覆盖）。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    if "__main__" not in src:
        return True
    try:
        proc = subprocess.run([python, str(path)], capture_output=True, text=True,
                              timeout=timeout, cwd=str(path.parent))
    except subprocess.TimeoutExpired:
        emit("RUNTIME", f"执行超时（>{timeout}s）")
        return False
    out = (proc.stdout or "").rstrip()
    combined = f"{out}\n{(proc.stderr or '').rstrip()}".strip()
    if proc.returncode == 0:
        emit("OK", "产物 __main__ 通过" + (f"，输出末段:\n{out[-TAIL:]}" if out else ""))
        return True
    emit(classify_run_error(combined), f"退出码 {proc.returncode}\n{combined[-TAIL:]}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--level", choices=("syntax", "import", "runtime", "run"), default="runtime")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--probe-timeout", type=int, default=90)
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        emit("RUNTIME", f"产物不存在: {path}")
        return 1
    if path.stat().st_size == 0:
        emit("RUNTIME", f"产物为空文件: {path}")
        return 1

    src = path.read_text(encoding="utf-8", errors="replace")
    tree = check_syntax(src, path)
    if tree is None:
        return 1
    if args.level == "syntax":
        emit("OK", "语法解析通过")
        return 0

    mods = collect_imports(tree)
    missing = check_imports(mods)
    if missing:
        emit("IMPORT", "以下模块无法导入: " + ", ".join(missing)
             + "\n修复：改用已安装的等价库或内联实现，禁止 try/except 静默跳过")
        return 1
    if args.level == "import":
        emit("OK", f"语法与依赖通过（{len(mods)} 个模块）")
        return 0

    danger = security_scan(tree)
    if danger:
        emit("SECURITY", "\n".join(danger[:15])
             + "\n策略产物只做计算，不得读写文件、访问网络或执行子进程")
        return 1

    ok_entry, msg = check_entry_contract(tree)
    if not ok_entry:
        emit("CONTRACT", msg)
        return 1

    suite = args.level == "run"
    ok, payload = run_runtime_probe(path, args.python, args.probe_timeout, suite)
    if not report_probe(payload):
        return 1
    if args.level == "runtime":
        return 0

    return 0 if check_main(args.python, path, args.timeout) else 1


if __name__ == "__main__":
    sys.exit(main())
