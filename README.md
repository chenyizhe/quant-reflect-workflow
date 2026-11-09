# 量化代码自我反思流

3 节点线性 DAG：**初稿生成 → 严厉审查 → 重构修复**，变量按序传递，最终产出 `final_code`。

两套运行时共用同一份 `workflow.yaml`：

| runtime | 驱动方式 | 额度 | 能否无人值守 |
|---|---|---|---|
| `builtin`（默认） | `orchestrator.py` 生成节点任务书，由内置模型（子代理）执行 | 走 WorkBuddy 平台额度 | 否，需编排层逐步调度 |
| `api` | `run.py` 直连 provider | 自己的 key / 余额 | 是 |

## 结构

```
quant-reflect-workflow/
├── workflow.yaml        # 单一事实源：节点 / 模型映射 / prompt / 变量绑定
├── orchestrator.py      # builtin 运行时：任务书生成 + 产物回收 + 档位审计
├── workflow_engine.py   # 通用 DAG 引擎：Kahn 拓扑排序 + 变量绑定 + 模板渲染 + api 调用
├── tools/check_code.py  # 产物检查器：语法 / 依赖 / 运行 / 断言 四级分类
├── tools/score_candidates.py  # Best-of-N 评分：check_code 硬门槛 + 四维客观分
├── run.py               # api 运行时 CLI
└── runs/<时间戳>/
    ├── tasks/01_spec.md         # 节点任务书（builtin）
    ├── spec.md                  # 契约（唯一真值基准）
    ├── draft_code.py
    ├── critique_report.md
    ├── final_code.py
    ├── history/                 # 每轮产物归档（自检返修）
    ├── verify/                  # 每轮自检日志
    ├── state.json               # 返修轮次 + 自检结果
    └── variables.json           # 全量变量快照
```

## builtin 运行时（推荐）

```bash
PY=/Users/chenyizhe/.workbuddy/binaries/python/envs/default/bin/python

$PY orchestrator.py init -t "你的策略需求"   # 建 run，生成节点 1 任务书（Best-of-N 时为 N 份）
$PY orchestrator.py status runs/<run>        # 看进度
$PY orchestrator.py step runs/<run> <node>   # 回收产物，跑自检，生成下一节点任务书
$PY orchestrator.py pick runs/<run> <node>   # Best-of-N 选优（有 verdict.json 时自动加权）
$PY orchestrator.py pick runs/<run> <node> --emit  # 只生成语义评选任务书
$PY orchestrator.py final runs/<run>         # 输出 final_code
$PY orchestrator.py audit runs/<run>         # 三路比对档位一致性
```

Python 不碰模型 API，只负责三件事：校验 DAG、渲染节点 prompt、回收产物推进指针。
真正的 LLM 调用发生在编排层——按任务书用内置模型执行。

## v1.7 端到端实测（2026-10-22，run 20261022-132619）

需求：20 日动量策略，A股 T+1，停牌不可成交，单边佣金万三 + 滑点千一。

| 节点 | 产出 | 关键数据 |
|---|---|---|
| spec | 契约 10,173 字 | 明确「t 日收盘算信号、t+1 开盘成交、禁止用 t 日收盘成交」 |
| generator | 3 份正交候选 | 规则分 90 / 91 / 93，**全部过冒烟门槛** |
| ↓ 语义评选 | verdict.json | 语义分 94 / 91 / **58** |
| critic | 19,325 字，9 条 | 1 阻断 + 4 高危 + 2 中危 + 2 低危 |
| refiner | 30,875 字 / 737 行 | 9 条全修，自检 11 项，**1 轮返修后通过** |

### 三个最有价值的发现

**1. 混合评分真的抓住了规则分抓不到的东西。**

三份候选规则分只差 3 分（93/91/90），语义评选直接把 c3 打到 58：

> c3 用 `target[i]` 在第 i 根 bar 开盘成交，而非 `target[i-1]`，构成 1 个 bar 前视：
> 首笔成交落在 i=20 而非 i=21，并把该 bar 的 open→close 收益计入净值（实测虚高约 +2.76%）

终分 = 0.7×93 + 0.3×58 = 82，从第一名掉到第三名，最终选了 c1（91 分）。
**如果只看规则分，会选中一个带 1 bar 前视的实现。**

**2. verify 抓到了 pandas 3.0 的真实兼容问题。**

refiner 首版 11 项自检里 10 项抛同一个错：

```
ValueError: assignment destination is read-only
```

根因是 `Series.to_numpy()` 在 pandas 3.0 返回只读视图，`arr[mask] = ...` 原地赋值必然失败。
返修一轮改了 20 处（14 处 `to_numpy` + 2 处辅助函数 + 统一数组出口加 `copy=True`），
11/11 通过。**这是靠读代码绝对发现不了的**——只有真跑才会炸。

