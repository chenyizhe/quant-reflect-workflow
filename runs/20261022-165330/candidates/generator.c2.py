"""20 日动量策略（A 股 T+1）—— 范式 B：防御与分层解耦。

分层结构（便于单测）：
  1. 配置层  _resolve_config          —— 参数校验与默认值收敛
  2. 适配层  _coerce_frame            —— 列名解析、dtype 归一、分组键/时间键提取
  3. 判定层  _suspend_mask / _one_word_mask / _momentum —— 纯函数，只吃 numpy 数组
  4. 状态层  _positions               —— 挂单 + T+1 + 顺延的可成交状态机
  5. 编排层  run_strategy             —— 面板分组、原序回填、产物组装

契约对齐（唯一真值）：
  - signal_timing = t 日收盘；execution_price = t+1 开盘；warmup = window 根 bar
  - signal / position 均为 {0, 1}；position 严格滞后 signal 一步（t 收盘出信号，t+1 才可能建仓）
  - mom == 0 判空仓（"动量为正才持有"，零不算正）
  - 停牌 / 一字涨停不可买、一字跌停不可卖 → 挂单顺延至下一可交易日，不过期作废
  - 成本参数不进入 signal / position，只做合法性校验

「Flash 标准防坑算子」落地位置：
  #1 面板分组与原序回填   -> _panel_order()   （flatnonzero 切片 + inv 逆置换回填）
  #2 停牌列安全布尔解析   -> _to_bool()        （防 "False" 被 astype(bool) 判 True）
  #3 一字板严格判定       -> _one_word_mask()  （high==low==open==close，1e-12 相对容差）
  #4 估值价连续性         -> _ffill_price()    （open-to-open 收益前先 ffill 执行价；本策略
                                                  不产出收益序列，该算子用于一字板方向判定
                                                  的前一有效收盘，避免 NaN 撕断比较链）
  #5 Pandas 3.0 兼容      -> 全文件仅用 to_numpy(copy=True) / 新数组写入，无只读视图原地改
  #6 空表输入            -> run_strategy() 早退返回列名/dtype 齐全的空 DataFrame（不 raise）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_EPS = 1e-12

_PRICE_KEYS = ("open", "high", "low", "close")
_OPTIONAL_PRICE_KEYS = ("volume",)
_GROUP_KEYS = ("symbol", "code", "ticker", "sec_code", "instrument")
_TIME_KEYS = ("date", "datetime", "trade_date", "time", "dt")
_SUSPEND_FLAG_KEYS = ("suspended", "is_suspended", "suspend", "halt", "is_halt")
_TRADABLE_FLAG_KEYS = ("tradable", "is_tradable", "can_trade")

_DEFAULTS = {
    "window": 20,
    "commission": 0.0003,
    "slippage": 0.001,
    "stamp_tax": 0.0005,
    "min_commission": 5.0,        # 契约：单笔最低 5 元；本节点不产出净值，仅校验
    "ffill_limit": None,          # None -> 取 window，对应 nan_policy=ffill_limit_window_then_drop
    "block_one_word_limit": True,  # 一字板不可成交（契约默认开启）
}
_ALLOWED_KEYS = frozenset(_DEFAULTS)


# --------------------------------------------------------------------------- #
# 算子 #2：停牌列安全布尔解析（照抄，勿改写）
# --------------------------------------------------------------------------- #
def _to_bool(s):
    if s.dtype == bool:
        return s.to_numpy()
    txt = s.astype("string").str.strip().str.lower()
    return txt.isin(["true", "1", "1.0", "t", "y", "yes"]).to_numpy()


# --------------------------------------------------------------------------- #
# 通用小工具（全部带零值 / 定义域 / NaN 保护）
# --------------------------------------------------------------------------- #
def _is_num(x):
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, bool)


def _close_enough(a, b, rtol=_EPS):
    """相对容差相等判定；任一为 NaN 一律判 False（不伪造真值）。"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    scale = np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
    with np.errstate(invalid="ignore"):
        ok = np.abs(a - b) <= rtol * scale
    return ok & np.isfinite(a) & np.isfinite(b)


