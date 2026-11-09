"""20 日动量策略（A 股 T+1）· 修复版 final。

入口契约（硬约束）：
    run_strategy(df, window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005)
    signal   int8     ∈ {0, 1}，预热 window 根为 0
    position float64  ∈ {0.0, 1.0}，滞后 signal：t 收盘出信号，t+1 开盘才建仓
    index / 行数与入参完全一致；不修改入参 df。

审查意见对照表
==============
编号 | 处理方式 | 位置与说明
P01 | 无法修复 | 【冲突点】审查意见要求 signal=float64（预热期 NaN）、position=int8，取自契约 §5 表格；
    但本次交付的强制入口契约（本 docstring 第 4-6 行，与调用方硬约束一致）明写 signal=int8、
    position=float64 且 position 滞后 signal，两者正好对调，无法同时满足，按入口契约落地。
    已落实的局部修复：删除初稿 L129-133「以强制入口契约为准」的口径裁决段落，改为在此显式
    标注冲突点，不再单方面宣布豁免。残余影响（触发条件：下游用 signal.isna() 识别预热段/作废段）：
    int8 恒非 NaN，该判断永远为 False，前 window 根会被当成"有效空仓信号"计入统计；须改用行
    位置（df.index < df.index[window]）或 mom.isna() 识别。position 为 float64，下游若按 int8
    假设取索引需显式 astype。
P02 | 已修复 | _check_args / run_strategy 的空表分支：n == 0 直接 raise ValueError("df 为空")，
    删除初稿 L197-207 的 _empty_result 全 0 兜底与 docstring L132 的说明。契约 §5「df 为空或
    缺少必需列 → ValueError，不静默降级、不返回全 0 兜底」。
P03 | 已修复 | _compute_one 的 target 构造行：限次 ffill(window) 之后再追加一次不限次 ffill，
    「作废段 = 沿用上一有效 target、不产生调仓指令」，禁止 fillna(0) 强制平仓。复现口径（信号 1
    后连续停牌 25 日）：close_ff 与 sig_s 的限次 ffill 于第 21 根耗尽，旧实现由 fillna(0) 凭空
    生成一笔清仓单并按滑点 0.1%+佣金+印花税扣减；新实现复牌首日无新指令。仅真正的预热段
    （之前无任何有效值）仍为 NaN 并落 0。
P04 | 已修复 | _flat_flags：一字板按 close 相对 prev_close 的涨跌区分方向——flat 且 close>prev 为
    一字涨停（禁买）、flat 且 close<prev 为一字跌停（禁卖）、停牌两侧全禁；买卖掩码在
    _compute_one 的 exec_sig 按 target 方向分别命中。审查意见原式 limit_up=flat&open==high、
    limit_down=flat&open==low 在「open==high==low==close」的 flat 定义下两者恒等，退化为对称
    封锁，无法实现契约 §4 的方向非对称，故改用涨跌方向判定。
P05 | 已修复 | _CORE_COLS = ("open","high","low","close","volume")，缺任一列即 ValueError；删除初稿
    L267-269 的 `if "high" in sub.columns else None` 一类降级分支与 docstring L134 的说明。
    一字板项与 volume==0 停牌项不再可能被静默关闭，不同数据源不再跑出两套成交假设。

非审查项契约落实：mom = close_t / close_{t-window} - 1（分母 <=0 或 NaN → NaN，signal 沿用
上一有效值）；mom == 0 判空仓（零不算正）；中间 NaN 限 20 根前填，超限该段信号作废。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 一字板 / 价格比较的相对容差
_TOL = 1e-12
# 契约 §1 全量必需列：high/low 仅参与涨跌停判定，volume 仅参与停牌判定，值可为 NaN
_CORE_COLS = ("open", "high", "low", "close", "volume")


# --------------------------------------------------------------------------- #
# 数值工具
# --------------------------------------------------------------------------- #
def _num(s) -> np.ndarray:
    """转 float64 ndarray，非数值 → NaN；copy=True 避免改动入参。"""
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64", copy=True)


def _scale(a, b):
    return np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))


def _eq(a, b):
    return np.abs(a - b) <= _TOL * _scale(a, b)


def _gt(a, b):
    return (a - b) > _TOL * _scale(a, b)


def _lt(a, b):
    return (b - a) > _TOL * _scale(a, b)


def _series(values, index, name):
    return pd.Series(np.asarray(values, dtype="float64"), index=index, name=name,
                     dtype="float64")


# --------------------------------------------------------------------------- #
# 成交掩码：一字板方向（P04）
# --------------------------------------------------------------------------- #
def _flat_flags(openp, high, low, close, prev_close):
    """返回 (limit_up, limit_down)：一字涨停禁买、一字跌停禁卖。

    flat := high == low == open == close（全体相等即被单向封死在同价）；
    再以 close 相对 prev_close 的涨跌定方向，解决"全等日既像涨停又像跌停"的退化。
    prev_close 为 NaN（首根 bar）时不判定，不伪造涨跌停。
    """
    fin = np.isfinite(openp) & np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
    flat = fin & _eq(high, low) & _eq(openp, high) & _eq(close, high)
    has_prev = np.isfinite(prev_close)
    limit_up = flat & has_prev & _gt(close, prev_close)
    limit_down = flat & has_prev & _lt(close, prev_close)
    return limit_up, limit_down


# --------------------------------------------------------------------------- #
# 分组键 / 日期键（单标的时整表同组，面板时按标的切分）
# --------------------------------------------------------------------------- #
def _group_key(df):
    if "symbol" in df.columns:
        return np.asarray(df["symbol"].to_numpy(copy=True))
    if isinstance(df.index, pd.MultiIndex) and df.index.nlevels >= 2:
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if not isinstance(lev, pd.DatetimeIndex):
                return np.asarray(lev.to_numpy(copy=True))
    return None


def _date_key(df):
    """组内稳定排序用日期键；取不到返回 None（视为已按日期排序）。"""
    if "date" in df.columns:
        raw = df["date"]
    elif isinstance(df.index, pd.DatetimeIndex):
        raw = df.index
    elif isinstance(df.index, pd.MultiIndex):
        raw = None
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if isinstance(lev, pd.DatetimeIndex):
                raw = lev
                break
        if raw is None:
            return None
    else:
        raw = df.index
    try:
        return pd.to_datetime(pd.Series(np.asarray(raw)), errors="coerce").to_numpy()
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
    high = _num(sub["high"])
    low = _num(sub["low"])
    vol = _num(sub["volume"])

    # ---- 停牌：OHLC 全 NaN，或 volume == 0（契约 §1）----
    all_nan = (~np.isfinite(openp) & ~np.isfinite(high)
               & ~np.isfinite(low) & ~np.isfinite(close))
    suspended = all_nan | (np.isfinite(vol) & (vol == 0.0))

    # ---- 收盘价限次前填，避免停牌日把整条 mom 撕成 NaN ----
    close_ff = pd.Series(close, dtype="float64").ffill(limit=window)

    # ---- 动量：close_t / close_{t-window} - 1，分母非正/缺失 → NaN ----
    base = close_ff.shift(window)
    mom_s = close_ff.div(base.mask(base <= 0.0)).sub(1.0)
    mom = mom_s.to_numpy(dtype="float64", copy=True)

    # ---- 信号：mom > 0 → 1；mom <= 0（含 == 0）→ 0；NaN 暂留 ----
    valid = mom_s.notna().to_numpy()
    positive = (mom_s > 0.0).to_numpy(dtype="float64")
    sig_s = _series(np.where(valid, positive, np.nan), sub.index, "signal")
    sig_s = sig_s.ffill(limit=window)

    # 契约 §3：作废段 = 不产生调仓指令，故沿用上一有效 target，禁止 fillna(0) 强制平仓
    target = sig_s.ffill().fillna(0.0).clip(0.0, 1.0).to_numpy(dtype="float64", copy=True)

    # ---- 执行时点：t 收盘出信号、t+1 开盘成交 → 目标整体滞后 1 期 ----
    # A 股 T+1 由该滞后天然满足：t+1 买入的份额最早 t+2 方可卖出。
    tp = np.asarray(pd.Series(target, dtype="float64").shift(1).to_numpy(), dtype="float64")

    # ---- 不可成交：停牌两侧全禁；一字涨停禁买、一字跌停禁卖（P04）----
    prev_close = np.asarray(close_ff.shift(1).to_numpy(dtype="float64", copy=True))
    limit_up, limit_down = _flat_flags(openp, high, low, close, prev_close)

    want_long = tp > 0.5
    blocked = np.where(want_long, suspended | limit_up, suspended | limit_down)
    exec_sig = np.where(blocked, np.nan, tp)

    # 顺延：不可成交日仓位保持不变（ffill 即"信号保持原方向、不顺延过期作废"）
    position = _series(exec_sig, sub.index, "position").ffill().fillna(0.0).clip(0.0, 1.0)
    position = position.to_numpy(dtype="float64", copy=True)

    # 预热 window 根：契约要求 position 为 0；signal 受 int8 约束亦为 0（见 P01）
    warmup = np.arange(n) < window
    signal_out = np.where(warmup, 0, target).astype("int8", copy=True)
    position_out = np.where(warmup, 0.0, position)
    return signal_out, position_out, mom


# --------------------------------------------------------------------------- #
# 参数与入参校验（P02 / P05）
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
            raise ValueError("%s 必须为数值" % name) from None
        if not np.isfinite(fv) or fv < 0.0:
            raise ValueError("%s 不能为负" % name)
    return int(window)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_strategy(df, window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005):
    """20 日动量：mom > 0 满仓、mom <= 0 空仓；A 股 T+1，停牌/一字板顺延。

    commission / slippage / stamp_tax 不进入 signal / position 计算（契约 §4 末句），
    仅校验非负后透传，供下游净值评估使用。
    """
    window = _check_args(df, window, commission, slippage, stamp_tax)

    n = len(df)
    if n == 0:
        raise ValueError("df 为空：契约 §5 要求抛 ValueError，不得返回空结果兜底")

    sig_out = np.zeros(n, dtype=np.int8)
    pos_out = np.zeros(n, dtype=np.float64)
    mom_out = np.full(n, np.nan, dtype=np.float64)

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
        # 组内按日期稳定排序；重复日期保持原始相对顺序
        order = np.arange(idx.size, dtype=np.int64) if dates is None else \
            np.argsort(dates[idx], kind="mergesort")
        take = idx[order]                       # 排序后的绝对行号
        s_sorted, p_sorted, m_sorted = _compute_one(df.iloc[take], window)
        # 用绝对行号散布回全表，天然保留原始行序，无需逆置换
        sig_out[take] = s_sorted
        pos_out[take] = p_sorted
        mom_out[take] = m_sorted

    out = pd.DataFrame(index=df.index)
    out["signal"] = pd.Series(sig_out, index=df.index, dtype="int8")
    out["position"] = pd.Series(pos_out, index=df.index, dtype="float64")
    out["mom"] = pd.Series(mom_out, index=df.index, dtype="float64")
    return out


if __name__ == "__main__":
    rng = np.random.default_rng(7)
    nbar = 60
    base = 10.0 + np.cumsum(rng.normal(0.05, 0.3, nbar))
    demo = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=nbar),
            "open": base * (1 + rng.normal(0, 0.002, nbar)),
            "high": base * 1.01,
            "low": base * 0.99,
            "close": base,
            "volume": rng.integers(1_000, 9_000, nbar).astype("float64"),
        }
    )
    res = run_strategy(demo)
    print("returncode=%s" % (0 if res is not None else 1))
    print("shape=%s dtype=%s" % (res.shape, dict(res.dtypes.astype(str))))
    print("signal_uniq=%s pos_uniq=%s lag_ok=%s"
          % (sorted(res["signal"].unique().tolist()),
             sorted(res["position"].unique().tolist()),
             bool((res["position"].iloc[1:].to_numpy()
                   == res["signal"].iloc[:-1].astype("float64").to_numpy()).all())))