**3. 端到端暴露了三个工具层 bug**（单元测试没覆盖到）：

| 缺陷 | 现象 | 修法 |
|---|---|---|
| audit 漏判 Best-of-N 任务书 | 文件名 `02_generator.c1.md` 匹配不上节点名 `generator`，误判 ✗ | 比对前剥掉 `.cN` 后缀 |
| 冒烟 runner 签名不匹配 | 产物要求 DatetimeIndex，合成数据是 RangeIndex → `skipped` | 合成 df 同时设 `df.index = DatetimeIndex` |
| 探针括号丢失误报 | `(changes > 0).sum()` 被判「全局聚合前视」 | 接收者是 Compare/BoolOp 时跳过 |

### 单轮成本

4 次 reasoning 调用 + 3 次并行 generator + 1 次语义评选 + 1 次返修，约 12 分钟。
builtin 模式零 API 花费。

## v1.9 提速改造（指令 1-2 已落地，3-6 待补）

### 1. 解除 reasoning 死锁，Flash 主导

```yaml
builtin: {profile: lite, lock: false}
models.fast:     {agent_profile: lite,      temperature: 0.2}
models.reasoner: {agent_profile: reasoning}
generator.best_of_n.n: 2      # 从 3 精简，省 33% 初稿耗时
```

`resolve_profile` 的优先级链本来就支持这个（节点 > 模型别名 > builtin > default），
本次只是解开锁并补上模型级别的声明。实测路由：

```
spec      lite
generator lite
critic    reasoning (来源 models.reasoner)
refiner   lite
```

同时修了 `run.py` 的提示文案：`lock=false` 时混合档位是**设计意图**，
原来却打红标「✗ 档位不一致」。

范式 B 合并为「防御与分层解耦」，保证 n=2 时两条候选仍覆盖「重校验」与「重结构」两种取舍。

### 2. 三个真实 bug

**verdict 双重 `.get("scores")`（语义分永久失效）**

```python
# v1.8（错）：先取出 scores，再对 scores 取 "scores" —— 第二次必然是 {}
verdict = json.loads(...).get("scores", {}) or {}
sem_map = verdict.get("scores", {})          # 永远 {}
```

改成保留顶层字典后实测：c1 语义 88 被读到，终分 `sqrt(91×88)=89.5`；
c2 语义 90 但 `has_lookahead_bias=true` → `VETOED / LLM_BIAS_FLAG`。

**`_write_verdict_task` 键名重复拼接**

`survivors[0]['id']` 本身已是 `"generator.c1"`，模板却又拼 `.c1`，
生成 `"generator.c1.c1": 88`，pick 永远匹配不上。已改为直接用完整 id，
并补上 `has_lookahead_bias` 字段与读取端对齐。

**同一份 draft_code 被 check_code 跑 3 次**

pick 的硬门禁刚验证过，step 又跑一遍，precheck 再跑一遍。现在 pick 把结论写进
`state["verify"][node_id]`（`cmd: cached_from_pick_gate`）并把预检文本缓存进
`ctx["precheck_log"]`；step 侧若 `ast_hash` 未变则直接复用：

```
✓ 复用 pick 阶段探针结果（未重复执行 check_code）   # 0.0s
```


### 4. 探针升级：脉冲扰动替代变化点求交集

v1.8 用 `signal` 与 `position` 的变化点是否有交集判断同根成交。这在信号连续翻转、
或停牌顺延让 position 晚于 signal 变化时会误报/漏报。

v1.9 改成**因果测试**：把第 45 根 bar 的 `close` 分别乘以 1.5 和 0.5，
断言 `position[:46]` 完全不变。

```
[TIMING]
timing: 扰动当日收盘价影响了当日及之前的输出 → position[t] 用到了 close[t]
  position: 把第 45 根 close 乘 0.5 后，前 46 行有 1 处变化，首个落点 i=45 （1.0 → 0.0）
```

**关键细节**：只对 `position` 断言，不对 `signal` 断言。
`signal[t]` 本来就该由 `close[t]` 算出，改 close 当然会改 signal——那不是前视。

新增三个探针：

| 探针 | 抓什么 |
|---|---|
| `panel` | 双标的长表面板（重复日期 + 乱序 + 字符串 `"False"`）：跨标的混算、行序错位、布尔误判 |
| `shape` | signal/position 取值集合、ret 含 NaN、是否修改入参 |
| `suite` | 6 项契约验收（非法 window、空表、一字板顺延等），**替代产物自己的 300 行断言** |

`panel` 的一个例外：产物**显式** `raise ValueError("...重复...")` 拒绝面板输入时，
判为 `skip` 而非 fail——单标的策略拒绝面板是合法契约行为，静默算错才不可接受。

### 5. 评分与校验

