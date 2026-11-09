```json
{
  "summary": "运行时预检全绿（smoke/causal/timing/panel/shape 均 ok），无前视与面板错位；但代码在 docstring 中自行宣布「强制入口契约优先于契约表格」，据此把契约 §5 的 signal=float64+预热 NaN 与 position=int8 两列 dtype 反转、把空表 ValueError 改成静默返回空表、把「NaN 段信号作废、不产生调仓指令」实现为 fillna(0) 强制平仓，并在数据契约层面把 high/low/volume 降级为非必需列——四处均为对唯一真值基准的单方面豁免，判 block。",
  "verdict": "block",
  "actionable_fixes": [
    {
      "id": "P01",
      "severity": "阻断",
      "target_function": "run_strategy",
      "line_hint": "L319,L412-L413",
      "root_cause": "返回契约被代码头单方面推翻。契约 §5 明写 signal 为 float64、预热期为 NaN，position 为 int8 {0,1}；而 L319 `signal_out = np.where(warmup, 0, target).astype(np.int8, copy=True)` 输出 int8 且把预热期压成 0，L412 `pd.Series(sig_out, index=df.index, dtype=\"int8\")`、L413 `pd.Series(pos_out, index=df.index, dtype=\"float64\")` 两列 dtype 与契约表完全对调。int8 无法承载 NaN，于是「预热期未知」被静默降级为「预热期明确空仓」，下游任何以 signal.isna() 识别预热段/作废段的代码都会把前 20 根 bar 当成有效空仓信号计入统计，且 position 为 float64 使下游 `position.astype(int8)` 之外的一切 int 假设失效（例如 `position==1` 的索引/内存布局与列 dtype 断言）。",
      "exact_code_change": "L319 改为 `signal_out = np.where(warmup, np.nan, target).astype(\"float64\", copy=True)`；L364 改为 `sig_out = np.full(n, np.nan, dtype=np.float64)`；L412-413 改为 `out[\"signal\"] = pd.Series(sig_out, index=df.index, dtype=\"float64\")` 与 `out[\"position\"] = pd.Series(pos_out.astype(np.int8), index=df.index, dtype=\"int8\")`；同时删除 docstring 第 129-133 行「以强制入口契约为准」的口径裁决段落，改为「dtype 与预热期取值严格按契约 §5」。"
    },
    {
      "id": "P02",
      "severity": "高危",
      "target_function": "run_strategy",
      "line_hint": "L361-L362",
      "root_cause": "静默降级。契约 §5 末句「`df` 为空或缺少必需列 → `ValueError`；不静默降级、不返回全 0 兜底」，而 L361-362 `if n == 0: return _empty_result(df)` 把空输入变成一张合法的零值结果表（_empty_result 于 L197-L207 返回 signal/position/mom 全 0 的结构）。调用方拿到 columns 与 dtype 都正确、行数为 0 的 DataFrame，无法区分「数据为空」与「策略判定全空仓」，回测框架会把该标的静默跳过而非报错，属典型不报错失败路径。",
      "exact_code_change": "L361-362 替换为 `if n == 0:\n        raise ValueError(\"df 为空：契约 §5 要求抛 ValueError，不得返回空结果兜底\")`，并删除 L197-L207 的 `_empty_result` 函数及 docstring 中 L132「空表（列齐全）→ 返回同结构空 DataFrame，不 raise」的说明。"
    },
    {
      "id": "P03",
      "severity": "高危",
      "target_function": "run_strategy",
      "line_hint": "L301",
      "root_cause": "NaN 传染/作废语义被实现成强制平仓。契约 §3 规定 `ffill_limit_window_then_drop`：中间 NaN 在 20 个 bar 内前值填充，「超出则该段信号作废、不产生调仓指令」。L299 `sig_s = sig_s.ffill(limit=window)` 之后 L301 `target = sig_s.fillna(0.0).clip(0.0, 1.0)` 把残留 NaN 一律收敛为 0，再由 L305 `target_prev = ...shift(1)`、L311 `exec_sig = target_prev.where(~blocked.to_numpy())`、L314 `position = exec_sig.ffill()` 传导为可执行的清仓目标。复现：某标的实际信号为 1，随后连续停牌 25 个交易日 → close_ff 的 ffill(limit=20) 于第 21 根耗尽 → base/mom 全 NaN → sig_s 的 ffill(limit=20) 同样耗尽 → L301 得 target=0 → 复牌首日 position 由 1 直接归 0。契约要求「不作废指令、只作废该段信号」，实际得到一条凭空产生的卖出指令（并按滑点 0.1%+佣金+印花税扣减），净值被系统性侵蚀且无任何告警。",
      "exact_code_change": "L301 改为 `target = sig_s.ffill().fillna(0.0).clip(0.0, 1.0).to_numpy(dtype=\"float64\", copy=True)`（即在 20 根限次前填之后再做一次不限次数的 ffill：超限的作废段沿用上一有效目标仓位、不产生新指令，仅真正的预热期段仍为 NaN 并落 0），并在该行上方加注释 `# 契约 §3：作废段 = 不产生调仓指令，故沿用上一有效 target，禁止 fillna(0) 强制平仓`。"
    },
    {
      "id": "P04",
      "severity": "中危",
      "target_function": "run_strategy",
      "line_hint": "L308",
      "root_cause": "涨跌停可成交性未区分方向，成交假设与契约 §4 不符。契约 §4 明写「t+1 开盘一字涨停不可买入、一字跌停不可卖出」，是方向相关的双边非对称约束；而 L308 `blocked = pd.Series(suspended | _flat_board(openp, high, low, close), dtype=\"bool\")` 把 open==high==low==close 全等日一律视为不可成交，L311 `exec_sig = target_prev.where(~blocked.to_numpy())` 对买卖同向封死。复现：持仓 1、t 日 mom 转负、t+1 为一字涨停 → 涨停封单方向是买盘，卖出完全可成交，代码却把该行置 NaN 并由 L314 ffill 维持满仓，把 t+1 之后的高位回撤全部计入净值，退出时点被系统性推迟。",
      "exact_code_change": "L308-L311 替换为方向感知版本：\n```python\n    flat = _flat_board(openp, high, low, close)\n    limit_up = flat & _eq(openp, high)      # 一字涨停：不可买入\n    limit_down = flat & _eq(openp, low)     # 一字跌停：不可卖出\n    want_long = pd.Series(target_prev, dtype=\"float64\").fillna(0.0).to_numpy() > 0.5\n    tp = pd.Series(target_prev, dtype=\"float64\").to_numpy()\n    exec_sig = pd.Series(\n        np.where(want_long, np.where(suspended | limit_up, np.nan, tp),\n                            np.where(suspended | limit_down, np.nan, tp)),\n        dtype=\"float64\", index=df.index,\n    )\n```\n（position 仍由下一行 `exec_sig.ffill().fillna(0.0)` 生成，逻辑不变。）"
    },
    {
      "id": "P05",
      "severity": "中危",
      "target_function": "run_strategy",
      "line_hint": "L156",
      "root_cause": "数据契约必需列被私自收窄，缺失 high/low/volume 时静默降级。契约 §1 表格中 high、low、volume 均为 schema 列（high/low 标「否」可空），§5 要求「缺少必需列 → ValueError」；但 L156 `_CORE_COLS = (\"open\", \"close\")` 只要求两列，缺 high/low 时 L267-L268 传 None，L190-L191 `_flat_board` 直接 `return np.zeros(...)`，涨跌停不可成交项整体失效且无异常无告警；缺 volume 时 L278-L279 也退化为仅按 OHLC 全 NaN 判停牌，volume==0 的停牌日被当成可成交日。同一份代码在不同数据源下跑出两套成交假设，回测结果不可比且无从察觉。",
      "exact_code_change": "L156 改为 `_CORE_COLS = (\"open\", \"high\", \"low\", \"close\", \"volume\")`（volume 列必须存在，值可为 NaN），并删除 L267-L269 中 `if \"high\" in sub.columns else None` 一类降级分支，改为 `high = _num(sub[\"high\"])`、`low = _num(sub[\"low\"])`、`vol = _num(sub[\"volume\"])`；同步删除 L134「high/low/volume 缺失时：一字板项 / 成交量停牌项不生效」的说明段落。"
    }
  ]
}
```
