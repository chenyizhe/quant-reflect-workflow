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