- `score_candidates` 用 `ThreadPoolExecutor` 并行跑门禁（3 候选 803ms）
- `dim_contract` 只评 `run_strategy`，不再被兄弟函数拉低分母；`**params` 视为可配置，
  参数标注给满分（实测 final_code 契约 20/20）
- `validate_critique` 允许 `verdict=pass` 时 `actionable_fixes` 为空

### 6. 提示词面向 Flash 重构

- `spec` 正文压到 2000 字以内，`assumed_defaults` ≤ 5 条
- `generator` 修正 `signal` 类型冲突（int8 / float64），注入 6 条**防坑算子微模板**
  （面板回填 / 布尔解析 / 一字板判定 / 估值价 ffill / pandas 3.0 copy / 空表）
- `refiner` 禁止块外 Markdown 表格（改写入模块 docstring），`__main__` ≤ 25 行，
  代码预算 250~350 行——完整的契约验收已由 suite 探针承担，不必重复手写

### 7. api 模式补齐（指令 6）

`workflow_engine.run()` 现在：
- `precheck` 真实调用 `check_code`，不再是占位字符串
- 产物落盘后执行 `verify.command`，失败则把日志拼进 prompt 重试，最多 `max_rounds` 轮

### 3. critic 零阻断短路

critic 判定 `verdict=pass` 且无「阻断/高危」级 fix 时，跳过 refiner，
`draft_code` 直接复制为 `final_code`，并把 refiner 标记为
`short_circuit_clean_critic` 完成。

实测：`final_code` 11,416 字（对比 v1.8 的 32,775 字），省掉整个 800 行重写节点。

## v1.8 五项重构

v1.7 留下的 9 个架构缺陷，逐项落地。核心变化：**从"静态猜"改成"跑起来看"**。

### 1. check_code v3：三个动态探针 + 强制入口 + 安全扫描

废弃默认的 AST 启发式，改为 `--level runtime`（默认），一进程跑完三个探针：

| 探针 | 做法 | 抓什么 | 判定 |
|---|---|---|---|
| **因果**（前缀不变性）| 100 行 vs 前 70 行，比前 70 行输出 | `shift(-n)`、无 rolling 的全样本均值、`bfill` | FAIL |
| **时序**（同根检测）| `signal` 与 `position` 的变化点求交集 | **同根 bar 成交**（signal[t] 决定 position[t]）| FAIL |
| **CoW** | `copy_on_write=True` + `chained_assignment='raise'` | pandas 3.0 只读视图原地赋值 | FAIL |

**为什么必须是两个探针**：前缀不变性抓不到 c3。实测 c3 的前 70 行在截断输入下**完全一致**——
它的前视是"用 t 的数据在 t 开盘成交"，不是"用了 t 之后的数据"。只有时序探针能抓：

```
[TIMING]
timing: signal 与 position 在同一根 bar 变化，信号在当期就成交了 → 前视
  4 处同根变化，前几处: i=40: signal 0->1, position 0->1; i=69: signal 1->0, position 1->0
```

强制入口：必须是 `run_strategy(df, **params) -> DataFrame[signal, position]`。
找不到直接 `[CONTRACT]` FAIL，不再靠函数名启发式猜——v1.7 的启发式让 c1 带着 read-only 崩溃
通过了冒烟（因为签名不匹配被跳过，而跳过算通过）。

安全扫描：AST 拦 `os/subprocess/shutil/socket/pathlib/requests` 与 `open/eval/exec/compile`。
`sys` 放行（自检入口需要 `sys.exit` 传退出码）。

### 2. 硬门禁替代线性加权

```
VETO: 探针 FAIL / 语义分 < 65 / LLM 标记 has_lookahead_bias  →  final_score = 0
通过者: final = sqrt(rule_score × semantic_score)             ← 几何平均，惩罚短板
```

实测同一批候选：

| 候选 | 规则分 | 语义分 | v1.7 线性 | v1.8 门禁 |
|---|---|---|---|---|
| c1 | 90 | 94 | 91.2 | **VETOED**（SMOKE: read-only）|
| c2 | 91 | 91 | 91.0 | PASS |
| c3 | **93** | 58 | **82.5** | **VETOED**（TIMING: 1 bar 前视）|

v1.7 里 c3 靠 93 的规则分把总分拉到 82.5，v1.8 直接归零。
更关键的是 **c1 也被否决了**——它在 pandas 3.0 下真实崩溃，v1.7 的冒烟跳过了没发现。

### 3. Critic 前置预检 + JSON 契约

- `precheck`：渲染前先对 `draft_code` 跑一次 `check_code --level runtime`，
  把真实 traceback 注入 `{{precheck_log}}`。critic 从此能看到运行时错误
