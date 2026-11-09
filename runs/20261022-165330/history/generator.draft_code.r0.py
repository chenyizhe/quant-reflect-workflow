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
        idx = np.flatnonzero(gid == k)
        if idx.size == 0:
            continue

        # 组内稳定排序（重复日期保持原始相对顺序）
        if dates is None:
            order = np.arange(idx.size, dtype=np.int64)
        else:
            order = np.argsort(dates[idx], kind="mergesort")

        take = idx[order]                    # 组内有序的绝对行号
        inv = np.empty(order.size, dtype=np.int64)
        inv[order] = np.arange(order.size, dtype=np.int64)   # 有序位 → 组内原始位

        sub = df.iloc[take]
        s_sorted, p_sorted, m_sorted = _compute_one(sub, window)

        # 预分配组内缓冲（组内原始行序），按逆置换填回后落到全局数组
        g_sig = np.zeros(idx.size, dtype=np.int8)
        g_pos = np.zeros(idx.size, dtype=np.float64)
        g_mom = np.full(idx.size, np.nan, dtype=np.float64)

        g_sig[inv] = s_sorted
        g_pos[inv] = p_sorted
        g_mom[inv] = m_sorted

        sig_out[idx] = g_sig
        pos_out[idx] = g_pos
        mom_out[idx] = g_mom

    # ---- 输出：index 与行数与入参完全一致（不因预热期截断） ----
    out = pd.DataFrame(index=df.index)
    out["signal"] = pd.Series(sig_out, index=df.index, dtype="int8")
    out["position"] = pd.Series(pos_out, index=df.index, dtype="float64")
    out["mom"] = pd.Series(mom_out, index=df.index, dtype="float64")
    return out
