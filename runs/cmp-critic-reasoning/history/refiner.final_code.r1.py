"""20 日动量策略（A 股 T+1）· refiner 定稿（返修 1）。唯一真值基准：契约 §1-§5。

入口：
    out = run_strategy(df, window=20, commission=0.0003, slippage=0.001, stamp_tax=0.0005)
返回列：signal(int8, {0,1}) / position(float64, {0.0,1.0}) / mom / cost / trade_price
时序：t 日收盘算信号，t+1 开盘成交；position 是 signal 的滞后（position[t] 来自 signal[t-1]），
      A 股 T+1 由此天然满足（t+1 买入的份额最早 t+2 可卖，最短持仓 1 个交易日）。

审查意见对照表（8 条，逐条不遗漏）
编号 | 处理方式 | 位置与说明
P01 | 不适用 | 强制入口契约规定 signal=int8、position=float64，而 int8 无 NaN 位，故预热 20 根只能 signal=0、position=0.0。改回 float64 signal + int8 position 会同时违反入口 dtype 硬约束（float64 signal 与 int8 position 与契约相反），且下游 astype("int8") 会把预热 NaN 静默截成 0、把 20 个 NaN 变成 20 个"真空仓观测"，反而放大 P01 指出的稀释偏差；两种 dtype 无法同时满足，按"入口签名优先"裁决，本文件仅此一处偏离审查建议。
P02 | 已修复 | run_strategy 空表分支改为 raise ValueError("df 不能为空")，删除 _empty_result 顺延兜底，与缺列/负成本分支同一标准（契约 §5「不静默降级、不返回全 0 兜底」）；上游数据断流立即暴露，不再伪装成"今日无信号"。
P03 | 已修复 | _compute_one 中 sig_s.ffill() 去掉 limit=window：超限 NaN 段（mom 整段作废）冻结于上一有效信号 = 不产生调仓指令，不再被 fillna(0.0) 打成清仓；fillna(0.0) 仅收敛前置预热 NaN，作废段与预热段彻底分离。
P04 | 已修复 | 返修：上一版 _board_dir 的涨跌幅方向与 ±9.5% 门限被 suite 判定为缺陷（一字板 bar 因涨幅不足门限而漏判、仍建仓）。现按契约实现 _flat_board：high==low 且 open==high 且 close==high（_eq，相对容差 1e-12）即为不可成交日，命中当日 blocked 掩码为真、仓位冻结（where(~blocked).ffill()），信号顺延至下一可成交 bar；掩码为对称口径（涨停买不进与跌停卖不出同属"当日不可成交"）。
P05 | 已修复 | 三成本参数落地为附加列 cost / trade_price：cost = 佣金 + (卖出单边)印花税 + 滑点，按成交价即时扣减的费率口径；trade_price = open×(1±slippage)。二者不进入 signal/position（契约 §4 末条）。单笔最低佣金 5 元需资金规模，本层无资金列，故 cost 为纯费率口径（残余：小额账户成本被低估，属已知偏差）。
P06 | 已修复 | 分组键 NaN 先填哨兵 "<NA_SYMBOL>" 再 factorize，NA 行自成一组正常进入 _compute_one，不再被编成 -1 后整组静默丢弃；并断言 gid>=0，任何无法识别的码立即 raise，不再把数据缺陷转译成"全程空仓"。
P07 | 已修复 | suspended 增加 ~isfinite(open) | ~isfinite(close)：执行价基准 open 非有限即不可定价、不可成交，按停牌顺延；"缺行被上游补成 NaN 行"这一主要停牌形态因此被真正覆盖，NaN 不会沿 trade_price→净值链路传染。
P08 | 已修复 | 组内排序前用 duplicated(date) 校验，重复交易日 raise ValueError（契约 §1 date 唯一），不再以"mergesort 稳定排序"把违约降级为实现细节；避免 20 日窗口跨两个同日 bar 导致 mom 分母取 t-19。

范式与运行时约束：
  - 除"按标的遍历分组"这一层骨架外，全部运算为 pandas/numpy 向量化，无逐 bar 循环；
  - 不 import os/subprocess/shutil/socket/pathlib/requests，不使用 open/eval/exec/compile；
  - to_numpy 一律 copy=True，无链式赋值、不原地改只读视图；除法前 mask 掉 <=0 分母。

已知残余风险（不影响契约列）：
  - 一字板按四价合一判定（1e-12 相对容差），不区分涨跌停幅度：真实行情中全天零振幅、
    成交正常的横盘 bar 亦满足四价合一，会被一并视为不可成交而顺延一日（契约 §4 只要求
    涨跌停顺延，属偏保守实现，不会产生虚假成交）；
  - 涨停日卖单、跌停日买单在现实中可成交，本实现对二者同样冻结，故离场/建仓可能
    晚 1 个 bar；
  - 连续多日被封锁时，prev_pos 取"未受可成交性约束的理想仓位"作比较基准，个别极端链
    （封锁日恰好连续改变方向）下顺延起点可能提前或推后 1 个 bar；
  - 复权口径 UNKNOWN，不做复权推断，仅要求输入同口径连续序列 [待确认]。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_TOL = 1e-12                     # 四价合一的相对容差
_CORE_COLS = ("open", "close")   # 缺失即 ValueError；high/low/volume 可缺省降级
_NA_KEY = "<NA_SYMBOL>"          # 缺失分组键哨兵，防止 factorize 编成 -1 后被丢弃


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _num(s):
    """列 → float64 ndarray；非数值转 NaN；copy=True，不动入参视图。"""
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64", copy=True)


def _eq(a, b):
    """带相对容差的近似相等。"""
    scale = np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
    return np.abs(a - b) <= _TOL * scale


def _flat_board(openp, high, low, close):
    """一字板：high==low 且 open==high 且 close==high（相对容差 1e-12）。

    三条件同时成立 → 当日无价格区间、单一价位成交，按"不可成交"处理：
    涨停买不进、跌停卖不出，当日不计成交、仓位冻结，信号顺延至下一可成交 bar。
    缺 high/low 时返回全 False，不伪造数据、不擅自封锁。
    """
    z = np.zeros(openp.shape[0], dtype=bool)
    if high is None or low is None:
        return z
    ok = (np.isfinite(openp) & np.isfinite(high)
          & np.isfinite(low) & np.isfinite(close))
    flat = ok & _eq(high, low) & _eq(openp, high) & _eq(close, high)
    return flat & ok


def _group_key(df):
    """分组键；None 表示单标的（整表同组）。优先级：symbol 列 > MultiIndex 非日期层。"""
    if "symbol" in df.columns:
        return np.asarray(df["symbol"].to_numpy(copy=True))
    if isinstance(df.index, pd.MultiIndex) and df.index.nlevels >= 2:
        for lv in range(df.index.nlevels):
            lev = df.index.get_level_values(lv)
            if not isinstance(lev, pd.DatetimeIndex):
                return np.asarray(lev.to_numpy(copy=True))
    return None


def _date_key(df):
    """组内排序用的日期键；取不到返回 None（视为已按日期排好）。"""
    idx = df.index
    if "date" in df.columns:
        try:
            return pd.to_datetime(df["date"], errors="coerce").to_numpy()
        except (TypeError, ValueError):
            return None
    if isinstance(idx, pd.DatetimeIndex):
        return idx.to_numpy(copy=True)
    if isinstance(idx, pd.MultiIndex):
        for lv in range(idx.nlevels):
            lev = idx.get_level_values(lv)
            if isinstance(lev, pd.DatetimeIndex):
                return lev.to_numpy(copy=True)
        return None
    if isinstance(idx, pd.RangeIndex):
        return None
    try:
        return pd.to_datetime(pd.Series(np.asarray(idx)), errors="coerce").to_numpy()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 单标的计算（全向量化）
# --------------------------------------------------------------------------- #
def _compute_one(sub, window, commission, slippage, stamp_tax):
    """返回 (signal int8, position float64, mom, cost, trade_price)，行序与 sub 一致。"""
    n = len(sub)
    close = _num(sub["close"])
    openp = _num(sub["open"])
    high = _num(sub["high"]) if "high" in sub.columns else None
    low = _num(sub["low"]) if "low" in sub.columns else None
    vol = _num(sub["volume"]) if "volume" in sub.columns else None

    # 停牌三条件：缺行（上游补成 NaN 行 → open/close 非有限）/ OHLC 全 NaN / volume==0。
    # P07：open 是执行价基准，非有限即不可定价、不可成交，一并按停牌处理。
    suspended = ~np.isfinite(openp) | ~np.isfinite(close)
    if vol is not None:
        suspended = suspended | (np.isfinite(vol) & (vol == 0.0))

    # 收盘价 ffill 限 window 根：中途停牌不撕裂动量；超限则 mom 为 NaN，交给"作废"分支
    close_s = pd.Series(close, dtype="float64")
    close_ff = close_s.ffill(limit=window)

    # mom_t = close_t / close_{t-window} - 1，分母 <=0/NaN 一律置 NaN（不除零）
    base = close_ff.shift(window)
    base = base.mask(base <= 0.0)
    mom = close_ff.div(base).sub(1.0)

    # 边界：mom > 0 → 1；mom <= 0（零不算正）→ 0；mom 为 NaN 暂留待前填
    valid = mom.notna().to_numpy()
    pos_mask = (mom > 0.0).to_numpy()
    sig_s = pd.Series(np.where(valid, np.where(pos_mask, 1.0, 0.0), np.nan),
                      dtype="float64")

    # nan_policy = ffill_limit_window_then_drop：window 内前填，超限段信号作废。
    # P03：作废 = 冻结于上一有效信号（不发调仓单），故 ffill 不带 limit。
    sig_s = sig_s.ffill()
    # fillna(0.0) 只收敛前置预热 NaN（序列头部），中段 NaN 已被 ffill 填满
    target = sig_s.fillna(0.0).clip(0.0, 1.0).to_numpy(dtype="float64", copy=True)

    # ---- 执行：t 收盘出信号 → t+1 开盘成交，整体滞后 1 期 ----
    target_prev = pd.Series(target, dtype="float64").shift(1)

    # 不可成交日：停牌 或 一字板（四价合一）→ 当日不成交，信号顺延至下一可成交 bar。
    # 掩码为对称口径：涨停买不进、跌停卖不出都表现为"当日无成交可能"。
    blocked = pd.Series(suspended | _flat_board(openp, high, low, close),
                        dtype="bool").to_numpy()

    # 不可成交日留 NaN，ffill = 顺延期间仓位保持原方向（不顺延过期作废）；
    # 下一可成交日仍按 target_prev 上的原目标补做。
    position = target_prev.where(~blocked).ffill().fillna(0.0).clip(0.0, 1.0)
    pos_arr = position.to_numpy(dtype="float64", copy=True)

    # ---- 成本（P05）：仅在仓位变化日发生，费率口径，按成交价即时扣减 ----
    prev = np.concatenate((np.zeros(1, dtype="float64"), pos_arr[:-1]))
    delta = pos_arr - prev
    trade = np.abs(delta) > 0.0
    buy = delta > 0.0
    fill_px = np.where(buy, openp * (1.0 + slippage), openp * (1.0 - slippage))
    fill_px = np.where(trade, fill_px, np.nan)
    cost = np.where(trade, commission + np.where(buy, 0.0, stamp_tax) + slippage, 0.0)

    # ---- 预热 20 根：signal=0（int8 无 NaN 位）、position=0.0，见对照表 P01 ----
    warm = np.arange(n) < window
    signal_out = np.where(warm, 0, target).astype("int8", copy=True)
    position_out = np.where(warm, 0.0, pos_arr).astype("float64", copy=True)
    return (signal_out, position_out,
            mom.to_numpy(dtype="float64", copy=True), cost, fill_px)


# --------------------------------------------------------------------------- #
# 参数校验（非法值直接 raise，不静默降级）
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
    for name, val in (("commission", commission), ("slippage", slippage),
                      ("stamp_tax", stamp_tax)):
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
    """20 日动量：mom>0 满仓、mom<=0 空仓；t 收盘出信号、t+1 开盘成交。

    position 为 signal 的滞后一档，T+1 约束由该滞后天然满足；
    停牌或一字板当日不可成交，信号顺延至下一可成交 bar。
    commission / slippage / stamp_tax 不进入 signal / position，
    仅以 cost / trade_price 附加列输出，供下游净值评估。
    """
    window = _check_args(df, window, commission, slippage, stamp_tax)

    n = len(df)
    if n == 0:
        raise ValueError("df 不能为空")          # P02：不返回全 0 兜底

    sig_out = np.zeros(n, dtype="int8")
    pos_out = np.zeros(n, dtype="float64")
    mom_out = np.full(n, np.nan, dtype="float64")
    cost_out = np.zeros(n, dtype="float64")
    px_out = np.full(n, np.nan, dtype="float64")

    keys = _group_key(df)
    dates = _date_key(df)

    if keys is None:
        gid = np.zeros(n, dtype=np.int64)
        ngroups = 1
    else:
        key_s = pd.Series(np.asarray(keys), dtype="object")
        key_s = key_s.where(key_s.notna(), _NA_KEY)     # P06：NA 自成一组，不丢行
        codes, uniques = pd.factorize(key_s, sort=False)
        gid = np.asarray(codes, dtype=np.int64)
        ngroups = int(uniques.size)
        if (gid < 0).any():
            raise ValueError("分组键存在无法识别的取值，拒绝静默丢弃")

    for k in range(ngroups):
        idx = np.flatnonzero(gid == k)                  # 该标的在原始表中的绝对行号
        if idx.size == 0:
            continue

        if dates is None:
            order = np.arange(idx.size, dtype=np.int64)
        else:
            d_grp = dates[idx]
            if pd.Series(d_grp).duplicated().any():     # P08：date 契约唯一性
                raise ValueError("同一标的存在重复交易日，违反契约 date 唯一性")
            order = np.argsort(d_grp, kind="mergesort")

        take = idx[order]                               # 组内日期升序后的绝对行号
        s_s, p_s, m_s, c_s, x_s = _compute_one(
            df.iloc[take], window, commission, slippage, stamp_tax)

        # 结果与 take 一一对应，直接散布回全表预分配数组，index 与行数完全不变
        sig_out[take] = s_s
        pos_out[take] = p_s
        mom_out[take] = m_s
        cost_out[take] = c_s
        px_out[take] = x_s

    out = pd.DataFrame(index=df.index)
    out["signal"] = pd.Series(sig_out, index=df.index, dtype="int8")
    out["position"] = pd.Series(pos_out, index=df.index, dtype="float64")
    out["mom"] = pd.Series(mom_out, index=df.index, dtype="float64")
    out["cost"] = pd.Series(cost_out, index=df.index, dtype="float64")
    out["trade_price"] = pd.Series(px_out, index=df.index, dtype="float64")
    return out


if __name__ == "__main__":
    import sys
    import traceback

    rng = np.random.default_rng(7)
    n = 60
    close = 10.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, n))
    demo = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=n),
        "open": close * (1.0 + rng.normal(0.0, 0.002, n)),
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": rng.integers(100000, 1000000, n).astype("float64"),
    })
    try:
        out = run_strategy(demo)
    except Exception:                                  # noqa: BLE001 - 自检仅打印栈
        traceback.print_exc()
        print("returncode=1")
        sys.exit(1)
    print("returncode=0")
    print("shape=%s" % (out.shape,))
    print("dtypes=%s" % out.dtypes.astype(str).to_dict())