- critic 输出改为紧凑 JSON（`summary` / `verdict` / `actionable_fixes[]`），
  废除 `##` 标题截取——那个找不到标题会静默返回全文，压缩收益丢失且不报错
- `validate_critique.py` 作为 critic 的 verify：JSON 不合规直接返修，**不静默放行**

实测旧版 Markdown 报告被拒：
```
[CRITIQUE] 报告里没有合法 JSON。Markdown 叙述会被直接拒绝——它无法保证下游结构一致
```

### 4. Spec 结构化 + 红线校验

契约正文后必须附 ```json 块，六字段：`signal_timing` `execution_price` `warmup_period`
`nan_policy` `entry_signature` `assumed_defaults`。

`validate_spec.py` 做三件事：
- 缺 JSON 块或字段 → FAIL
- **硬红线**：`signal_timing=t_close` 时 `execution_price` 不得为 `t_close`/`same_bar` → FAIL
- `assumed_defaults` 超过 3 条 → `[SPEC_WARN]` 告警（不阻断，模型不可能零假设）

实测 v1.7 的纯 Markdown 契约被拒。

### 5. Audit 差分审计

`audit` 现在做三件实质检查：

```
差分审计:
  · AST 已变更: 031082bc → d47f083c（结构层面确有修改）
  · 探针判定: draft=未通过 → final=未通过
  · final 仍未通过探针: [CONTRACT]
```

第三条是真实的：**v1.7 跑出来的 final_code.py 入口叫 `momentum_20d_strategy`，
过不了 v1.8 的强制入口契约**。这说明 v1.8 不向后兼容旧产物——
必须用新 prompt 重新生成，不能拿旧 run 直接续跑。

## v1.7 四项改造

### 1. `check_code.py`：AST 探针 + 合成数据冒烟

新增 `--level ast`（默认）与 `--level smoke`，在调模型之前零成本拦截：

| 探针 | 判定 | 说明 |
|---|---|---|
| `.shift(-n)` 负向位移 | **[FAIL]** 确定性前视 | 把未来数据搬到当期 |
| 全局 `.mean()/.std()/.quantile()` 无 rolling | [WARN] | 语境相关，可能是常数阈值 |
| `iterrows()` / `itertuples()` | [WARN] | 量化里通常该向量化 |
| 除法无 replace/where/clip 保护 | [WARN] | 按函数粒度判断，有防护则跳过 |
| `log()` 无定义域保护 | [WARN] | 0 或负值得 -inf |

`--strict` 把 WARN 升级为 FAIL。`--level smoke` 用合成行情实跑入口函数：100 行、
首尾 NaN、35-37 行停牌零成交、50-51 行一字板、40/70 行 ±15% 跳空。

### 2. `score_candidates.py`：硬门槛 + 四维客观分

硬门槛直接调 `check_code --level smoke`，不过者 0 分淘汰。

客观分 100 = 规范 30（docstring/命名/无占位）+ 向量化 20（无逐行/有窗口算子）
+ 稳健 30（raise/replace-where-clip/保留 index）+ 契约 20（返回注解/默认值/参数标注）。

语义分（30%）由 `pick --emit` 生成任务书，让模型只核对「实现是否符合契约的时序与公式」，
结果写入 `candidates/verdict.json`，重跑 `pick` 时自动按 0.7/0.3 加权。

### 3. 防震荡：AST 哈希早停 + 段落过滤

- `ast_hash()` 对 AST 规范化后取 SHA256，忽略注释/空行/格式差异。
  返修后若结构与上轮完全相同且上轮自检未过，直接 `exit 1` 早停，不再空跑。
- `{{critique_report|section:修复指令}}` 让 refiner 只拿问题清单，
  实测上下文从 13,670 字符降到约 200 字符。

### 4. Spec 节点：契约先行

DAG 从 3 节点变 4 节点：`spec → generator → critic → refiner`。

spec 节点把需求固化成五节契约（数据/信号时序/指标/交易/输出），
generator 照写、critic 加一段【契约核对】照核、refiner 以契约优先。
三者共用同一份真值，避免每轮各自重新理解需求。

### 未做的两项及理由

**并发**：builtin 模式的实际瓶颈是编排层串行调度子代理，不是 Python 侧。
`verify` / `score` 本身是秒级子进程，上 asyncio 收益接近零。
只有 `runtime: api` 无人值守时有意义，那条路目前没接 best_of_n。

**ToT**：评估环节与 critic 职责重叠，且成本翻倍。见上一节说明。

## 多路径采样 Best-of-N（v1.6）

原来每个节点只调一次，单路径。现在 generator 可以铺 N 条候选，规则打分选优，
只把胜出的那份送进 critic。

### 一个关键调整：换范式，不是重复采样

你说的“同一 prompt 生成 5 次”，我不建议做成默认。同 prompt 在低温度下重复采样，
差异主要是变量命名和注释措辞，真正的多样性收益很小。

改用**正交实现范式**——每条候选走不同的路线：

| 编号 | 范式 | 指令要点 |
|---|---|---|
| A | 向量化优先 | 能用 pandas/numpy 原生运算的，不写 Python 循环 |
| B | 防御型 | 优先写校验与边界处理，宁可多 30 行校验 |
| C | 分层解耦 | 配置对象 + 纯函数 + 适配层，便于单测 |
| D | 极简 | 只实现核心，短到能一眼读完 |

两种模式都支持：`diversity: orthogonal`（默认）和 `diversity: sample`（你要的原版）。

### 配置

```yaml
- id: generator
  best_of_n:
    n: 3
    diversity: orthogonal     # orthogonal | sample
    select: rule              # 规则打分
