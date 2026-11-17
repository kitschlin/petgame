# Agent 军师改造计划（v2）

## 0. 背景与目标

现有军师（v1，管线式）是“引擎负责算，LLM 负责选择和讲解”：
- 战斗时 LLM 只在 ≤3 个差距不到半个标准误的候选里挑一个，再写 60 字说明；
- 选将时 LLM 提出的方案最终会被引擎的穷举覆盖；
- 复盘时结论已由引擎五选一定好，LLM 只是把它写成文字。

结果是把 LLM 换成模板，效果几乎不变，没有发挥 LLM 的能力。

已有的 agent 实验（`traces/agent/batch-nothink`）也说明，在纯计算上 LLM 赢不了引擎：只给基础工具 3%，加引擎工具 17%，给全套工具 50%，而管线版是 67%。

**v2 的目标**：把“程序调用 LLM”改为“LLM 调用程序”。

- 引擎降级为工具，负责计算和验证；
- LLM 负责决定检索什么、算什么，综合经验库和引擎结果做决策，自己选择复盘的归纳角度，写入并维护经验库，制定培养路线；
- 数字只能来自工具结果，这条防幻觉的底线保留；
- **旧设计进行删除**：新模式中不使用旧的设置。

## 1. 删除与保留的范围

**删除**：v1 中“由程序编排流程、LLM 只填空”的部分。

| 模块 | 删除的内容 |
|---|---|
| `coach/draft.py` | `DraftAdvisor`：LLM 一次性提最多 8 套方案 → 校验 → 引擎决选 → 复核的固定流程；`build_user_message` / `read_plans` / `validate_plans` 等 v1 提示词和解析 |
| `coach/battle.py` | `BattleAdvisor`：“候选区 ≤3 个 + 事实卡 + LLM 只能在候选里选”的流程；`build_user_message` / `read_choice` / `hint_for` |
| `coach/review.py` | 固定的“五选一结论”作为最终结论；LLM 讲解 + 事后逐条丢弃的流程 |
| `coach/growth.py` | “主攻手攻击 → 速度 → 生命轮着取、最多 3 项”的固定选项规则；LLM 只讲解、不能增删项目的限制；`COACH_GROWTH=full` 的固定路线搜索流程 |
| `coach/prompts/` | `draft_system.md` / `battle_system.md` / `review_system.md` / `growth_system.md` / `growth_light_system.md` |
| `coach/llm.py` | `FakeLLM` 中针对 v1 的 `_plans` / `_battle` / `_review` / `_growth` |
| `coach/service.py` | 调用上述 Advisor 的编排代码 |
| 相关测试 | 只测 v1 流程的测试用例 |

**保留，改为工具的后端**：引擎计算能力本身不属于“旧设计”。推演、事实卡、对阵表、经验修正、逐步评级、四项归因、台阶计算、升级模拟这些函数，从 v1 流程中拆出来，作为 Agent 的工具实现。

**不再存在的东西**：
- 没有 `pipeline | agent` 模式开关，也没有“经典军师”入口；
- 没有回退到 v1 的兜底；
- 战斗中没有 v1 先出建议、Agent 再替换的并行机制。

## 2. 总体架构

新增目录 `petarena/coach/agentic/`，取代 v1 的 Advisor：

```
petarena/coach/agentic/
  runtime.py        ReAct 主循环（复用 agent/loop.py 的 ChatClient）：步数/时间预算、trace 记录
  guard.py          事实校验：输出里的数字必须出自本轮工具结果；引用的证据编号必须存在；动作必须合法
  tools/
    draft_tools.py
    battle_tools.py
    review_tools.py
    growth_tools.py
  draft_agent.py
  battle_agent.py
  review_agent.py
  growth_agent.py
  prompts/          四个 system prompt
```

**API**：`/api/coach/draft`、`/api/coach/battle`、`/api/coach/review/{id}/report`、`/api/coach/growth` 保持路径不变，内部直接改为调用 Agent。返回结果新增 `trace` 字段。

**可解释性**：每条建议都附带可展开的“思考过程”，按顺序显示调用了哪些工具、拿到了什么结果、最后为什么这样决定。

**失败处理**：沿用 AGENTS.md 第八节第 4 条的规则——超时、出错、未通过 guard 校验都算一次失败；自动重试最多 2 次，失败原因附在下一次请求里；仍失败时显示“暂无建议”和“重试”按钮。不回退到任何旧流程。

**测试**：FakeLLM 改为按脚本调用工具，用于离线单测。

## 3. 选将：Agent 自己检索，再综合推荐

### 工具