def _safe_ratio(num, den):
    """num/den，den 为 0 / NaN / inf 时返回 NaN（禁止裸除）。"""
    num = np.asarray(num, dtype=np.float64)
    den = np.asarray(den, dtype=np.float64)
    out = np.full(num.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(num) & np.isfinite(den) & (den != 0.0)
    if ok.any():
        out[ok] = num[ok] / den[ok]
    return out


def _ffill_price(values, limit=None):
    """算子 #4：价格连续性前值填充（估值价）。返回新数组，不改入参。"""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return arr.copy()
    ser = pd.Series(arr.copy())
    filled = ser.ffill(limit=limit)
    return filled.to_numpy(dtype=np.float64, copy=True)


# --------------------------------------------------------------------------- #
# 1. 配置层
# --------------------------------------------------------------------------- #
def _resolve_config(params):
    """参数校验与默认值收敛；非法值一律 ValueError，不静默降级。"""
    unknown = sorted(set(params) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"未知参数: {unknown}；允许: {sorted(_ALLOWED_KEYS)}")

    cfg = dict(_DEFAULTS)
    cfg.update(params)

    window = cfg["window"]
    if isinstance(window, bool) or not isinstance(window, (int, np.integer)):
        raise ValueError(f"window 必须为整数，收到 {type(window).__name__}={window!r}")
    if int(window) < 1:
        raise ValueError(f"window 必须 >= 1，收到 {window!r}")
    cfg["window"] = int(window)

    for key in ("commission", "slippage", "stamp_tax", "min_commission"):
        val = cfg[key]
        if not _is_num(val):
            raise ValueError(f"{key} 必须为数值，收到 {type(val).__name__}={val!r}")
        val = float(val)
        if not np.isfinite(val):
            raise ValueError(f"{key} 必须为有限值，收到 {val!r}")
        if val < 0.0:
            raise ValueError(f"{key} 不能为负，收到 {val!r}")
        cfg[key] = val

    cfg["ffill_limit"] = cfg["window"] if cfg["ffill_limit"] is None else cfg["ffill_limit"]
    lim = cfg["ffill_limit"]
    if isinstance(lim, bool) or not isinstance(lim, (int, np.integer)) or int(lim) < 1:
        raise ValueError(f"ffill_limit 必须为正整数，收到 {lim!r}")
    cfg["ffill_limit"] = int(lim)

    cfg["block_one_word_limit"] = bool(cfg["block_one_word_limit"])
    return cfg


# --------------------------------------------------------------------------- #
# 2. 适配层
# --------------------------------------------------------------------------- #
def _column_map(columns):
    """列名 -> 位置。忽略大小写与首尾空白；归一后重名直接拒绝（防静默取错列）。"""
    mapping = {}
    for pos, name in enumerate(columns):
        if not isinstance(name, str):
            continue
        key = name.strip().lower()
        if key in mapping:
            raise ValueError(f"列名重复（忽略大小写/空白后）: {key!r}")
        mapping[key] = pos
    return mapping


def _pick(colmap, keys):
    for key in keys:
        if key in colmap:
            return colmap[key]
    return None


def _numeric(df, pos):
    return pd.to_numeric(df.iloc[:, pos], errors="coerce").astype("float64").to_numpy(copy=True)


def _coerce_frame(df):
    """数据适配：校验必需列、归一 dtype、抽取分组键与时间键。返回 ctx 字典。"""
    if not isinstance(df, pd.DataFrame):
        raise ValueError(f"输入必须为 pandas.DataFrame，收到 {type(df).__name__}")

    colmap = _column_map(df.columns)
    missing = [k for k in _PRICE_KEYS if k not in colmap]
    if missing:
        raise ValueError(f"缺少必需列: {missing}；实际列: {list(df.columns)}")

    ctx = {
        "colmap": colmap,
        "open": _numeric(df, colmap["open"]),
        "high": _numeric(df, colmap["high"]),
        "low": _numeric(df, colmap["low"]),
        "close": _numeric(df, colmap["close"]),
        "volume": None,
        "suspend_flag": None,
        "tradable_flag": None,
    }

    vpos = _pick(colmap, _OPTIONAL_PRICE_KEYS)
    if vpos is not None:
        ctx["volume"] = _numeric(df, vpos)
    # volume 缺失不伪造：停牌判定退化为「OHLC 全 NaN / 显式标记」两条

    spos = _pick(colmap, _SUSPEND_FLAG_KEYS)
    if spos is not None:
        ctx["suspend_flag"] = _to_bool(df.iloc[:, spos])

    tpos = _pick(colmap, _TRADABLE_FLAG_KEYS)
    if tpos is not None:
        ctx["tradable_flag"] = _to_bool(df.iloc[:, tpos])

    ctx["group"] = _group_codes(df, colmap)
    ctx["time"] = _time_codes(df, colmap)
    return ctx


def _group_codes(df, colmap):
    """分组键 -> 0..G-1 整型码；无分组信息则视为单标的。"""
    gpos = _pick(colmap, _GROUP_KEYS)
    if gpos is not None:
        raw = pd.factorize(df.iloc[:, gpos].astype("string"), sort=False)[0]
    elif isinstance(df.index, pd.MultiIndex):
        names = [str(nm).strip().lower() for nm in df.index.names]
        hit = next((k for k in _GROUP_KEYS if k in names), None)
        if hit is None:
            return np.zeros(len(df), dtype=np.int64)
        level = names.index(hit)
        raw = pd.factorize(df.index.get_level_values(level), sort=False)[0]
    else:
        return np.zeros(len(df), dtype=np.int64)

    codes = np.asarray(raw, dtype=np.int64).copy()
    if codes.size and (codes < 0).any():
        codes[codes < 0] = int(codes.max()) + 1  # 分组键缺失 -> 独立成组，不与其它标的混算
    return codes


def _time_codes(df, colmap):
    """时间键 -> int64 纳秒；无法识别时返回 None（沿用调用方给定顺序）。"""
    tpos = _pick(colmap, _TIME_KEYS)
    if tpos is not None:
        ser = pd.to_datetime(df.iloc[:, tpos], errors="coerce")
        return ser.to_numpy(dtype="datetime64[ns]").astype("int64")

    if isinstance(df.index, pd.DatetimeIndex):
        return df.index.to_numpy(dtype="datetime64[ns]").astype("int64")

    if isinstance(df.index, pd.MultiIndex):
        names = [str(nm).strip().lower() for nm in df.index.names]
        hit = next((k for k in _TIME_KEYS if k in names), None)
        if hit is not None:
            level = names.index(hit)
            ser = pd.to_datetime(df.index.get_level_values(level), errors="coerce")
            return ser.to_numpy(dtype="datetime64[ns]").astype("int64")

    return None


# --------------------------------------------------------------------------- #
# 算子 #1：面板分组 + 组内稳定排序 + 逆置换原序回填
# --------------------------------------------------------------------------- #
def _panel_order(group, time):
    """返回 (sub_orders, inv)：sub_orders 为每个分组的行号（组内时间升序），
    inv 满足 out[inv[r]] == computed_in_merged_order[r]，即按原行号还原。"""
    n = group.size
    if n == 0:
        return [], np.empty(0, dtype=np.int64)

    sub_orders = []
    for gid in pd.unique(group):
        rows = np.flatnonzero(group == gid)
        if time is not None:
            rows = rows[np.argsort(time[rows], kind="stable")]
        sub_orders.append(rows)

    merged = np.concatenate(sub_orders) if sub_orders else np.empty(0, dtype=np.int64)
    inv = np.empty(n, dtype=np.int64)
    inv[merged] = np.arange(n, dtype=np.int64)
    return sub_orders, inv


# --------------------------------------------------------------------------- #
# 3. 判定层（纯函数，只吃 numpy）
# --------------------------------------------------------------------------- #
def _suspend_mask(open_, high, low, close, volume, suspend_flag, tradable_flag):
    """停牌判定：OHLC 全 NaN / volume==0 / 显式标记。三选一即停牌。
    契约中「当日缺行」由上游对齐负责，本函数无法凭空补行（不伪造）。"""
    ohlc_all_nan = np.isnan(open_) & np.isnan(high) & np.isnan(low) & np.isnan(close)
    suspend = ohlc_all_nan.copy()

    if volume is not None:
        with np.errstate(invalid="ignore"):
            zero_vol = np.isfinite(volume) & (volume == 0.0)
        suspend |= zero_vol  # volume 为 NaN 时不判停牌（缺失 != 无成交）

    if suspend_flag is not None:
        suspend |= np.asarray(suspend_flag, dtype=bool)

    if tradable_flag is not None:
        suspend |= ~np.asarray(tradable_flag, dtype=bool)

    return suspend


def _one_word_mask(open_, high, low, close):
    """算子 #3：一字板严格判定（四项全等，1e-12 相对容差）。
    缺 high/low 列时该判定整体不生效（返回全 False），不伪造。"""
    n = close.size
    if high is None or low is None or open_ is None:
        return np.zeros(n, dtype=bool)
    return _close_enough(high, low) & _close_enough(open_, high) & _close_enough(close, high)


def _limit_direction(one_word, close, prev_close):
    """一字板方向：与前一有效收盘比较（含停牌日 ffill，算子 #4）。
    方向不明的窄幅一字板不做限制（不猜涨跌停）。"""
    up = one_word & (close > prev_close) & np.isfinite(prev_close)
    down = one_word & (close < prev_close) & np.isfinite(prev_close)
    return up, down


def _momentum(close, window, ffill_limit):
    """契约公式 mom_t = close_t / close_{t-window} - 1。
    缺失处理 ffill_limit_window_then_drop：中间 NaN 在 window 根 bar 内前值填充，
    超出窗口仍为 NaN -> mom = NaN -> 信号沿用上一有效值（不产生调仓指令）。"""
    n = close.size
    filled = _ffill_price(close, limit=ffill_limit)
    base = np.full(n, np.nan, dtype=np.float64)
    if n > window:
        base[window:] = filled[: n - window]
    return _safe_ratio(filled, base) - 1.0


def _signal_from_mom(mom, window):
    """mom>0 -> 1；mom<=0（含 0）-> 0；mom NaN -> 沿用上一有效值；预热期内为 0。"""
    n = mom.size
    raw = np.full(n, np.nan, dtype=np.float64)
    valid = np.isfinite(mom)
    if valid.any():
        raw[valid] = np.where(mom[valid] > 0.0, 1.0, 0.0)

    carried = pd.Series(raw).ffill().fillna(0.0)
    sig = carried.to_numpy(dtype=np.float64, copy=True)
    sig = np.where(np.isfinite(sig), sig, 0.0)
    sig = np.where(sig > 0.5, 1.0, 0.0)

    warm = min(int(window), n)
    sig[:warm] = 0.0  # 预热期 signal 置 0（强制入口契约）
    return sig.astype(np.int8)


# --------------------------------------------------------------------------- #
# 4. 状态层：T+1 + 停牌/一字板顺延
# --------------------------------------------------------------------------- #
def _positions(signal, tradable, can_buy, can_sell):
    """t 日收盘出信号，t+1 开盘执行 -> position[i] 只可能由 signal[:i] 决定。

    - 挂单 pending 一旦挂出，遇不可成交日不顺延过期，持续到可成交为止；
    - 出现更新的收盘信号时，用新目标覆盖挂单（合理替换，非过期作废）；
    - T+1：position 只在 i 日按 signal[i-1] 变动，买入后最早 i+1 日才可能卖出，
      天然满足最短持仓 1 个交易日；
    - 反手（1->0 / 0->1）在同一个开盘完成，成本由评估层按双边计。
    """
    n = signal.size
    pos = np.zeros(n, dtype=np.float64)
    pending = np.int8(0)
    held = 0.0
    for i in range(n):
        if i > 0:
            pending = signal[i - 1]
        if np.int8(pending) == np.int8(held):
            pos[i] = held
            continue
        if pending == 1:
            if bool(tradable[i]) and bool(can_buy[i]):
                held = 1.0
        else:
            if bool(tradable[i]) and bool(can_sell[i]):
                held = 0.0
        pos[i] = held
    return pos


# --------------------------------------------------------------------------- #
# 单标的（组内已按时间升序）核心计算
# --------------------------------------------------------------------------- #
def _compute_group(ctx, rows, cfg):
    open_ = ctx["open"][rows]
    high = ctx["high"][rows]
    low = ctx["low"][rows]
    close = ctx["close"][rows]
    volume = None if ctx["volume"] is None else ctx["volume"][rows]
    suspend_flag = None if ctx["suspend_flag"] is None else ctx["suspend_flag"][rows]
    tradable_flag = None if ctx["tradable_flag"] is None else ctx["tradable_flag"][rows]

    window = cfg["window"]
    n = close.size

    tradable = ~_suspend_mask(open_, high, low, close, volume, suspend_flag, tradable_flag)

    if cfg["block_one_word_limit"]:
        one_word = _one_word_mask(open_, high, low, close)
        prev_close = np.full(n, np.nan, dtype=np.float64)
        if n > 1:
            prev_close[1:] = _ffill_price(close, limit=cfg["ffill_limit"])[:-1]
        up, down = _limit_direction(one_word, close, prev_close)
        can_buy = tradable & ~up      # 一字涨停 -> 买不到
        can_sell = tradable & ~down   # 一字跌停 -> 卖不掉
    else:
        can_buy = tradable
        can_sell = tradable

    mom = _momentum(close, window, cfg["ffill_limit"])
    signal = _signal_from_mom(mom, window)
    position = _positions(signal, tradable, can_buy, can_sell)
    return signal, position, mom


# --------------------------------------------------------------------------- #
# 5. 编排层：强制入口
# --------------------------------------------------------------------------- #
def _empty_result(index):
    """算子 #6：空表返回列名 / dtype 齐全的空 DataFrame，不 raise。"""
    return pd.DataFrame(
        {
            "signal": np.zeros(0, dtype=np.int8),
            "position": np.zeros(0, dtype=np.float64),
            "mom": np.full(0, np.nan, dtype=np.float64),
        },
        index=index,
    )


def _new_result(index, signal, position, mom):
    out = pd.DataFrame(
        {
            "signal": np.asarray(signal, dtype=np.int8),
            "position": np.asarray(position, dtype=np.float64),
            "mom": np.asarray(mom, dtype=np.float64),
        },
        index=index,
    )
    out["signal"] = out["signal"].astype(np.int8)
    out["position"] = out["position"].astype(np.float64).clip(0.0, 1.0)
    out["mom"] = out["mom"].astype(np.float64)
    return out


def run_strategy(df, **params):
    """20 日动量策略主入口。

    Parameters
    ----------
    df : pd.DataFrame
        日线数据，必需列 open/high/low/close，可选 volume 及停牌标记列。
        index 可为 DatetimeIndex、MultiIndex(symbol, date) 或含 symbol 列的重复日期索引。
    **params
        window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005,
        min_commission=5.0, ffill_limit=None, block_one_word_limit=True

    Returns
    -------
    pd.DataFrame，index 与入参完全一致、行数不变：
        signal   int8     {0, 1}   预热期 0
        position float64  {0.0, 1.0} 为 signal 的滞后（t 收盘出信号，t+1 才可能建仓）
        mom      float64  20 日动量，预热期 / 不可算处为 NaN
    """
    cfg = _resolve_config(params)
    if df is None:
        raise ValueError("输入 df 为 None")
    ctx = _coerce_frame(df)

    n = len(df)
    if n == 0:
        # 契约 §5 与算子 #6 的冲突处理：空表按算子 #6 早退返回空表（列名/dtype 齐全），
        # 缺列等结构性错误仍由 _coerce_frame 抛 ValueError。
        return _empty_result(df.index)

    sub_orders, inv = _panel_order(ctx["group"], ctx["time"])

    signal_flat = np.zeros(n, dtype=np.int8)
    position_flat = np.zeros(n, dtype=np.float64)
    mom_flat = np.full(n, np.nan, dtype=np.float64)

    cursor = 0
    for rows in sub_orders:
        stop = cursor + rows.size
        sig, pos, mom = _compute_group(ctx, rows, cfg)
        signal_flat[cursor:stop] = sig
        position_flat[cursor:stop] = pos
        mom_flat[cursor:stop] = mom
        cursor = stop

    # 逆置换回填：恢复调用方原始行序
    signal_out = signal_flat[inv]
    position_out = position_flat[inv]
    mom_out = mom_flat[inv]

    # 预热期硬约束（按组内位置已置 0，这里再按整体兜底一次，防止分组回填错位）
    warm = min(cfg["window"], n)
    signal_out[:warm] = 0
    position_out[:warm] = 0.0

    return _new_result(df.index, signal_out, position_out, mom_out)