```

### 打分规则（`tools/score_candidates.py`）

**硬门槛**（不过直接淘汰，不参与排名）：语法解析失败、依赖不可导入、**含占位符**
（TODO / NotImplementedError / 省略号）。

**领域加权**（量化代码特有，不是通用代码质量指标）：

| 加分 | 扣分 |
|---|---|
| `+2/处` raise 校验，上限 10 | `-25/处` 占位符（同时淘汰）|
| `+1/处` isfinite/isna 防护，上限 6 | `-8/处` `reset_index(drop=True)` |
| `+6` 代码中真实引用 volume（停牌处理）| `-15/处` 负向 `shift(-n)`（前视嫌疑）|
| `+5` 代码中引用成本/滑点 | `-15` 少于 15 行 |
| `+8×覆盖率` docstring | `-10` 超过 2000 行 |
| `+4×覆盖率` 类型标注 | |

刻意用 **AST 提取的标识符**判断领域特征，而不是正则扫全文——否则注释里写一句
“TODO: 补充停牌处理”就能骗到 `+6`。

### 它筛的是什么，不筛什么

筛的是「**是否考虑到了该考虑的东西**」，不是「策略逻辑对不对」。

规则看不懂“这个动量窗口该用 20 还是 60”，也看不出“信号日该不该顺延”。
逻辑正确性仍然是 critic 的活。Best-of-N 在这里的价值是**提高初稿的工程基线，
减少 critic 的返工**，不是“选出最赚钱的策略”。这点必须分清。

### 实测

三份候选（同一需求：20 日动量 + 停牌处理）：

```
     候选          得分  硬门槛  规模
★  generator.c2  100   通过    28 行
   generator.c1  85    通过    5 行
   generator.c3  52    淘汰    8 行