| 工具 | 说明 | 后端 |
|---|---|---|
| `get_draft_state` | 当前选将局面、双方已选、属性表 | `draft.Situation` |
| `search_pet_experience` | 宠物经验检索 | `knowledge.TOOLS` |
| `search_combo_experience` | 组合经验检索 | `knowledge.TOOLS` |
| `search_lessons(pets, against)` | 复盘沉淀的经验条目 | `memory.lessons` / `experience.relevant_lessons` |
| `get_real_record(opponent)` | 真实战绩 | `experience.real_record` |
| `get_opponent_profile` | 对手画像；真人对手额外附上历史选将 | `draft.profile_for` |
| `evaluate_lineups(lineups[])` | 试遍站位、对上对手补选，返回修正后的胜率 | 从 `draft.decide` 拆出的打分函数 |
| `engine_top_lineups(k)` | 引擎穷举的前 k 名 | 从 `draft.decide` 拆出的穷举函数 |
| `verify_lineup(lineup)` | 用新种子复核 | 从 `draft.decide` 拆出的复核函数 |
| `submit_recommendation` | 结束动作：推荐、备选、整体思路、理由（需引用证据） | 新增 |

### 流程

1. LLM 根据局面决定检索什么（例如对手已经选了小乌，就查“破盾”相关的经验）；
2. 根据经验提出候选阵容，调用 `evaluate_lineups` 验证；
3. 调用 `engine_top_lineups` 和引擎最优做对照；
4. 综合给出推荐。理由里要写清楚“经验库说了什么、引擎算出什么、两者矛盾时为什么这样取舍”。

### 约束

- 胜率数字只能来自工具结果，由 guard 校验；
- 如果 LLM 推荐的阵容比引擎最优差超过 2 个标准误，面板上并列展示“Agent 推荐 vs 引擎最优”，不隐藏差距；
- 预算：最多 8 次工具调用，15 秒内完成。

## 4. 战斗：每回合按需推演，再做决策

### 工具

| 工具 | 说明 | 后端 |
|---|---|---|
| `get_state` | 完整局面 | `agent/env.py` |
| `evaluate_moves(games=24\|96)` | 每个合法动作的推演胜率；LLM 可以要求加深推演 | `battle.evaluate` |
| `simulate_line([...])` | 推演一串连招，例如“兔子快快跟上 → 小猫猛爪” | 新增，基于 `rollout` |
| `fact_card(move)` | 伤害、能否击倒、溢出、剩余能量、协同补刀 | `rollout.fact_card` |
| `search_lessons` | 相关经验条目 | `memory` |
| `submit_move` | 结束动作：动作、理由、风险、本回合计划 | 新增 |

### 做法

- 整局保持同一段对话，LLM 能记住“这回合的计划”，跨宠物行动保持连贯；
- LLM 自己决定推演多深、要不要推演连招，可以选任何合法动作；
- guard 只校验动作是否合法、数字是否出自工具结果，不再限制“只能在候选里选”；
- 面板同时展示引擎给各动作的推演数值，LLM 的选择和引擎最优不一致时，玩家可以直接看到。

### 速度

- 预算：最多 4 次工具调用，目标 P50 ≤ 3 秒，和动画同时进行；
- 进入玩家回合时预先启动一次 `evaluate_moves`，结果缓存，LLM 调用时直接返回。

## 5. 复盘：LLM 复盘全局，自己决定归纳角度

### 工具

| 工具 | 说明 | 后端 |
|---|---|---|
| `get_timeline` | 压缩后的全局事件流 | 战斗记录 |
| `get_turn_detail(turn)` | 某一回合的详细局面和事件 | 重演 |
| `grade_steps` | 我方每一步的评级和损失 | 从 `review.analyse` 拆出 |
| `attribution` | 四项归因（属性底子 / 阵容 / 操作 / 运气），只作为数据，不给结论 | 从 `review.analyse` 拆出 |
| `counterfactual(turn, move)` | 从某一步换个动作重打，看结果变化 | `battle.evaluate` |
| `compare_lineups` | 实际阵容和更优阵容的对比 | matchup / experience |
| `query_history(n)` | 最近 n 局的失误类型、采纳率、q 值 | `memory.battles` / `memory.quality` |
| `search_lessons` | 查已有经验 | `memory` |
| `write_lesson` / `update_lesson` / `retire_lesson` | 写入、修改、淘汰经验 | 扩展 `memory` |

### 归纳角度和结论由 LLM 决定

可选的角度包括：阵容、能量节奏、目标选择、出手顺序、对手模式、玩家的固有习惯等。跨局的规律（例如“连续 3 局第 2 回合都浪费了能量”）只有读 `query_history` 才能发现。

