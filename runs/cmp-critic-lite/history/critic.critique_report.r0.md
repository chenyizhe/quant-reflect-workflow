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