```

- c2（防御范式）：3 处 raise + isfinite + volume 引用 + docstring + 类型标注 → 100
- c1（向量化范式）：5 行，只扣“疑似未实现完整” → 85
- c3：TODO 占位 + `reset_index(drop=True)` → 52，**淘汰**（不进排名）

选中 c2 后落为 `draft_code.py`，静态检查 0.12s 通过，推进 critic。

### 为什么不做 ToT

思维树（提 3 个架构 → 评估 → 淘汰 → 细化）在当前流程里是负收益：

1. **场景不对**。ToT 适合“需求模糊、方案空间大”的架构设计。这套流程的输入是
   “写个 20 日动量策略”，方案空间就那么大，探索不出三种架构。
2. **评估环节已有**。ToT 的核心是“评估哪个更好”，而 critic 就是这个角色，而且更专业
   （十维覆盖 + 行号证据）。再插一个评估节点是重复劳动。
3. **成本翻倍**。3 方案 + 1 评估 + 1 细化 = 至少 2 轮 reasoning 调用，而收益不确定。

真要用于架构级任务，最小改法是把 `best_of_n` 的范式换成“架构路线”（单体 / 分层 / 插件），
让 critic 增加一个“架构合理性”维度——不需要引入 ToT 的多阶段树。

## 代码执行闭环（v1.5）

「写完就跑、报错回灌」这套机制原来只有 refiner 有，generator 产出的初稿从不执行——
这意味着**跑不起来**的问题要靠 critic 读代码猜。现在补齐两级。

### 两级检查，按成本分配

| 节点 | 检查级别 | 命令 | 拦得住什么 | 耗时 |
|---|---|---|---|---|
| generator | `--level import` | `check_code.py {artifact} --level import` | 语法错、依赖缺失 | ~0.1s |
| critic | 无 | — | 它是审查，不产出可执行代码 | — |
| refiner | `--level run` | `check_code.py {artifact} --level run` | 运行期异常、断言失败 | ~1-2s |

generator 用静态检查而不是真跑，理由是**成本**：critic 一次要 5 分钟、23k 字输出，
如果初稿连冒号都没写、或者 `import talib` 而环境里没有，这 5 分钟全是浪费。静态检查
0.1 秒就能拦住。

### 四级错误分类

`tools/check_code.py` 的输出带标记，返修 prompt 据此决定修法——**写错和逻辑错要用不同的修法**：

| 标记 | 含义 | 返修策略 |
|---|---|---|
| `[SYNTAX]` | 语法解析失败 | 直接修，不许注释掉整段绕过 |
| `[IMPORT]` | 模块不存在 | 换等价库或内联实现，不许 `try/except` 静默跳过 |
| `[RUNTIME]` | 执行期异常 | 先定位触发条件再改实现，不许吞异常 |
| `[ASSERT]` | 断言失败 | 逻辑错，改实现；**不许改断言或删用例** |

### 实测

| 场景 | 构造 | 结果 |
|---|---|---|
| 语法错 | `def momentum(df)` 少冒号 | `[SYNTAX] 第 4 行第 17 列: expected ':'` → 返修 → 通过（1 轮）|
| 缺依赖 | `import talib`（环境无此库）| `[IMPORT] 无法导入: talib` → 拦截，exit 1 |
| 断言失败 | `assert f(2) == 5` 实际返回 4 | `[ASSERT] Traceback ...` → 分类正确 |
| 正常代码 | `import pandas` | `[OK] 1 个模块可导入`，exit 0 |

### 与「独立测试脚本」的差别

现在跑的是**产物自带的自检入口**（`if __name__ == "__main__": sys.exit(_self_check())`），
不是独立的 `test_*.py`。区别在于：

- 内嵌自检：单文件交付、零额外依赖，但自检和实现同源，可能一起错
- 独立测试：能交叉验证，但要给节点加第二个 output（`test_suite`），verify 改跑 pytest

要做后者，配置层面支持（`outputs` 可以多个），但 venv 里没装 pytest，得先解决依赖。
当前选择内嵌，是因为它对"策略代码交付"这个场景够用——而且自检失败会被 verify 抓住，
不会静默通过。

## CoT 强化（v1.4）

只改 critic 和 refiner，**generator 不改**。

理由：generator 要做的是“快”，不是“想得深”；显式推理会挤占输出预算（前面吃过 8192 全被
reasoning 吃光、content 返回空串的亏）。而且这三个节点锁在 reasoning 档，模型内部本来就在
step by step——Zero-shot CoT 那句魔法咒语对它收益接近零。

### critic：四段式 + 十维覆盖 + 3 条 Few-shot

```
【问题拆解】  列出所有关键假设，每条一行
【逐条验证】  逐项判定 通过/存疑/违规，附 函数名 + 行号 + 代码片段
             强制覆盖十个维度（前视偏差、停牌与涨跌停、NaN 传染、复权、
             成本、参数边界、多标的面板、返回契约、性能可复现、静默降级）
【反例构造】  每个违规项给一个带数字的行情场景
【结论】      阻断/高危/中危/低危 分级，一条一行
```

Few-shot 直接截取自 `runs/20261021-174853/critique_report.md` 的三条真实问题，
分别对应阻断 / 高危 / 中危，示范的是**证据密度**而不是推理路径。

### refiner：对照表 + 自检入口

第一部分输出 `| 编号 | 意见摘要 | 处理方式 | 代码位置 |`，处理方式只允许三种：
`已修复` / `不适用（必须给量化理由）` / `无法修复（必须写残余风险）`。
第二部分输出代码，且**必须带自检入口**，非零退出码会触发返修循环。

### A/B 实测（同一份 draft_code，只换 critic prompt）

| 版本 | 字数 | 问题数 | 多标的提及 | 行号引用 | 带数字反例 |
|---|---|---|---|---|---|
| 原始 prompt | 13,670 | 23 | 7 | 14 | **0** |
| v1：四段 + 6 项清单 | 11,227 | 12 | **0** | 51 | 20 |
| v2：四段 + 10 项清单 | 23,605 | 14 | 9 | **126** | 13 |

**结论一：结构化模板显著提升可核对性。** 行号引用 14 → 126（9 倍），带数字的反例从
0 到 13。旧版 23 条问题里有大量“可能存在问题”式的泛泛描述，新版每条都能定位到行。

**结论二：模板会收窄视野。** v1 把开放的“数据稳健性”收窄成 6 个具体子项，结果完全
漏掉“多标的面板”这个维度（旧版提 7 次，v1 提 0 次）。这是模板化的固有权衡：**开放的
指令覆盖广而浅，模板化的指令深而窄**。必须在清单里把维度写全，否则模型只会答你问的。

**结论三：允许“无法修复”提升可信度。** refiner 对复权口径给出了 `无法修复`，并写明
“阈值以下的现金分红缺口识别不到，5 元股价派 0.25 元不触发”、残余影响“RSI 压低 1–2 点、
持续约 14 个 bar、可能提前 1–3 天触发买入”。这比硬凑一条“已修复”有价值得多。

### refiner 两版对比

| 版本 | 行数 | 字符 | 对照表 | 自检 |
|---|---|---|---|---|
| 原始 prompt | 1,246 | 49,006 | 无 | 11 项 |
| 仅加对照表 | 478 | 17,592 | 14 条 | **无** |
| 对照表 + 自检要求 | 1,047 | 41,353 | 18 条 | 有 |

中间那版暴露了一个真问题：**要求输出格式后，模型不再自发写自检**，`verify` 退化成
“能跑就算过”。补上硬性要求才恢复。

### 代价

critic 输出从 13,670 字涨到 23,605 字（1.73 倍）。builtin 走平台额度不花钱，但单节点
耗时从约 3.5 分钟涨到约 5 分钟。要压成本就把十维砍到六维，但会重演 v1 的漏项问题。

## 自检返修闭环（v1.3）

问题出在哪：refiner 交付的代码自带自检，但**自检跑红后模型不会回头修**——它把失败当终态交出来。

解法：节点级 `verify`，把"自检失败"变成下一次迭代的输入。

```yaml
- id: refiner
  verify:
    command: "{python} {artifact}"     # {python}=当前解释器, {artifact}=产物绝对路径
    timeout: 300
    max_rounds: 2
    log_tail_chars: 4000
    repair_prompt: |
      上一轮产物自检未通过。请只修复日志中的失败项，保持已通过的部分不变。