主要原因由 LLM 根据归因数据和逐步评级自己判断，不再套用 v1 固定的五选一阈值。

### 结构化经验条目

```json
{
  "trigger": "触发条件（局面特征）",
  "claim": "结论",
  "action": "建议动作",
  "evidence": ["证据编号"],
  "scope": "适用范围（对手模式 / 阵容）",
  "confidence": 0.0,
  "status": "verified | pending | retired"
}
```

`memory` 中 v1 的经验条目表（适用范围 / 宠物 / 对手宠物 / 宜忌 / 一句话）改为上面的结构，旧条目不迁移。

### 入库前校验（Reflection）

- 关于阵容的结论：用对阵表核对；
- 关于出招的规则：在历史局面里找出满足触发条件的决策点，用引擎检验规则的命中率；
- 通过的标为 `verified`，没通过的标为 `pending`，后续对局持续更新置信度；
- 重复或互相冲突的条目由 LLM 决定合并、修改或淘汰；
- 和引擎数据冲突时，不直接丢弃，而是标出矛盾，让 LLM 再调一次工具核实后决定。

### 输出

总评、主要原因、关键回合点评、1～3 条下一局可以照做的建议、经验库的变更记录（新增、修改、淘汰了哪些条目）。

## 6. 培养：LLM 结合玩家情况制定路线

### 工具

| 工具 | 说明 | 后端 |
|---|---|---|
| `get_roster` | 当前属性 | 已有 |
| `find_thresholds(pet)` | 每项属性的台阶（每一点跨过的台阶最多的档位） | `growth` 中的台阶计算 |
| `simulate_upgrade(upgrade)` | 模拟某次升级后的胜率变化 | `growth.run_configs` |
| `get_player_profile` | 常用宠物、主要失利原因、剩余挑战次数 | `memory` |
| `submit_plan` | 结束动作：分阶段路线、每项练到多少就停、不建议练的项 | 新增 |

### 流程

LLM 结合玩家的偏好、资源和失利原因，自己决定看哪些宠物的台阶、模拟哪几种升级方案；对比模拟结果后给出分阶段路线。路线里的胜率变化必须来自 `simulate_upgrade`。

## 7. 实施顺序

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | runtime + guard + 工具注册框架；FakeLLM 改为按脚本调用工具；从 v1 中拆出引擎函数作为工具后端 | 能跑通空 agent，带单测 |
| P1 | 战斗 Agent（最多复用 `agent/env.py`）；删除 `BattleAdvisor` 及其提示词和测试 | 战斗面板显示思考过程 |
| P2 | 选将 Agent；删除 `DraftAdvisor` 及其提示词和测试 | 选将面板展示“Agent 推荐 vs 引擎最优”对照 |
| P3 | 复盘 Agent + 结构化经验 + 经验校验；删除 v1 的五选一结论和讲解流程 | 复盘页和经验库页 |
| P4 | 培养 Agent；删除固定的选项规则和完整模式 | 培养面板 |
| P5 | 评测（见第 8 节） | 报告的数据 |
| P6 | 文档：重写 AGENTS.md 第八节、DEV_NOTES.md；报告第 4 节改为 v2 的设计，加上 trace 截图 | 新的文档和 tex |

先做战斗：现成代码最多（`evaluate_moves`、`fact_card` 可以直接用）。

每个阶段删除旧代码时同步跑全量测试，确保引擎和其他模块不受影响。

如果时间紧，优先做 P0、P1、P3，选将和培养只做精简版。

## 8. 评测

扩展 `scripts/agent_match.py`，用固定种子评测 v2：

- **胜率**：对智能对手、随机对手。v1 不再运行，以报告中记录的 v1 数据（对智能对手 67.1% ± 5.9%）作为历史参照；
- **延迟**：每次建议的 P50 / P95，工具调用次数，token 用量；
- **可靠性**：guard 拦截率、重试率、最终失败率；
- **决策质量**：动作和推演最优一致的比例、平均价值损失、和引擎最优不一致的次数及其结果；
- **经验库**：新增条目数、验证通过率、多局后置信度的变化；
- **教学效果**：玩家的 q 值随局数的变化。

## 9. 待确认的决策

1. **战斗胜率下降的处理**。去掉候选区限制后，胜率可能低于 v1 的 67%。如果评测下降明显，是收紧 prompt 让 LLM 更多参考推演结果，还是接受下降、换取更多 LLM 自主决策？
2. **v1 数据在报告中的位置**。只在评测里作为历史参照，还是保留一小段“改造动机”说明 v1 的问题？
3. **范围和时间**。P0 到 P6 全做完比较紧，可以先做 P0、P1、P3。
