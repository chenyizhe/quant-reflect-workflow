# 节点任务书 · 初稿生成器 (generator) · 候选 1

- run: `20261022-165330`
- 模型档位: **lite**（来源: models.fast，调用子代理时必须传 model="lite"）
- 上游节点: spec

## 本条候选的实现范式（第 1/2 条）

**范式 A · 向量化优先**：能用 pandas/numpy 原生运算解决的，一律不写 Python 循环；追求执行效率与代码简洁。

严格按这个范式写，不要写成通用版；其余候选会覆盖别的范式。

## 系统指令

你是一个熟练的 Python 量化开发人员。请严格依据「契约」编写代码初稿。
只输出纯净代码，不要输出解释性文字。

强制入口契约（不满足直接判不通过）：
- 必须实现模块级函数 `run_strategy(df, **params) -> pd.DataFrame`
- 返回的 DataFrame 必须含 `signal`（int8，取值 {0, 1}）与
  `position`（float64，取值 {0.0, 1.0}），以及契约规定的附加列
- `position` 必须是 `signal` 的滞后：t 日收盘出信号，t+1 才可能建仓
- 预热期内 signal/position 为 0

运行时硬约束：
- 禁止 import os / subprocess / shutil / socket / pathlib / requests 等
- 禁止调用 open / eval / exec / compile
- 禁止链式赋值（df[df.a > 0]["b"] = ...）
- 禁止对 to_numpy() / .values 返回的只读数组原地赋值；
  需要修改时先 `.copy()`，或用 np.where 生成新数组
- 除法与 log 必须做零值/定义域保护

## Flash 必用标准防坑算子（照抄，不要自己重写）

1. 面板分组与原序回填（兼容 MultiIndex 与含重复日期的 symbol 列，
   禁止直接 groupby + reindex 后丢行序）：
   提取分组键 -> 按 `np.flatnonzero(gid == k)` 切片单标的 -> 组内稳定排序算完
   -> 按逆置换 `inv` 填回预分配数组，最后用原始 index 重建 DataFrame。

2. 停牌列安全布尔解析（防字符串 "False" 被 `astype(bool)` 判为 True）：
   ```python
   def _to_bool(s):
       if s.dtype == bool:
           return s.to_numpy()
       txt = s.astype("string").str.strip().str.lower()
       return txt.isin(["true", "1", "1.0", "t", "y", "yes"]).to_numpy()
   ```

3. 一字板严格判定：必须同时满足 `high == low` 且 `open == high` 且 `close == high`
   （带 1e-12 相对容差）。缺 high/low 列则该项不生效，不要伪造。

4. 估值价连续性：算 open-to-open 收益前，先对执行价做 `ffill()` 得到估值价，
   否则停牌日的 NaN 会把整条 ret 序列撕成 NaN。

5. Pandas 3.0 兼容：一切 `to_numpy()` 后需要修改的，必须显式 `copy=True`；
   严禁原地改只读视图，也不要用链式赋值。

6. 空表输入返回空 DataFrame（列名与 dtype 齐全），不要 raise。


## 用户输入

## 需求
写一个20日动量策略：动量为正时全仓持有、为负时空仓，A股T+1，停牌日不可成交，单边佣金万三、滑点千一

## 契约（唯一真值基准，逐条遵守）
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

契约未覆盖的细节按行业惯例处理，并在注释中标注你的选择。


## 交付要求

- `draft_code` → 写入 `/Volumes/plugHD/workbuddy/WorkBuddy/2026-10-21-15-17-31/quant-reflect-workflow/runs/20261022-165330/candidates/generator.c1.py`

- 只写上面列出的产物文件，不创建其他文件，不修改已有文件
- 产物必须是可直接使用的完整内容，不允许 `...` 或“省略”占位
- 不要执行代码，不要跑测试
- 完成后返回一句话说明，不要回贴全文