```

流程：

```
refiner 产出 → 回收 → 归档 r0 → 自检
                                ├─ 通过  → 推进下一节点
                                └─ 失败  → 生成 03_refiner.repair1.md（内联失败日志）
                                            → 子代理返修 → 回收 → 归档 r1 → 自检
                                                            ├─ 通过 → 推进
                                                            └─ 超 max_rounds → exit 1，转人工
```

落盘结构：

```
runs/<id>/
├── final_code.py                      # 始终是最新版
├── history/refiner.final_code.r0.py   # 每轮归档，幂等（重复 step 不刷重复文件）
├── verify/refiner.r1.log              # 每轮完整日志（含命令与全量输出）
├── state.json                         # rounds / verify 摘要，不含日志正文
└── tasks/03_refiner.repair1.md        # 返修任务书
```

判定规则：

- 退出码 `0` = 通过；非 0 = 失败
- `_pending_nodes` 把「产物齐全但自检未过」也算未完成，不会误报“全部完成”
- 返修任务书要求「先读上一轮文件再定点修复」，并明令禁止删用例来“让它通过”
- 超限返回 `exit 1`，打印最后一轮产物路径和日志，人工接手

### 实测

| 场景 | 构造 | 结果 |
|---|---|---|
| 失败→修复→收敛 | 注入 `raise SystemExit` | 归档 r0 → 自检✗ → 返修 1/2 → 归档 r1 → 自检✓（共 1 次返修）|
| 恒失败→超限 | 注入恒失败 | 返修 1/2 → 2/2 → `已达返修上限 2 轮，未收敛` → exit 1 |
| 一次通过 | 主线 run | 归档 r0 → 自检✓（0 次返修）|

## 档位锁定（v1.2）

所有节点共用同一个内置档位，配置只有一处：

```yaml
builtin:
  profile: reasoning     # lite / reasoning / default
  lock: true
