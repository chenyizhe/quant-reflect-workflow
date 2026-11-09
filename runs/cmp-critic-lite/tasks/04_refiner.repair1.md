# 节点任务书 · 重构修复者 (refiner) · 返修第 1 轮

- run: `cmpL`
- 模型档位: **lite**（来源: models.fast，调用子代理时必须传 model="lite"）
- 触发原因: 自检未通过（退出码 1，命令 `/Users/chenyizhe/.workbuddy/binaries/python/envs/default/bin/python /Volumes/plugHD/workbuddy/WorkBuddy/2026-10-21-15-17-31/quant-reflect-workflow/tools/check_code.py /private/tmp/cmpL/final_code.py --level run --timeout 240`）

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
  "summary": "运行时探针全绿（smoke/causal/timing/panel/shape 均 ok）说明前视、面板回填与 T+1 滞后无静态缺陷；但返回契约的 dtype 与预热期语义、空表 ValueError 两处直接违反契约 §5，加上 close 先 ffill 再算动量、一字板买卖不分方向，共 4 条不报错但系统性错单的路径，判 block。",
  "verdict": "block",
  "actionable_fixes": [
    {
      "id": "P01",
      "severity": "阻断",
      "target_function": "run_strategy",
      "line_hint": "L412",
      "root_cause": "L412 `out['signal'] = pd.Series(sig_out, index=df.index, dtype='int8')`、L413 `out['position'] = pd.Series(pos_out, index=df.index, dtype='float64')`；契约 §5 L93-L94 明确 signal 为 float64、{0.0,1.0}、预热期为 NaN，position 为 int8、{0,1}、预热期为 0。文件头 L123-L127 的『口径裁决』是生成器自述，不能覆盖唯一真值契约。L319 `signal_out = np.where(warmup, 0, target).astype(np.int8)` 与 L321 `position_out = np.where(warmup, 0.0, position_out)` 把预热期信号写成 0，使『前 20 bar 未预热』与『真实空仓』不可区分：下游按 L413 的 position 做绩效归因时会把这段计入空仓区间，而 int8 本身无法承载契约要求的 NaN，属于结构性返约失败。",
      "exact_code_change": "1) L319 改为 `signal_out = np.where(warmup, np.nan, target).astype('float64', copy=True)`；2) L321 改为 `position_out = np.where(warmup, 0, position.to_numpy()).astype('int8', copy=True)`；3) L412 改为 `out['signal'] = pd.Series(sig_out, index=df.index, dtype='float64')`；4) L413 改为 `out['position'] = pd.Series(pos_out, index=df.index, dtype='int8')`；5) L202/L203 的 `_empty_result` 同步改为 `'signal': pd.Series(np.full(n, np.nan), dtype='float64')` 与 `'position': pd.Series(np.zeros(n, dtype=np.int8))`。"
    },
    {
      "id": "P02",
      "severity": "阻断",
      "target_function": "run_strategy",
      "line_hint": "L361",
      "root_cause": "L360-L362 `n = len(df); if n == 0: return _empty_result(df)`。契约 §5 L98 明确『df 为空或缺少必需列 → ValueError』且『不静默降级、不返回全 0 兜底』；L132 自述『空表（列齐全）→ 返回同结构空 DataFrame，不 raise』与契约正面冲突。后果：数据源断链/读表失败返回空表时，调用方拿到一份索引与列齐全、全 0 仓位的『合法』输出，绩效恒为 0 而不是异常中断，属于典型的静默降级。",
      "exact_code_change": "把 L361-L362 替换为 `if n == 0: raise ValueError('df 为空，无法计算动量')`；同时删除 L197-L207 的 `_empty_result` 定义（不再有调用点）与 L132 的自述条款；L329-L334 的 `_check_args` 中在列校验前补 `if df is None or len(df) == 0: raise ValueError('df 为空，无法计算动量')`，保证参数校验与空表判定同一出口。"
    },
    {
      "id": "P03",
      "severity": "高危",
      "target_function": "_compute_one",
      "line_hint": "L286",
      "root_cause": "L286 `close_ff = pd.Series(close, dtype='float64').ffill(limit=window)` 在 L289 `base = close_ff.shift(window)` 与 L291 `mom_s = close_ff.div(base_safe).sub(1.0)` 之前就把停牌日 NaN 填成前收盘。契约 §3 L72 要求『close_{t-window} 为 0 或 NaN → mom = NaN，signal 沿用上一有效值』。填值后分子分母同源于同一前值，停牌段内 mom 恒等于 0.0，L295 `pos_mask = (mom_s > 0.0)` 为 False、L296 `valid` 却为 True，L297 因此产出 0.0 而非 NaN，L299 的 `sig_s.ffill(limit=window)` 完全无从生效，L301 `target = sig_s.fillna(0.0)` 把仓位强制翻 0。即：一次 20 日以内的停牌会让『沿用上一有效信号』退化成用伪造的零动量强行平仓，复牌首日按 0 仓执行。",
      "exact_code_change": "删掉 L286 的预填值，L289 改为 `base = pd.Series(close, dtype='float64').shift(window)`，L291 改为 `mom_s = pd.Series(close, dtype='float64').div(base_safe).sub(1.0)`（`base_safe = base.mask(base <= 0.0)` 保持在 L290）；停牌价格的 ffill 只用于估值展示，不参与 mom；缺失处理仍只由 L299 的 `sig_s.ffill(limit=window)` 承担，确保停牌日 mom 为 NaN 而非 0.0。"
    },
    {
      "id": "P04",
      "severity": "高危",
      "target_function": "_flat_board",
      "line_hint": "L193",
      "root_cause": "L192-L194 `flat = ok & _eq(high, low) & _eq(openp, high) & _eq(close, high)` 把一字板定义成『high==low==open==close』的全天封死形态，且 L308 把同一个布尔量无差别用于买卖两端：`blocked = suspended | _flat_board(...)`。两个方向的错单：(a) 盘中封板后打开的票（open==high、low<high）被漏判为可成交，回测以涨停价买入，实盘根本没有对手盘，系统性高估收益；(b) 一字涨停日禁止卖出、一字跌停日禁止买入，与实际约束恰好相反地收紧，导致该卖的组合被强行顺延、该买的永不成交。契约 §4 L79 的要求是方向敏感的：『一字涨停不可买入、一字跌停不可卖出』。",
      "exact_code_change": "把 L305-L314 整段改为方向敏感的阻塞：`tp = pd.Series(target, dtype='float64').shift(1).to_numpy(); flat = pd.Series(_flat_board(openp, high, low, close), dtype='bool').to_numpy(); ret1 = pd.Series(close, dtype='float64').shift(1); ret1v = (pd.Series(close, dtype='float64') / ret1 - 1.0).to_numpy(); up_flat = flat & (ret1v >= 0.095); dn_flat = flat & (ret1v <= -0.095); sus = pd.Series(suspended, dtype='bool').to_numpy(); blocked_action = ((sus | up_flat) & (tp == 1.0)) | ((sus | dn_flat) & (tp == 0.0)); exec_sig = pd.Series(np.where(blocked_action, np.nan, tp), dtype='float64'); position = exec_sig.ffill().fillna(0.0).clip(0.0, 1.0)`。这样只在『要买却涨停/停牌』或『要卖却跌停/停牌』时置 NaN 保持原仓，反方向不受影响，仍保持纯向量化。"
    },
    {
      "id": "P05",
      "severity": "中危",
      "target_function": "_check_args",
      "line_hint": "L156",
      "root_cause": "L156 `_CORE_COLS = ('open', 'close')` 与 L332 `missing = [c for c in _CORE_COLS if c not in df.columns]` 只强制这两列；数据契约 §1 L45-L50 把 high/low/close/volume 全部列为数据表字段（可空性仅指取值可为空，不是列可缺），§5 L98 要求『缺少必需列 → ValueError』。更严重的失效在 L190-L191：`if high is None or low is None: return np.zeros(openp.shape[0], dtype=bool)`，缺 high/low 时一字板判定整体退化为全 False，L308 的 blocked 只剩停牌一路，契约 §4 L79 的『一字涨停买不进/一字跌停卖不出』被静默关闭且无任何告警，回测直接按收盘可成交计入。",
      "exact_code_change": "L156 改为 `_CORE_COLS = ('open', 'high', 'low', 'close')`；L332 前补 `if 'volume' not in df.columns: raise ValueError('df 缺少必需列: volume')`（volume 值可空但列必须存在，否则停牌判定的 volume==0 分支在 L281 被静默跳过）；删除 L190-L191 的降级分支，改为 `if high is None or low is None: raise ValueError('high/low 缺失，无法判定一字板')`，并同步删除 L134 的自述条款。"
    },
    {
      "id": "P06",
      "severity": "中危",
      "target_function": "run_strategy",
      "line_hint": "L355",
      "root_cause": "L339-L345 只对 commission/slippage/stamp_tax 做有限性与非负校验，L352 入口接收后除校验外零使用，L355-L356 自述『本函数不做净值回测』。需求『单边佣金万三、滑点千一』与契约 §4 L80-L84（买入价 = open×(1+0.001)、卖出价 = open×(1-0.001)、佣金双向万三、印花税千五单边）在返回值中完全不可观测：下游拿 L413 的 position 直接乘 close 收益，全程零成本，一次买卖往返漏计约 0.001+0.0003+0.001+0.0003+0.0005≈0.31%，20 日动量年换手 10 次以上时年化收益被系统性高估 3 个百分点量级，且调用方无法从返回值判断成本是否已计入。",
      "exact_code_change": "在 L414 之后、L415 `return out` 之前按契约 §4 逐笔口径落地成本并旁挂，不新增列以保持 §5 的两列契约：`turn = np.diff(np.concatenate(([0.0], pos_out.astype('float64')))); rate = np.zeros(n, dtype='float64'); bm = turn > 0; sm = turn < 0; rate[bm] = turn[bm] * (float(slippage) + float(commission)); rate[sm] = -turn[sm] * (float(slippage) + float(commission) + float(stamp_tax)); out.attrs['cost_rate'] = pd.Series(rate, index=df.index); out.attrs['total_cost'] = float(rate.sum())`；若上游同意扩列，则在契约 §5 增加 `net_return` 列并改为 `out['net_return'] = pos_out * (df['close'].pct_change().fillna(0.0)) - out.attrs['cost_rate']`。"
    }
  ]
}
```

修复时以契约为准：契约与审查意见冲突时，先按契约实现，再在注释中标注冲突点。


## 上一轮产物

`/private/tmp/cmpL/final_code.py` — 共 315 行，已归档到 `history/`。
**先读这个文件**，在它的基础上定点修复，不要凭记忆重写。

## 自检失败日志（末 85 字符）

```
[SUITE]
suite: window='20' 未报错
  第 41 根为一字板（open==high==low==close）却仍建仓，未顺延
[/SUITE]

```

## 修复要求

上一轮产物自检未通过。按日志里的错误标记处理：
- [SYNTAX] / [IMPORT]：是写错了，直接修；不要注释掉功能或加 try/except 绕过。
- [RUNTIME]：执行期异常，先定位触发条件再改实现；不要吞掉异常。
- [ASSERT]：断言失败说明逻辑错，要改实现，不要改断言或删用例。
保持已经通过的部分不变，不要重构无关代码。

- 写回同一个文件 `/private/tmp/cmpL/final_code.py`，保持完整内容，不允许省略或占位
- 不要删除或弱化已有的自检用例来“让它通过”
- 不要执行代码，不要跑测试，脚本会自己跑自检
- 完成后一句话说明改了什么，不要回贴全文
