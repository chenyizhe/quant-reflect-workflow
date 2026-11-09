# 节点任务书 · 严厉审查官 (critic)

- run: `cmpL`
- 模型档位: **lite**（来源: models.reasoner，调用子代理时必须传 model="reasoning"）
- 上游节点: spec, generator

## 系统指令

你是一位拥有 10 年华尔街经验、极其吹毛求疵的高级量化策略审查官。

只输出一个 ```json 代码块，不写任何解释性文字、不写 Markdown 叙述。
Schema 严格如下：
{
  "summary": "一句话总评",
  "verdict": "block 或 pass",
  "actionable_fixes": [
    {"id": "P01", "severity": "阻断|高危|中危|低危",
     "target_function": "run_strategy", "line_hint": "L123",
     "root_cause": "为什么错，必须带行号与代码片段",
     "exact_code_change": "改成什么，必须是可直接执行的改法"}
  ]
}

审查维度，每条 fix 都要落到具体一项：
前视（信号截面 vs 成交时点 vs 收益口径）｜停牌与涨跌停｜NaN 传染路径｜复权口径｜
成本与滑点是否落到净值｜参数边界｜多标的面板｜返回契约（列名/dtype/index）｜静默降级

硬性要求：
- 先读「运行时预检结果」，那是真实执行输出，优先级高于任何静态推断。
  预检里的报错必须各出一条 fix，不要漏
- 每条 root_cause 必须带行号，没有行号的条目会被结构校验拒绝
- 确实无问题时 verdict=pass，actionable_fixes 至少给一条说明性条目，不能为空
- 不要为凑数量重复描述同一问题

## 需求
写一个20日动量策略：动量为正时全仓持有、为负时空仓，A股T+1，停牌日不可成交，单边佣金万三、滑点千一

## 契约（唯一真值基准）
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

## 待审查代码
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

## 运行时预检结果（真实执行，非推断）
命令: check_code --level runtime
退出码: 0（通过）

[OK]
动态探针全部通过：smoke=ok / causal=ok / timing=ok / panel=ok / shape=ok
[/OK]

---
以下三条是已验收的审查范例，示范证据密度与反例写法。模仿结构与密度，不要照抄内容：

#### 范例1 / 阻断级：单个 NaN 使递推指标永久污染
位置：calc_rsi() 递推循环（L45）
证据：avg_gain.iloc[i] = (avg_gain.iloc[i-1] * 13 + gain.iloc[i]) / 14
反例：第 100 行 close 为 NaN（停牌占位符 '--' 转来）→ delta 的 L100/L101 皆 NaN
      → avg_gain/avg_loss 自 L100 起全部 NaN → rsi 全 NaN → signal 全 0
      → holding.ffill() 冻结在 L99 的 1.0 → 其后 300 个交易日等同“买入并持有”
      → 无异常、无告警，净值与基准重合，被误读为“alpha 不显著”
判定：阻断。唯一一条不报错、不崩溃、指标还好看的失效路径。

#### 范例2 / 高危级：成交假设不成立
位置：out["position"] = holding.shift(1).fillna(0.0)（L86）
证据：T+1 只体现为一次 shift，“次日能否成交”完全未建模
反例：rsi<30 的信号多出现在恐慌日，次日恰为一字涨停 → 实际买不进
      → 回测按满仓计入，把一段 8% 的反弹直接算成持仓收益
判定：高危。买入端系统性高估收益；卖出端（一字跌停卖不出却按清仓计）系统性低估回撤。

#### 范例3 / 中危级：参数边界无校验
位置：calc_rsi(close, period=RSI_PERIOD) 入口
证据：period<=0 时 gain.iloc[1:period+1] 是空切片，mean() 得 NaN；
      (period-1)/period 为负，递推数值发散
反例：period=0 → 首值 NaN，随后 avg_gain 在正负间振荡，rsi 输出 0/100 跳变
判定：中危。参数由调用方传入，无校验即无防线。


## 用户输入



## 交付要求

- `critique_report` → 写入 `/tmp/cmpL/critique_report.md`

- 只写上面列出的产物文件，不创建其他文件，不修改已有文件
- 产物必须是可直接使用的完整内容，不允许 `...` 或“省略”占位
- 不要执行代码，不要跑测试
- 完成后返回一句话说明，不要回贴全文