```

`lock: true` 的语义是**硬拒绝**，不是静默覆盖：

| 情况 | 行为 |
|---|---|
| 节点写了 `agent_profile` 且与全局不同 | 校验失败，拒绝启动，报出冲突项 |
| 节点写了 `agent_profile` 且与全局相同 | 允许，但不改变任何东西 |
| `lock: false` | 允许节点逐点覆盖，`validate` 会标出档位不一致 |

`models.fast/reasoner` 在 builtin 模式下**不参与决策**，只服务于 `runtime: api`。
这样同一份配置能跑两种运行时，且不会出现“改了一个地方忘了另一个”。

### 为什么锁到 reasoning

- **一致性**：三节点同一个档位，跨次运行的输出差异只来自采样，不来自档位漂移
- **代价**：generator/refiner 变慢。推理模型写代码会先推演再落笔，单节点延迟高于 lite
- **代价**：token 消耗上升（reasoning tokens 计入），但 builtin 走平台额度，不扣自有余额
- **不牺牲质量**：reasoning 是更高档位，质量不会下降；真正“拿质量换一致性”的是统一到 lite

### 验证档位确实生效

`audit` 做三路比对，任一路缺失或不等即判失败：

| 检查面 | 数据来源 | 作用 |
|---|---|---|
| 配置 | `workflow.yaml` 解析结果 | 确认解析器认为的档位 |
| 任务书 | `tasks/*.md` 里的 `模型档位:` 行 | 确认交给执行者的指令 |
| trace | `tasks` 回收时写入的 `trace.json` | 确认实际记录的执行档位 |

判定为 `✓` 需要同时满足：配置等于锁定值、任务书有声明、trace 有记录、三者相同。
**缺记录判 ✗，不能算一致**——那只是“未审计”。

```bash
$PY orchestrator.py audit runs/20261021-174853
```

```
  节点       配置       来源         任务书     trace      模式     判定
  generator  reasoning  global-lock  reasoning  reasoning  builtin  ✓
  critic     reasoning  global-lock  reasoning  reasoning  builtin  ✓
  refiner    reasoning  global-lock  reasoning  reasoning  builtin  ✓

结论: ✓ 全部 3 个节点生效档位均为 reasoning，锁定有效。
```

## api 运行时

```bash
$PY run.py --validate                # 静态校验，不调模型
$PY run.py --dry-run -t "策略需求"    # 空跑全链路，零成本
$PY run.py                           # 交互式输入 user_task
$PY run.py --resume runs/<run>       # 断点续跑，复用已产出变量
```

## 变量绑定语义

依赖边由**变量引用自动推断**，不需要手写连线：

| 节点 | 读取 | 写入 | 推断出的上游 |
|---|---|---|---|
| generator 初稿生成器 | `user_task` | `draft_code` | — |
| critic 严厉审查官 | `user_task`, `draft_code` | `critique_report` | generator |
| refiner 重构修复者 | `user_task`, `draft_code`, `critique_report` | `final_code` | generator, critic |

执行前 4 项硬校验，任一失败即拒绝执行：

1. 变量引用必须在此刻已定义（按拓扑序增量检查），不静默留空
2. 无循环依赖（Kahn 算法）
3. 输出变量不重复声明、不与输入变量冲突
4. 必填输入变量非空

## 为什么不直接调 WorkBuddy CLI

App 内置了 `codebuddy -p --model <id>` headless 入口，模型列表里确实有
`deepseek-v4.1-flash` / `deepseek-v4-pro`。但从 WorkBuddy 会话内部再拉一个 CLI 进程会
卡住不返回（沙箱 IPC + 嵌套 session 互斥），实测 5 分钟零输出。所以 builtin 走子代理，
不走 CLI。

## 已知坑

**DeepSeek V4 系列的 reasoning_tokens 与 content 共享 max_tokens。** 额度打满时 `content`
返回空串、`finish_reason=length`。引擎已处理：预算从 24576/32768 起，空 content 且 length
时翻倍重试（上限 65536），并用 `effort` 参数压思考链。api 运行时才涉及，builtin 不受影响。

## 实测

### 轮次 1：双均线（混档，api + builtin 混合执行）

| 节点 | 档位 | 产出 |
|---|---|---|
| generator | lite（api） | 2,963 字初稿 |
| critic | reasoning（api） | 2,646 字审查，20 条问题 |
| refiner | lite（builtin） | 18,952 字（548 行），20 条逐条对应 |

审查官抓到的问题：信号日/执行日未分离 → 前视偏差；停牌期间 `ffill` 静默跨不可交易区间
持仓；`fast == slow` 被误判死叉；未校验 `fast < slow`；零成交量未过滤；净值含不可成交的
隔夜跳空。

修复版冒烟测试（400 根 K 线，注入 3 根停牌）：6 次完整交易，停牌区段正确标记
`is_stale` 并告警，输出 22 列含 `equity` / `benchmark_equity`。

### 轮次 2：RSI 均值回归（全 reasoning，锁定验证）

| 节点 | 档位 | 产出 |
|---|---|---|
| generator | reasoning | 3,308 字初稿 |
| critic | reasoning | 13,669 字审查，23 项问题（5 阻断/6 高危/6 中危/6 低危）|
| refiner | reasoning | 49,006 字（1,246 行），23 项逐条对应 |

`audit` 三路一致，全部 `reasoning` / `global-lock`。

产物自带 11 项自检，**初始 1 项失败**：`RSI 双实现逐点对拍` 报 warm-up 区间不一致。
根因不是策略 bug——自检用 `10 + cumsum(N(0,0.2))` 生成价格，500 步后跌成负值（min=-2.83，
38 个非正价）。`calc_rsi` 把非正价视为无效、ffill 20 行后切 segment 重新预热，这是正确
守卫；而 `_wilder_rsi_reference` 是裸算法无此守卫，两边 NaN 模式必然错配。**是用例设计
错误，断言层级错配**。

已人工修正该用例（改为几何随机游走保证价格恒正），复跑 11 项全过、退出码 0。
这次暴露一个事实：**模型自己写的自检跑红，它不会主动回头修**——refiner 交付了带失败
自检的代码。自检是资产，但不是验收，验收得人来跑。
