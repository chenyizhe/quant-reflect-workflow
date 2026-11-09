# 节点任务书 · 重构修复者 (refiner) · 返修第 1 轮

- run: `cmpR`
- 模型档位: **lite**（来源: models.fast，调用子代理时必须传 model="lite"）
- 触发原因: 自检未通过（退出码 1，命令 `/Users/chenyizhe/.workbuddy/binaries/python/envs/default/bin/python /Volumes/plugHD/workbuddy/WorkBuddy/2026-10-21-15-17-31/quant-reflect-workflow/tools/check_code.py /private/tmp/cmpR/final_code.py --level run --timeout 240`）

## 系统指令

你是一个负责修复 Bug 的高级量化开发人员。

只输出一个 ```python 代码块。块外不允许出现任何 Markdown 表格、标题或叙述文字——
builtin 模式会把产物原样写入 .py 文件，块外内容会直接变成语法错误。

## 代码顶部 docstring 里写审查意见对照表

格式（三列，逐条不遗漏）：
编号 | 处理方式 | 位置与说明

处理方式只允许三种，不接受其他写法：
- 已修复
- 不适用（必须给量化理由，例如"该策略不持仓过夜，隔夜跳空不产生收益"）
- 无法修复（必须写明触发条件与残余影响）

## 自检入口精简

`if __name__ == "__main__":` 里**最多 25 行**，只做一件事：
调用 run_strategy 跑一遍内置的几十行合成数据，打印 returncode 与结果形状。

完整的契约验收（signal 取值、不改入参、空表、一字板顺延、面板一致性）
已由 `check_code --level run` 的 suite 探针承担，**不要重复手写 300 行断言**。
这是硬约束：多写的断言既拖慢生成，又容易和内置套件冲突。

## 输出预算

代码总量控制在 250~350 行。超过 400 行说明你在重复实现工具函数或堆砌测试。


## 用户输入（原始，未变）

## 契约（唯一真值基准，逐条落实）
# 契约：20 日动量策略（A 股 T+1）

## 1. 数据契约

| 列名 | dtype | 可空 | 说明 |
|---|---|---|---|
| date | datetime64[ns] | 否 | 交易日索引，唯一、升序 |
| open | float64 | 否 | 执行价基准 |
| high | float64 | 否 | 仅参与涨跌停判定 |
| low | float64 | 否 | 同上 |
| close | float64 | 否 | 动量计算基准 |
| volume | float64 | 是 | 成交量，用于停牌判定 |

- 频率：日线 bar，每行一个交易日；`date` 无时区（本地 naive），不允许重复日期。
- 复权口径：**UNKNOWN** — 输入须为同一口径的连续价格序列；若含分红/拆股跳空则 20 日动量失真，列入风险 [待确认]。
- 停牌表示：当日缺行、或 OHLC 全 NaN、或 volume==0，三者任一即视为停牌 [默认]。

## 2. 信号时序契约

- 信号截面：窗口 = [t-window, t] 共 window+1 个收盘价，全部 ≤ t 日收盘，**不含 t+1 任何数据**，无前视。
- 计算时点：t 日收盘后。
- 执行时点：**t+1 开盘**。t 日收盘计算、t 日不可成交（A 股 T+1，且 t 日收盘价已知时已无法以该价成交）。
- 收益口径：分段计算 = 隔夜段（t 收盘 → t+1 开盘）+ 日内段（t+1 开盘 → t+1 收盘）；建仓日收益自 t+1 开盘价起算。
- T+1 约束：t+1 买入的份额最早 t+2 日方可卖出，故持仓最短 1 个交易日。

## 3. 指标契约

- 公式：`mom_t = close_t / close_{t-window} - 1`，`window = 20`。
- 预热期：20 个 bar；首个有效动量落于第 21 根 bar（0-based index 20）。前 20 根 `mom = NaN`。
- 边界口径：
  - 全涨（mom > 0）→ signal = 1
  - 全跌（mom < 0）→ signal = 0
  - 零波动（mom == 0，含 close_{t-20} == close_t）→ signal = 0（“动量为正才持有”，零不算正）[默认]
  - `close_{t-window}` 为 0 或 NaN → mom = NaN，signal 沿用上一有效值。
- 缺失处理：`ffill_limit_window_then_drop` — 中间 NaN 在 20 个 bar 内前值填充，超出则该段信号作废、不产生调仓指令。

## 4. 交易契约

- 仓位：0/1 全仓，单标的，无杠杆无做空。`position ∈ {0, 1}`。
- 调仓触发：仅当目标仓位 ≠ 当前仓位时下单，避免同向重复交易。
- 可成交性：t+1 停牌 → 不可成交，信号顺延至下一个可交易日 [默认]；t+1 开盘一字涨停不可买入、一字跌停不可卖出，顺延 [默认]；顺延期间信号保持原方向，不顺延过期作废。
- 成本（均在成交价上即时扣减）：
  - 佣金 0.03%，买卖双向各收，单笔最低 5 元 [默认]
  - 印花税 0.05%，仅卖出单边 [默认]
  - 滑点 0.1%，单边：买入价 = `open×(1+0.001)`，卖出价 = `open×(1-0.001)`
- 成本不进入 signal/position 计算，仅用于净值评估。

## 5. 输出契约

- 返回 `pd.DataFrame`；index 与入参 `df.index` **完全一致**（保留调用方 index，行数不变，不因预热期截断）。
- 必须列：

| 列名 | dtype | 取值 |
|---|---|---|
| signal | float64 | {0.0, 1.0}，预热期为 NaN |
| position | int8 | {0, 1}，预热期为 0 |

- 允许附加列：`mom`（float64），非必需。
- 接口：`run_strategy(df, window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005) -> DataFrame[signal, position]`
- 非法值：`window < 1` 或非整数 → `ValueError`；`df` 为空或缺少必需列 → `ValueError`；成本参数为负 → `ValueError`。不静默降级、不返回全 0 兜底。

```json
{
  "signal_timing": "t_close",
  "execution_price": "t1_open",
  "warmup_period": 20,
  "nan_policy": "ffill_limit_window_then_drop",
  "entry_signature": "run_strategy(df, window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005) -> DataFrame[signal, position]",
  "assumed_defaults": [
    "动量口径 close_t/close_{t-20}-1，mom==0 判为空仓",
    "印花税 0.05% 仅卖出单边，佣金最低 5 元",
    "停牌/一字涨跌停不可成交时信号顺延至下一可交易日",
    "当日缺行或 OHLC 全 NaN 视为停牌；输入复权口径 UNKNOWN",
    "输出保留原 index 与行数，预热 20 bar 内 signal=NaN、position=0"
  ]
}
```

## 初稿代码
"""20 日动量策略（A 股 T+1）· 候选 c1 · 范式 A：向量化优先。

入口契约（强制，不满足直接判不通过）：
    run_strategy(df, **params) -> pd.DataFrame
    必须列：
        signal   int8      ∈ {0, 1}
        position float64   ∈ {0.0, 1.0}
    附加列：mom float64（契约允许）
    position 是 signal 的滞后：t 日收盘出信号，t+1 才可能建仓。
    预热期内 signal / position 均为 0。

口径裁决（契约表格与本文件头部契约冲突时，以强制入口契约为准）：
  * dtype：signal 采用 int8（{0,1}）、position 采用 float64（{0.0,1.0}）；
  * 预热 20 bar：signal=0、position=0.0（int8 无法承载 NaN）；
  * 空表（列齐全）→ 返回同结构空 DataFrame，不 raise（防坑算子 6）；
    缺少核心列 open/close → 仍按契约 §5 抛 ValueError；
  * high/low/volume 缺失时：一字板项 / 成交量停牌项不生效，不伪造数据（算子 3）；
  * 复权口径 UNKNOWN：不做复权推断，仅要求输入为同口径连续序列 [待确认]。

范式 A 约束：组内一切运算用 pandas/numpy 原生向量化，无 Python 逐 bar 循环
（仅保留「按标的遍历分组」这一层，属算子 1 要求的必要分组骨架）。

运行时硬约束自检：
  - 不 import os / subprocess / shutil / socket / pathlib / requests
  - 不调用 open / eval / exec / compile
  - 无链式赋值；to_numpy() 一律 copy=True 后使用，不原地改只读视图
  - 除法前用 mask 剔除 0 / 非正值，log 未使用
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 一字板判定的相对容差
_TOL = 1e-12

# 核心必需列（缺失即 ValueError）；high / low / volume 允许缺失并降级
_CORE_COLS = ("open", "close")


# --------------------------------------------------------------------------- #
# 算子 2：停牌列安全布尔解析（防字符串 "False" 被 astype(bool) 判为 True）
# --------------------------------------------------------------------------- #
def _to_bool(s):
    if s.dtype == bool:
        return s.to_numpy()
    txt = s.astype("string").str.strip().str.lower()
    return txt.isin(["true", "1", "1.0", "t", "y", "yes"]).to_numpy()


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(s):
    """转 float64 ndarray，非数值 → NaN；copy=True（算子 5）。"""
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64", copy=True)


def _eq(a, b):
    """带相对容差的近似相等（算子 3）。"""
    scale = np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
    return np.abs(a - b) <= _TOL * scale


def _flat_board(openp, high, low, close):
    """一字板：high == low 且 open == high 且 close == high（算子 3）。

    三者同时成立时，无论涨停还是跌停，当日均不可成交：
    涨停买不进、跌停卖不出，统一按「不可成交 → 顺延」处理。
    无 high/low 时由调用方传入 None，本函数返回全 False（不伪造）。
    """
    if high is None or low is None:
        return np.zeros(openp.shape[0], dtype=bool)
    ok = np.isfinite(openp) & np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
    flat = ok & _eq(high, low) & _eq(openp, high) & _eq(close, high)
    return flat & ok


def _empty_result(df):
    """算子 6：空表返回列名与 dtype 齐全的空 DataFrame。"""
    n = len(df)
    return pd.DataFrame(
        {
            "signal": pd.Series(np.zeros(n, dtype=np.int8), index=df.index),
            "position": pd.Series(np.zeros(n, dtype=np.float64), index=df.index),
            "mom": pd.Series(np.full(n, np.nan, dtype=np.float64), index=df.index),
        },
        index=df.index,
    )


# --------------------------------------------------------------------------- #
# 分组键 / 日期键（算子 1 前置）
# --------------------------------------------------------------------------- #
def _group_key(df):
    """提取分组键。返回 None 表示单标的（整表同组）。

    优先级：symbol 列 > MultiIndex 中首个非日期层。
    """
    if "symbol" in df.columns:
        return np.asarray(df["symbol"].to_numpy(copy=True))

    if isinstance(df.index, pd.MultiIndex) and df.index.nlevels >= 2:
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if not isinstance(lev, pd.DatetimeIndex):
                return np.asarray(lev.to_numpy(copy=True))
    return None


def _date_key(df):
    """提取用于组内稳定排序的日期键；取不到则返回 None（视为已排序）。"""
    if "date" in df.columns:
        vals = df["date"]
    elif isinstance(df.index, pd.DatetimeIndex):
        vals = df.index
    elif isinstance(df.index, pd.MultiIndex):
        vals = None
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if isinstance(lev, pd.DatetimeIndex):
                vals = lev
                break
        if vals is None:
            return None
    elif isinstance(df.index, pd.RangeIndex):
        return None
    else:
        try:
            return pd.to_datetime(pd.Series(np.asarray(df.index)), errors="coerce").to_numpy()
        except (TypeError, ValueError):
            return None

    try:
        return pd.to_datetime(pd.Series(np.asarray(vals)), errors="coerce").to_numpy()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 单标的计算：全向量化
# --------------------------------------------------------------------------- #
def _compute_one(sub, window):
    """返回 (signal int8, position float64, mom float64)，顺序与 sub 行序一致。"""
    n = len(sub)

    close = _num(sub["close"])
    openp = _num(sub["open"])
    high = _num(sub["high"]) if "high" in sub.columns else None
    low = _num(sub["low"]) if "low" in sub.columns else None
    vol = _num(sub["volume"]) if "volume" in sub.columns else None

    # ---- 停牌判定：OHLC 全 NaN，或 volume == 0 ----
    all_nan = (
        ~np.isfinite(openp)
        & ~np.isfinite(close)
        & (high is None or ~np.isfinite(high))
        & (low is None or ~np.isfinite(low))
    )
    if vol is None:
        suspended = all_nan
    else:
        suspended = all_nan | (np.isfinite(vol) & (vol == 0.0))

    # ---- 算子 4：估值价连续性 ----
    # 收盘价先做 ffill（限 window 根），否则停牌日 NaN 会把整条 mom 序列撕成 NaN。
    # 契约 nan_policy = ffill_limit_window_then_drop：超限的 NaN 段信号作废。
    close_ff = pd.Series(close, dtype="float64").ffill(limit=window)

    # ---- 20 日动量：close_t / close_{t-window} - 1，分母做零值保护 ----
    base = close_ff.shift(window)
    base_safe = base.mask(base <= 0.0)          # 0 / 负值 / NaN → NaN，防除零
    mom_s = close_ff.div(base_safe).sub(1.0)
    mom = mom_s.to_numpy(dtype="float64", copy=True)

    # ---- 信号：mom > 0 → 1；mom <= 0（零不算正）→ 0；NaN 暂留待前填 ----
    pos_mask = (mom_s > 0.0).to_numpy()
    valid = mom_s.notna().to_numpy()
    sig_s = pd.Series(np.where(valid, np.where(pos_mask, 1.0, 0.0), np.nan), dtype="float64")
    # 缺失处理：20 根内前值填充沿用上一有效信号
    sig_s = sig_s.ffill(limit=window)
    # 仍为 NaN（预热期 / 超限作废段）→ 0，即不产生建仓指令
    target = sig_s.fillna(0.0).clip(0.0, 1.0).to_numpy(dtype="float64", copy=True)

    # ---- 执行：t 收盘出信号，t+1 开盘成交 → target 整体滞后 1 期 ----
    # A 股 T+1 由该滞后天然满足：t+1 买入的份额最早 t+2 才可能卖出，持仓最短 1 个交易日。
    target_prev = pd.Series(target, dtype="float64").shift(1)

    # 不可成交日：停牌 或 一字板（买不进 / 卖不出）→ 顺延至下一可成交日
    blocked = pd.Series(suspended | _flat_board(openp, high, low, close), dtype="bool")

    # 只在可成交日写入「昨日目标仓位」，不可成交日留 NaN
    exec_sig = target_prev.where(~blocked.to_numpy())
    # ffill 等价于「顺延期间仓位保持原方向、不顺延过期作废」：
    # 最后一个被成功执行的制度即为当前持仓。
    position = exec_sig.ffill().fillna(0.0).clip(0.0, 1.0)

    # signal 取「已前填」的 target：mom 为 NaN 时沿用上一有效值（契约 §3）
    # 预热期 / 超限作废段已在 target 中收敛为 0，故 signal 与 position 天然同源。
    warmup = np.arange(n) < window
    signal_out = np.where(warmup, 0, target).astype(np.int8, copy=True)
    position_out = position.to_numpy(dtype="float64", copy=True)
    position_out = np.where(warmup, 0.0, position_out)

    return signal_out, position_out, mom


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def _check_args(df, window, commission, slippage, stamp_tax):
    if not isinstance(df, pd.DataFrame):
        raise ValueError("df 必须是 pandas.DataFrame")
    missing = [c for c in _CORE_COLS if c not in df.columns]
    if missing:
        raise ValueError("df 缺少必需列: %s" % ",".join(missing))
    if isinstance(window, bool) or not isinstance(window, (int, np.integer)):
        raise ValueError("window 必须为正整数")
    if int(window) < 1:
        raise ValueError("window 必须 >= 1")
    for name, val in (("commission", commission), ("slippage", slippage), ("stamp_tax", stamp_tax)):
        try:
            fv = float(val)
        except (TypeError, ValueError):
            raise ValueError("%s 必须为数值" % name)
        if not np.isfinite(fv) or fv < 0.0:
            raise ValueError("%s 不能为负" % name)
    return int(window)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_strategy(df, window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005):
    """20 日动量：mom > 0 满仓、mom <= 0 空仓；A 股 T+1，停牌/一字板顺延。

    参数 commission / slippage / stamp_tax 不进入 signal / position 计算，
    仅按契约 §4 供下游净值评估使用（本函数不做净值回测）。
    """
    window = _check_args(df, window, commission, slippage, stamp_tax)

    n = len(df)
    if n == 0:
        return _empty_result(df)

    sig_out = np.zeros(n, dtype=np.int8)
    pos_out = np.zeros(n, dtype=np.float64)
    mom_out = np.full(n, np.nan, dtype=np.float64)

    # ---- 算子 1：面板分组与原序回填 ----
    keys = _group_key(df)
    dates = _date_key(df)

    if keys is None:
        gid = np.zeros(n, dtype=np.int64)
        ngroups = 1
    else:
        codes, uniques = pd.factorize(pd.Series(np.asarray(keys)), sort=False)
        gid = np.asarray(codes, dtype=np.int64)
        ngroups = len(uniques)

    for k in range(ngroups):
        # 1) 分组键 gid == k → 该标的在原始表中的绝对行位置
        idx = np.flatnonzero(gid == k)
        if idx.size == 0:
            continue

        # 组内按日期稳定排序（重复日期保持原始相对顺序）
        if dates is None:
            order = np.arange(idx.size, dtype=np.int64)
        else:
            order = np.argsort(dates[idx], kind="mergesort")

        take = idx[order]                    # 组内日期升序后的绝对行号，供 iloc 取子表
        inv = np.empty(order.size, dtype=np.int64)
        inv[order] = np.arange(order.size, dtype=np.int64)
        # inv[组内原始位 i] = 该行在排序后结果数组中的位置（order 的逆）

        # 2) iloc 取子表：长度严格等于 idx，算完的结果与 take 一一对应
        sub = df.iloc[take]
        s_sorted, p_sorted, m_sorted = _compute_one(sub, window)

        # 3) 按逆置换还原到组内原始行序，再 scatter 回预分配的全表数组
        #    方向必须正确：sorted[j] 属于组内原始位 order[j]，
        #    故 out[idx[i]] 取 sorted[inv[i]]（等价于 out[take] = sorted）。
        #    写成 g[inv] = sorted 会得到逆置换（即 out 被 order 而非 order^{-1} 搬运），
        #    乱序面板下就是跨标的/跨行错位。
        sig_out[idx] = s_sorted[inv]
        pos_out[idx] = p_sorted[inv]
        mom_out[idx] = m_sorted[inv]

    # ---- 输出：index 与行数与入参完全一致（不因预热期截断） ----
    out = pd.DataFrame(index=df.index)
    out["signal"] = pd.Series(sig_out, index=df.index, dtype="int8")
    out["position"] = pd.Series(pos_out, index=df.index, dtype="float64")
    out["mom"] = pd.Series(mom_out, index=df.index, dtype="float64")
    return out

## 审查结果（JSON，逐条落实 actionable_fixes）
```json
{
  "summary": "运行时探针（smoke/causal/timing/panel/shape）全绿只说明不崩、不乱序、不前视，但 dtype/预热语义、空表兜底、超限 NaN 处置、一字板方向性与成本落地这五项契约条款它一条都没覆盖——代码用 L129-131 的注释单方面改写了唯一真值基准，判定阻断。",
  "verdict": "block",
  "actionable_fixes": [
    {
      "id": "P01",
      "severity": "阻断",
      "target_function": "run_strategy",
      "line_hint": "L412",
      "root_cause": "返回契约的 dtype 被整体对调：L412 `out['signal'] = pd.Series(sig_out, index=df.index, dtype='int8')` 输出 int8，契约 §5 要求 float64；L413 `out['position'] = pd.Series(pos_out, ..., dtype='float64')` 输出 float64，契约要求 int8。同时 L319 `signal_out = np.where(warmup, 0, target).astype(np.int8, copy=True)` 把预热 20 根的 signal 由契约要求的 NaN 硬压成 0（int8 无 NaN 位，L131 注释亦自认），L364 的 `sig_out = np.zeros(n, dtype='int8')` 与 L202 `_empty_result` 同病。根因是 L129-131 的「口径裁决：契约表格与本文件头部契约冲突时，以强制入口契约为准」——用代码注释单方面改写唯一真值基准。运行时 shape 探针只比对行数/index，不比对 dtype，故全绿掩盖：下游 `out['signal'].isna().sum()` 恒为 0、`out['position'].astype('int8')` 静默通过，预热 20 根被当成 20 个「有效的空仓信号」进入 IC、换手与胜率统计，把预热期稀释成真实观测。",
      "exact_code_change": "# 1) _compute_one 末尾 L318-321 整段替换：\nwarmup = np.arange(n) < window\nsignal_out = np.where(warmup, np.nan, target).astype('float64', copy=True)\nposition_out = np.where(warmup, 0, position.to_numpy(dtype='float64', copy=True)).astype('int8', copy=True)\n\n# 2) run_strategy L364-366 预分配替换：\nsig_out = np.full(n, np.nan, dtype=np.float64)\npos_out = np.zeros(n, dtype=np.int8)\nmom_out = np.full(n, np.nan, dtype=np.float64)\n\n# 3) run_strategy L412-413 替换：\nout['signal'] = pd.Series(sig_out, index=df.index, dtype='float64')\nout['position'] = pd.Series(pos_out, index=df.index, dtype='int8')\n\n# 4) _empty_result L202-203 替换：\n'signal': pd.Series(np.full(n, np.nan, dtype=np.float64), index=df.index),\n'position': pd.Series(np.zeros(n, dtype=np.int8), index=df.index),\n\n# 5) 删除 L129-131「口径裁决」三条 bullet，改写为：\"输出结构严格遵循契约 §5，本文件不作任何口径改写\"。"
    },
    {
      "id": "P02",
      "severity": "高危",
      "target_function": "run_strategy",
      "line_hint": "L361",
      "root_cause": "L361-362 `if n == 0: return _empty_result(df)` 对空表静默返回同结构空 DataFrame，L132 注释明写「空表（列齐全）→ 返回同结构空 DataFrame，不 raise（防坑算子 6）」。契约 §5 第 98 行明文规定「df 为空或缺少必需列 → ValueError」，且追加「不静默降级、不返回全 0 兜底」。这是把上游数据断流（行情接口返回 0 行）伪装成「策略今天没有信号」：调用方拿到 0 行结果后按 `signal.mean()`、`position.sum()` 汇总，会得到 0 而非异常，整段回测静默变成空跑，比崩溃更难排查。缺列分支（L332-334）确实 raise 了，同一函数内两种非法态两套标准。",
      "exact_code_change": "L361-362 替换为：\nif n == 0:\n    raise ValueError('df 不能为空')\n\n# 同时删除 L132 的「空表（列齐全）→ 返回同结构空 DataFrame，不 raise」条目；\n# _empty_result 仅保留给内部构造使用，不再从 run_strategy 的空表分支调用。"
    },
    {
      "id": "P03",
      "severity": "高危",
      "target_function": "run_strategy",
      "line_hint": "L301",
      "root_cause": "缺失处理被写成「超限即清仓」。L299 `sig_s = sig_s.ffill(limit=window)` 只前填 20 根，L301 `target = sig_s.fillna(0.0).clip(0.0, 1.0)...` 把剩余 NaN 一律收敛为 0。契约 §3 line 73 的 nan_policy 是 `ffill_limit_window_then_drop`，语义为「超出则该段信号作废、不产生调仓指令」——作废是保持原仓位，不是发一条清仓单。反例：某标的停牌 25 个交易日，第 21 天起 close 链断，mom 整段 NaN，L301 将 target 打成 0，再经 L305 `target_prev = shift(1)` 传到 L314 `position = exec_sig.ffill()`，第 22 个 bar 起 position 由 1 掉到 0——以「信号作废」为名在无量可成交的停牌段强制平仓，契约要求的「不顺延过期作废」被反向执行为「过期强制离场」。注意 L301 的 fillna(0.0) 无法区分「前置预热 NaN」与「中段作废 NaN」，二者被同一个 0 抹平。",
      "exact_code_change": "L294-301 整段替换为：\npos_mask = (mom_s > 0.0).to_numpy()\nvalid = mom_s.notna().to_numpy()\nsig_s = pd.Series(np.where(valid, np.where(pos_mask, 1.0, 0.0), np.nan), index=sub.index, dtype='float64')\nsig_s = sig_s.ffill()                      # 去掉 limit=window：作废段「不产生调仓指令」= 沿用上一有效信号\ntarget = sig_s.fillna(0.0).clip(0.0, 1.0).to_numpy(dtype='float64', copy=True)   # fillna 仅收敛前置预热 NaN\n\n# 同步把 L285 注释改为：超限 NaN 段 target 冻结于上一有效值，position 保持不变，不发调仓单。"
    },
    {
      "id": "P04",
      "severity": "高危",
      "target_function": "run_strategy",
      "line_hint": "L193",
      "root_cause": "一字板判定既没有涨跌幅方向，也没有幅度门限。L193 `flat = ok & _eq(high, low) & _eq(openp, high) & _eq(close, high)` 只判断四价合一（容差 _TOL=1e-12，L153），任何 high==low==open==close 的 bar 都被判为不可成交——包括全天横盘、零振幅、成交正常的票，这类 bar 在低流动性小票上并不罕见，全部被误判为停牌顺延。更严重的是无方向：L308 `blocked = pd.Series(suspended | _flat_board(openp, high, low, close), dtype='bool')` 把涨停与跌停合并成同一个 blocked 掩码，再交给 L311 `exec_sig = target_prev.where(~blocked.to_numpy())` 统一置 NaN。契约 §4 line 79 写的是「一字涨停不可买入、一字跌停不可卖出」，方向不对称：一字涨停当天卖单完全可成交（且是流动性最好的卖出窗口），一字跌停当天买单可成交。当前实现把所有涨停日的卖出（position 1→0）一并冻结为 1，L314 的 ffill 让这段本应离场的仓位继续吃满后续下跌；对称地，跌停日的买入被冻结为 0，错过跌停打开后的反弹建仓。两端偏差方向一致，都在美化回测。",
      "exact_code_change": "# 1) L183-194 的 _flat_board 整体替换为带方向与幅度门限的版本：\ndef _board_dir(openp, high, low, close, prev_close):\n    if high is None or low is None:\n        z = np.zeros(openp.shape[0], dtype=bool)\n        return z, z\n    ok = (np.isfinite(openp) & np.isfinite(high) & np.isfinite(low)\n          & np.isfinite(close) & np.isfinite(prev_close) & (prev_close > 0.0))\n    flat = ok & _eq(high, low) & _eq(openp, high) & _eq(close, high)\n    chg = np.where(ok, close / np.where(prev_close > 0.0, prev_close, np.nan) - 1.0, np.nan)\n    limit_up = flat & (chg >= 0.095)      # 主板 10% 一字涨停，留 0.5% 容差吸收四舍五入\n    limit_down = flat & (chg <= -0.095)\n    return limit_up, limit_down\n\n# 2) L305-314 替换为方向敏感的执行链：\ntarget_prev = pd.Series(target, dtype='float64').shift(1)\nprev_close = pd.Series(close, dtype='float64').ffill().shift(1).to_numpy(dtype='float64', copy=True)\nblock_buy, block_sell = _board_dir(openp, high, low, close, prev_close)\nprev_pos = pd.Series(target_prev, dtype='float64').ffill().fillna(0.0)\nno_fill = (pd.Series(suspended, dtype='bool')\n           | pd.Series((target_prev.to_numpy() > prev_pos.to_numpy()) & block_buy, dtype='bool')\n           | pd.Series((target_prev.to_numpy() < prev_pos.to_numpy()) & block_sell, dtype='bool'))\nexec_sig = target_prev.where(~no_fill.to_numpy())\nposition = exec_sig.ffill().fillna(0.0).clip(0.0, 1.0)"
    },
    {
      "id": "P05",
      "severity": "中危",
      "target_function": "run_strategy",
      "line_hint": "L356",
      "root_cause": "三个成本参数是空转的死参数。L352 签名接收 commission/slippage/stamp_tax，L339-345 只做非负校验，此后全函数无任何引用点：L311-L314 的 position 链不含 cost，L411-415 的输出只有 signal/position/mom，没有任何净值、成交价或成本列。L355-356 docstring 以「本函数不做净值回测」单方面把契约 §4 的成本条款移出交付范围。后果是 §4 明列的滑点 0.1% 单边、佣金万三双向、印花税千五卖出——每轮全仓进出约 0.26% 的摩擦——在交付物里没有任何落点，下游若直接拿 signal 当权重乘 close 收益率，20 日动量的高换手会被系统性地算成零成本，策略看似有效实为成本幻觉。参数被接收却不被使用，比不接收更具误导性：调用方看到签名会以为成本已计入。",
      "exact_code_change": "# 在 L314 之后、L318 之前插入成本计算，并在 L414 后追加附加列：\npos_arr = position.to_numpy(dtype='float64', copy=True)\nopenp_raw = _num(sub['open'])\ntrade = np.zeros(n, dtype=bool)\ntrade[1:] = np.nan_to_num(np.abs(np.diff(pos_arr))) > 0.0\nbuy = np.zeros(n, dtype=bool)\nbuy[1:] = (np.diff(pos_arr) > 0.0)\nfill_px = np.where(buy, openp_raw * (1.0 + slippage), openp_raw * (1.0 - slippage))\ncost_bps = np.where(trade,\n                    commission + np.where(buy, 0.0, stamp_tax) + slippage,\n                    0.0)\n\n# 返回 _compute_one 增加两个数组（c_ 前缀），run_strategy 预分配并 scatter：\n#   cost_out = np.zeros(n, dtype=np.float64)   ->  out['cost'] = pd.Series(cost_out, index=df.index, dtype='float64')\n#   px_out   = np.full(n, np.nan, dtype=np.float64) -> out['trade_price'] = pd.Series(px_out, index=df.index, dtype='float64')\n# 成本按「成交价上即时扣减」语义记入 cost 列（单位：占成交额比例），供下游净值 = cumprod(1+r) * cumprod(1-cost) 直接使用；\n# 删除 L355-356 中「本函数不做净值回测」的免责句，改为「成本以 cost / trade_price 附加列输出，不进入 signal/position」。"
    },
    {
      "id": "P06",
      "severity": "中危",
      "target_function": "run_strategy",
      "line_hint": "L376",
      "root_cause": "NaN 分组键被 factorize 编成 -1 后整组静默丢失。L376 `codes, uniques = pd.factorize(pd.Series(np.asarray(keys)), sort=False)` 对 symbol 为 NaN/None 的行返回码 -1，L380 `for k in range(ngroups)` 的 k 从 0 起，永远取不到 -1，这些行从未进入 _compute_one，于是保留 L364-366 的初值：signal=0、position=0.0、mom=NaN。既不报错也不计数。多标的面板里但凡拼接时有一列 symbol 缺失（multiindex 降维、concat 后未 reindex 都会产生），该标的整段行情被当作「全程空仓」参与统计，回测标的数与实际不符，且因为 signal 全 0 不贡献任何收益，只稀释分母。这是典型的静默降级：数据缺陷被转译成一个合法的策略结论。",
      "exact_code_change": "L369-378 替换为：\nkeys = _group_key(df)\ndates = _date_key(df)\n\nif keys is None:\n    gid = np.zeros(n, dtype=np.int64)\n    ngroups = 1\nelse:\n    key_s = pd.Series(np.asarray(keys), dtype='object')\n    key_s = key_s.where(key_s.notna(), '<NA_SYMBOL>')\n    codes, uniques = pd.factorize(key_s, sort=False)\n    gid = np.asarray(codes, dtype=np.int64)\n    ngroups = len(uniques)\n    if (gid < 0).any():\n        raise ValueError('分组键存在无法识别的取值，拒绝静默丢弃')"
    },
    {
      "id": "P07",
      "severity": "中危",
      "target_function": "run_strategy",
      "line_hint": "L281",
      "root_cause": "停牌判定要求 OHLC 全 NaN，导致「open 为 NaN 但 close 有值」的半残 bar 被当成可成交。L272-277 的 `all_nan` 用 `&` 串起四个非有限判断，只要 close 有值就整体为 False；L281 `suspended = all_nan | (np.isfinite(vol) & (vol == 0.0))` 也不覆盖 open 缺失；L308 的 blocked 同样只否掉 all_nan 行。于是这类 bar 进入 L311 `exec_sig = target_prev.where(~blocked)`，position 被置为 1，而该 bar 的 open 是 NaN——契约 §1 说 open 是「执行价基准」，执行价 NaN 意味着这笔仓位永远无法定价。本函数不产出收益列所以当下不崩，但 P05 一旦补上 trade_price/net_value，NaN 会立刻沿净值链路传染成整段 NaN（一次 NaN 乘 cumprod 即污染到底），且无抛错、无告警，与范例 1 的失效形态同构。停牌三条件（缺行/OHLC 全 NaN/volume==0）里，「缺行」在传入前已被上游补齐成 NaN 行，正是这种 open 缺失的形态。",
      "exact_code_change": "L278-281 替换为：\nif vol is None:\n    suspended = all_nan | ~np.isfinite(openp) | ~np.isfinite(close)\nelse:\n    suspended = (all_nan | ~np.isfinite(openp) | ~np.isfinite(close)\n                 | (np.isfinite(vol) & (vol == 0.0)))\n\n# 说明：执行价基准 open 非有限即不可定价、不可成交，按停牌处理并顺延；\n# close 非有限同样不能作为 mom 输入，已在 L286 的 ffill 前被冻结，此处一并归入 suspended 以保证口径一致。"
    },
    {
      "id": "P08",
      "severity": "低危",
      "target_function": "run_strategy",
      "line_hint": "L390",
      "root_cause": "契约 §1 line 52 要求 date「唯一、升序」，代码对唯一性零校验且对重复日期无处置。L386-390 `order = np.argsort(dates[idx], kind='mergesort')` 只做稳定排序，重复日期保持原相对顺序，两条同日 bar 会被当成两个连续交易日喂进 _compute_one：L289 `base = close_ff.shift(window)` 的 20 日窗口因此跨过两天而非一个自然交易日，mom 分母取到 t-19 而非 t-20，动量值系统性偏小；L305 `shift(1)` 的 T+1 滞后也变成「同日滞后」。L390 的注释只交代「重复日期保持原始相对顺序」，把契约违约降级成实现细节。行情源复权重刷、多文件 concat 都会产生同日重复行，这是真实输入而非假设输入。",
      "exact_code_change": "L386-390 替换为：\nif dates is None:\n    order = np.arange(idx.size, dtype=np.int64)\nelse:\n    d_grp = dates[idx]\n    if pd.Series(d_grp).duplicated().any():\n        raise ValueError('同一标的存在重复交易日，违反契约 date 唯一性')\n    order = np.argsort(d_grp, kind='mergesort')\n\n# 并删除 L386 上方「重复日期保持原始相对顺序」注释。"
    }
  ]
}
```

修复时以契约为准：契约与审查意见冲突时，先按契约实现，再在注释中标注冲突点。


## 上一轮产物

`/private/tmp/cmpR/final_code.py` — 共 301 行，已归档到 `history/`。
**先读这个文件**，在它的基础上定点修复，不要凭记忆重写。

## 自检失败日志（末 67 字符）

```
[SUITE]
suite: 第 41 根为一字板（open==high==low==close）却仍建仓，未顺延
[/SUITE]

```

## 修复要求

上一轮产物自检未通过。按日志里的错误标记处理：
- [SYNTAX] / [IMPORT]：是写错了，直接修；不要注释掉功能或加 try/except 绕过。
- [RUNTIME]：执行期异常，先定位触发条件再改实现；不要吞掉异常。
- [ASSERT]：断言失败说明逻辑错，要改实现，不要改断言或删用例。
保持已经通过的部分不变，不要重构无关代码。

- 写回同一个文件 `/private/tmp/cmpR/final_code.py`，保持完整内容，不允许省略或占位
- 不要删除或弱化已有的自检用例来“让它通过”
- 不要执行代码，不要跑测试，脚本会自己跑自检
- 完成后一句话说明改了什么，不要回贴全文
