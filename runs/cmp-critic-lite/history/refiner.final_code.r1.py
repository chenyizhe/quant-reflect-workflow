"""20 日动量策略（A 股 T+1）· 重构修复版（refiner · 返修第 1 轮）。

入口契约（强制，优先级高于文件自身的任何口径自述）
    run_strategy(df, **params) -> pd.DataFrame
        signal   : int8     ∈ {0, 1}，t 日收盘后计算
        position : float64  ∈ {0.0, 1.0}，为 signal 的可成交滞后一期实现
        mom      : float64（附加列，预热期 NaN）
  int8 无 NaN 哨兵，预热 20 bar 的 signal 记 0；用 mom 的 NaN 区分
  「尚未预热」与「真实空仓」，避免下游把预热段误计入空仓归因。

参数（全部经 **params 透传，缺省即契约缺省）
    window=20  commission=0.0003  slippage=0.001  stamp_tax=0.0005
成本口径（契约 §4）：买入价 = open*(1+slippage)，卖出价 = open*(1-slippage)；
  佣金双向万三、印花税千五单边；成本不进入 signal / position，仅旁挂 attrs。

审查意见对照表（编号 | 处理方式 | 位置与说明）
P01 | 不适用 | 与强制入口契约正面冲突：signal 必须 int8、position 必须 float64（探针逐列校验
      dtype，2/2 列类型与之相反）。契约 §5 的 signal float64+NaN / position int8 无法同时满足
      int8 宿主语义（int8 取值域 {0,1}，写入 NaN 必然被强转为 -128 或抛错）。按「强制契约优先」，
      预热期 signal=0、position=0.0，并用 mom 的 NaN（前 window 行全 NaN）承载预热语义，
      残余影响：仅看 signal 列的下游无法区分预热段与真实空仓，需要读 mom 或行号。
P02 | 已修复 | _check_df：窗口前先判 None/空表，len(df)==0 → ValueError，删除初稿 _empty_result
      全 0 兜底。与初稿文件头自述「空表返回同结构空表」冲突，按契约 §5「df 为空 → ValueError、
      不静默降级」执行；缺列判定同出口，两条非法路径不再分叉。
P03 | 已修复 | _compute_one：删除 close 的 ffill 预填，动量分子分母直接取原始 close，
      分母 base.mask(base <= 0) 归 NaN；停牌日 mom 为 NaN 而非伪 0.0，缺失处理只由
      sig_s.ffill(limit=window) 承担，20 bar 内沿用上一有效信号、超限回落 0，不再被伪零动量强制平仓。
P04 | 已修复 | _flat_board 严格按 high==low 且 open==high 且 close==high（1e-12 相对容差）判定，
      命中即当日不可成交、仓位保持原方向，待下一可成交 bar 才落单。与审查意见的方向敏感版本
      （涨停禁买 / 跌停禁卖）冲突：合成探针里「与前收同价的一字板」不构成涨停也不构成跌停，
      方向敏感版会漏判并照常建仓，故按契约 §4「一字板不可成交 → 顺延」的更强口径改为方向无关阻塞。
      残余：一字涨停日同样禁止卖出、一字跌停日同样禁止买入，相对 §4 的方向性表述偏保守（不漏单、可能少成交）。
P05 | 已修复 | 必需列扩为 open/high/low/close/volume（契约 §1 全字段，volume 仅值可空），
      任一缺失 → ValueError；删除初稿「high/low 缺失则一字板判定整体返回全 False」的静默降级分支。
P06 | 已修复 | run_strategy 末尾按契约 §4 逐笔口径计算 cost_rate 并写入 out.attrs
      （total_cost 为其和），不新增列以守住 §5 两列契约。残余：佣金「单笔最低 5 元」需资金规模，
      本函数不接收 capital，故按纯比例计提，小额账户实际成本被低估（单笔 <1.67 万元时偏低）。
本轮返修 | 已修复 | ① _as_int 显式 isinstance(str) → TypeError，且 float() 转换失败一律抛 TypeError，
      不再让 '20' 这类字符串被静默整数化；② 一字板阻塞去掉方向判定的漏判分支（见 P04）。

自检说明：完整契约验收（signal 取值、不改入参、空表、一字板顺延、面板一致性）由
check_code --level run 的 suite 探针承担，本文件 __main__ 只跑一遍合成数据打印形状。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_TOL = 1e-12
_REQUIRED = ("open", "high", "low", "close", "volume")
_DEFAULTS = {
    "window": 20,
    "commission": 0.0003,
    "slippage": 0.001,
    "stamp_tax": 0.0005,
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(s) -> np.ndarray:
    """任意列 → float64 ndarray（copy=True，不改调用方数据）。"""
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64", copy=True)


def _eq(a, b) -> np.ndarray:
    """带相对容差的近似相等（浮点价格比较，容差 1e-12）。"""
    scale = np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
    return np.abs(a - b) <= _TOL * scale


def _flat_board(o, h, l, c) -> np.ndarray:
    """一字板：high == low 且 open == high 且 close == high（1e-12 相对容差）。

    三个条件必须同时成立；任一价格缺失（NaN）即判非一字板，交给停牌分支处理。
    """
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    return finite & _eq(h, l) & _eq(o, h) & _eq(c, h)


# --------------------------------------------------------------------------- #
# 面板分组键 / 日期键
# --------------------------------------------------------------------------- #
def _group_key(df):
    """symbol 列 > MultiIndex 首个非日期层；None 表示单标的整表同组。"""
    if "symbol" in df.columns:
        return np.asarray(df["symbol"].to_numpy(copy=True))
    if isinstance(df.index, pd.MultiIndex):
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if not isinstance(lev, pd.DatetimeIndex):
                return np.asarray(lev.to_numpy(copy=True))
    return None


def _date_key(df):
    """组内稳定排序用的日期键；取不到返回 None（视为已按时间排序）。"""
    if "date" in df.columns:
        vals = df["date"].to_numpy(copy=True)
    elif isinstance(df.index, pd.DatetimeIndex):
        return df.index.to_numpy(copy=True)
    elif isinstance(df.index, pd.MultiIndex):
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if isinstance(lev, pd.DatetimeIndex):
                return lev.to_numpy(copy=True)
        return None
    else:
        vals = df.index.to_numpy(copy=True)

    try:
        dt = pd.to_datetime(pd.Series(vals), errors="coerce")
        out = dt.to_numpy(dtype="datetime64[ns]", copy=True)
    except (TypeError, ValueError):
        return None
    if out.size and bool(np.isnat(out).all()):
        return None
    return out


# --------------------------------------------------------------------------- #
# 单标的计算（纯向量化）
# --------------------------------------------------------------------------- #
def _compute_one(sub, window):
    """返回 (signal int8, position float64, mom float64)，顺序与 sub 行序一致。"""
    n = len(sub)
    o = _num(sub["open"])
    h = _num(sub["high"])
    l = _num(sub["low"])
    c = _num(sub["close"])
    v = _num(sub["volume"])
    fin = np.isfinite

    # 停牌：OHLC 全 NaN 或 volume == 0（契约 §1）
    suspended = (~fin(o) & ~fin(h) & ~fin(l) & ~fin(c)) | (fin(v) & (v == 0.0))

    # 动量：close_t / close_{t-window} - 1；P03 —— 不对 close 做 ffill 预填，
    # 停牌日分子分母同为 NaN，mom 保持 NaN，交由信号层 ffill 处理。
    cs = pd.Series(c, dtype="float64")
    base = cs.shift(window)
    mom_s = cs / base.where(base > 0.0) - 1.0
    mom = mom_s.to_numpy(dtype="float64", copy=True)

    # 信号：mom > 0 → 1；mom <= 0（零不算正）→ 0；NaN 留给下一层
    valid = mom_s.notna().to_numpy(copy=True)
    raw = pd.Series(np.where(valid, (mom > 0.0).astype("float64"), np.nan), dtype="float64")
    # 缺失处理 ffill_limit_window_then_drop：窗口内沿用上一有效值，超限作废（归 0 空仓）
    sig_s = raw.ffill(limit=window).fillna(0.0).clip(0.0, 1.0)
    signal = sig_s.to_numpy(dtype="float64", copy=True)
    signal[:window] = 0.0

    # 执行：t 日收盘出信号 → t+1 开盘成交，position 恒滞后一 bar（A 股 T+1 天然满足）
    target = np.concatenate((np.zeros(1, dtype="float64"), signal[:-1]))

    # 可成交性：停牌 或 一字板（P04）→ 当日不可成交，不落单、仓位保持原方向
    flat = _flat_board(o, h, l, c)
    blocked = suspended | flat

    # 顺延：不可成交日写 NaN，由 ffill 保持原仓；下一可成交 bar 才写入待执行目标仓位。
    # 「信号保持原方向、不顺延过期作废」由 target 本身携带的上一有效 signal 保证。
    exec_val = np.where(blocked, np.nan, target)
    pos_s = pd.Series(exec_val, dtype="float64").ffill().fillna(0.0).clip(0.0, 1.0)
    position = pos_s.to_numpy(dtype="float64", copy=True)
    position[:window] = 0.0

    return signal.astype(np.int8, copy=True), position, mom


# --------------------------------------------------------------------------- #
# 参数与入参校验
# --------------------------------------------------------------------------- #
def _check_df(df):
    if not isinstance(df, pd.DataFrame):
        raise ValueError("df 必须是 pandas.DataFrame")
    if len(df) == 0:
        raise ValueError("df 为空，无法计算动量（契约 §5：不静默降级、不返回全 0 兜底）")
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("df 缺少必需列: %s" % ",".join(missing))


def _as_int(val, name):
    """正整数参数：字符串/不可转换类型 → TypeError；非整数或 <1 → ValueError。

    显式拒绝 str 而非交给 float() 兜住：'20' 能被 float 静默整数化成 20，
    会让非法入参悄悄跑通（本轮返修项 ①），故先做类型判定再转换。
    """
    if isinstance(val, bool):
        raise ValueError("%s 必须为正整数（bool 不接受）" % name)
    if isinstance(val, str) or isinstance(val, (bytes, bytearray)):
        raise TypeError("%s 必须为整数，不接受字符串 %r" % (name, val))
    try:
        f = float(val)
    except (TypeError, ValueError) as exc:
        raise TypeError("%s 必须为整数，收到类型 %s" % (name, type(val).__name__)) from exc
    if not np.isfinite(f) or f != int(f):
        raise ValueError("%s 必须为正整数" % name)
    if int(f) < 1:
        raise ValueError("%s 必须 >= 1" % name)
    return int(f)


def _as_nonneg(val, name):
    try:
        f = float(val)
    except (TypeError, ValueError):
        raise ValueError("%s 必须为数值" % name)
    if not np.isfinite(f) or f < 0.0:
        raise ValueError("%s 不能为负" % name)
    return f


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_strategy(df, **params):
    """20 日动量：mom > 0 满仓、mom <= 0 空仓；t 收盘出信号、t+1 开盘成交。

    停牌与一字板当日不可成交，顺延至下一可成交 bar；成本仅旁挂 attrs。
    """
    _check_df(df)

    window = _as_int(params.get("window", _DEFAULTS["window"]), "window")
    commission = _as_nonneg(params.get("commission", _DEFAULTS["commission"]), "commission")
    slippage = _as_nonneg(params.get("slippage", _DEFAULTS["slippage"]), "slippage")
    stamp_tax = _as_nonneg(params.get("stamp_tax", _DEFAULTS["stamp_tax"]), "stamp_tax")

    n = len(df)
    sig = np.zeros(n, dtype=np.int8)
    pos = np.zeros(n, dtype=np.float64)
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

        # 组内按日期稳定排序，重复日期保持原始相对顺序
        if dates is None:
            order = np.arange(idx.size, dtype=np.int64)
        else:
            order = np.argsort(dates[idx], kind="mergesort")

        take = idx[order]
        inv = np.empty(order.size, dtype=np.int64)
        inv[order] = np.arange(order.size, dtype=np.int64)

        s_sorted, p_sorted, m_sorted = _compute_one(df.iloc[take], window)

        # 逆置换回组内原始行序，再 scatter 到全表绝对行号，乱序面板不错位
        sig[idx] = s_sorted[inv]
        pos[idx] = p_sorted[inv]
        mom_out[idx] = m_sorted[inv]

    out = pd.DataFrame(index=df.index)
    out["signal"] = pd.Series(sig, index=df.index, dtype="int8")
    out["position"] = pd.Series(pos, index=df.index, dtype="float64")
    out["mom"] = pd.Series(mom_out, index=df.index, dtype="float64")

    # P06：成本按契约 §4 逐笔口径旁挂 attrs（比例计提，不新增列）
    turn = np.diff(np.concatenate((np.zeros(1, dtype="float64"), pos)))
    rate = np.zeros(n, dtype="float64")
    buy = turn > 0.0
    sell = turn < 0.0
    rate[buy] = turn[buy] * (slippage + commission)
    rate[sell] = -turn[sell] * (slippage + commission + stamp_tax)
    out.attrs["cost_rate"] = pd.Series(rate, index=df.index)
    out.attrs["total_cost"] = float(rate.sum())
    out.attrs["params"] = {
        "window": window,
        "commission": commission,
        "slippage": slippage,
        "stamp_tax": stamp_tax,
    }
    return out


if __name__ == "__main__":
    rng = np.random.default_rng(20261021)
    nrow = 60
    close = 10.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, nrow))
    op = close * (1.0 + rng.normal(0.0, 0.004, nrow))
    demo = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-02", periods=nrow, freq="B"),
            "open": op,
            "high": np.maximum(op, close) * 1.005,
            "low": np.minimum(op, close) * 0.995,
            "close": close,
            "volume": rng.integers(1000, 5000, nrow).astype("float64"),
        }
    ).set_index("date")
    demo.loc[demo.index[30], ["open", "high", "low", "close"]] = np.nan  # 停牌
    demo.loc[demo.index[41], ["open", "high", "low", "close"]] = close[41]  # 一字板
    try:
        res = run_strategy(demo)
        print("returncode=0", "shape=%s" % (res.shape,), "dtypes=%s" % res.dtypes.astype(str).to_dict())
        print("total_cost=%.6f" % res.attrs["total_cost"])
    except Exception as exc:  # noqa: BLE001
        print("returncode=1", repr(exc))
