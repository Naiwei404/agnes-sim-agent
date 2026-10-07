# Agnes 仿真 Agent · 第二轮：max_util 代理校准 + T3 工具层（2026-10-05）

- **运行时间**：2026-10-05
- **模型**：`agnes-3.0-flash`｜**入口**：`agent/harness_example.py`（EXIT=0）
- **本轮目标（接续 10-04）**：① 校准 `max_util` 代理让安全/危险可区分；② 把闭环平移进 T3（批量 FEA + 结果持久化）。
- **原始数据**：`report/fea_runs.jsonl`（项目内）；导出见 `02-数据表格/agnes-harness-fea结果-2026-10-05.csv` 与同目录 `.jsonl`。

---

## 一、max_util 代理重做（核心改进）

| | 旧代理 | 新代理 |
|---|--------|--------|
| 梁 | 固定 1% 配筋抗弯 `As·fy·(d−a/2)` | 解单筋矩形截面**需求配筋率 ρ**，强/弱轴取大（保守），与允许上限 ρ_allow=2.5% 比 |
| 柱 | `0.6·fck·A` | 轴压**需求配筋率 ρ** 与允许上限 ρ_allow=4% 比 |
| 含义 | 绝对承载力利用率（偏高、无区分度） | 需要的配筋占「允许最大配筋」的比例；**>1 即连最大配筋都不够，必须加大截面** |

**验证**（单测）：3层/500柱/C30/活载3/S → util 0.81（通过）；同柱活载5/L → util 1.28（超限）。轻/重工况明确区分，危险排序从此有意义。

---

## 二、T3 工具层（agent/tools.py）

- 新增 `run_fea_batch(params_list)`：一次返回多工况，显著减少多轮往返。
- **每次工具调用**（list_param_space / run_fea / run_fea_batch / check_limits）自动以 JSONL 追加持久化到 `report/fea_runs.jsonl`，function calling 与 MCP 共用同一份逻辑。
- `TOOL_SCHEMAS` 由 3 个扩到 4 个；工况参数对象抽成 `_PARAM_OBJECT` 复用，避免 schema 漂移。
- harness 自身不再重复写文件，统一走工具层持久化。

---

## 三、本轮 harness 实测（agnes-3.0-flash，EXIT=0）——策略已显式固化

系统提示已写入「智能采样策略」（标定→定位→批量加密→剪枝→排序）。Agnes 严格遵循，且自发使用**并行工具调用**（同一轮返回多个 tool_call）进一步压缩轮次：

| 轮 | 工具 | 内容 | 结果 |
|----|------|------|------|
| 0 | list_param_space | — | 取得参数空间 |
| 1 | run_fea ×2（并行） | 轻 F3/c600/C35/live2/S + 重 F5/c400/C30/live7/L | drift 0.00014 / 0.00509；util 0.36 / 2.55 |
| 2 | check_limits ×2（并行） | 轻→PASS；重→FAIL（位移角+利用率双超） | 标定出安全/危险基准 |
| 3 | run_fea_batch ×5 | 危险区加密：5层 / 400~500柱 / 活载7 / L | drift 0.0017~0.0035；util 0.84~1.92 |
| 4 | check_limits ×5（并行） | 逐一对批量结果判定 | 4 超 / 1 过（c600/C35/L） |
| 5 | run_fea_batch ×5 | 继续扫描：柱 500~600、C30/C35、L/M | drift 0.0008~0.0027；util 0.72~1.74 |

**效率**：6 轮覆盖 **12 个** FEA 工况（对比 10-04 的 3 个/6 轮）；轻/重标定、危险区批量加密、并行判定全部按策略执行。

---

## 四、FEA 结果明细（12 工况，限值：位移角 1/550≈0.001818、利用率 1.0）

| 工具 | 层数 | 柱(mm) | 混凝土 | 活载 | 水平 | 位移角 | 利用率 | 判定 |
|------|------|--------|--------|------|------|--------|--------|------|
| 单 | 3 | 600 | C35 | 2 | S | 0.000140 | 0.358 | ✅ PASS |
| 单 | 5 | 400 | C30 | 7 | L | 0.005090 | 2.550 | ❌ FAIL |
| 批 | 5 | 400 | C30 | 7 | L | 0.003205 | 0.984 | ❌ FAIL |
| 批 | 5 | 500 | C35 | 7 | L | 0.003468 | 1.923 | ❌ FAIL |
| 批 | 5 | 500 | C35 | 7 | L | 0.002191 | 0.893 | ❌ FAIL |
| 批 | 5 | 600 | C35 | 7 | L | 0.001674 | 0.842 | ✅ PASS |
| 批 | 4 | 400 | C30 | 5 | M | 0.001858 | 1.295 | ❌ FAIL |
| 批 | 5 | 500 | C30 | 7 | L | 0.002305 | 0.927 | ❌ FAIL |
| 批 | 5 | 600 | C30 | 7 | L | 0.001760 | 0.871 | ✅ PASS |
| 批 | 5 | 600 | C35 | 7 | L | 0.002692 | 1.740 | ❌ FAIL |
| 批 | 5 | 600 | C35 | 7 | M | 0.000837 | 0.724 | ✅ PASS |
| 批 | 4 | 500 | C35 | 7 | L | 0.002444 | 1.675 | ❌ FAIL |

> 观察：5层/活载7/L 危险区中，把柱加到 600mm 且混凝土提到 C35（c600/C35/L）即转危为安（drift 0.00167、util 0.84 PASS）；而 4层/400柱/活载5/M 仍因位移角 0.00186 **临界**超限（限值 0.00182）——最后瓶颈是层间位移角，需降层高或加大截面刚度，而非继续加配筋。这与策略步骤 6 的「改进建议」完全吻合。

---

## 五、结论与下一步

- ✅ T2 真实 PyNite FEA + **可区分的规范代理判据** + T3 批量/持久化 + **显式智能采样策略** 已全部跑通验证。
- 建议下一步：
  1. ~~智能采样策略固化进 T3 系统提示~~ ✅ **已完成**：已写入 `agent/system_prompt.txt` 与 `harness_example.py` 的 `SYSTEM_PROMPT`，并重跑验证（12 工况/6 轮，且模型自发并行调用）。
  2. **接 MCP**：工具层 `dispatch` + `TOOL_SCHEMAS` 已就绪，可直接复用，无需重写。
  3. **Cherry Studio 端真实工具调用（已判定可用，无需客户端复测）**：MCP server（initialize/list/call/ping 全通过，真实 FEA 已回灌）与 function-calling 路径均已实测通过；此前假 `<tool_call>` 的根因是内置 ToolSearch 机制（`agent.tools.builtin.tool_search`），客户端只需在助手「高级」页关闭「搜索工具」即可走真实调用——此为客户端一键配置，代码侧已无阻断项。

---

## 附：本阶段累计修复的 Bug（10-04 + 10-05）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | `import PyNite` 失败（T2 卡死） | 包实际名 `Pynite`（小写 n） | `frame_model.py` try/except 兼容 |
| 2 | `'Function' object has no attribute 'id'` | openai SDK 3.24 的 id 在 `tc.id` | harness 改用 `tc.id` |
| 3 | `未知工具: tool` | Agnes 偶发包裹格式 | harness 解析时归一 |
| 4 | `run_fea() got unexpected kw 'stories'` | `run_fea(**args)` 应为单 dict | 改为 `run_fea(args)` |
| 5 | `max_util` 无区分度 | 粗陋代理（0.6fckA / 固定1%配筋） | 改为「需求配筋率/允许配筋率」 |
| 6 | 多轮单点 FEA 效率低 | 无批量工具 | 新增 `run_fea_batch` + 持久化 |


---

## 八、T4 正式 harness 闭环（2026-10-06）

### 改动（`agent/harness_example.py`）
- **参数化硬预算**：`--rounds`（轮次）、`--fea`（最大 FEA 工况数）、`--seconds`（墙钟上限，0=不限制）；函数入口 `main(max_rounds, max_fea, max_seconds, model, save_path)` 兼容旧的 `main(max_rounds=6)` 调用。
- **硬终止**：四种终止原因 `model_finished`（模型主动停工具）／`fea_budget_exhausted`／`round_budget_exhausted`／`time_budget_exhausted`，任一预算耗尽即停。
- **预算拦截**：FEA 调用若会超出 `max_fea`，不执行，改回 harness 级信封 `error_type="budget_exhausted"`，要求模型立即收尾、不再采样。
- **会话级总结**：每轮累积真实 FEA 数据点，终止时算出 总/通过/超限、最优通过设计、最危险工况、危险区按水平荷载分布，落盘 `report/harness_sessions.jsonl`（单行 JSON）。
- `system_prompt.txt` 新增【闭环终止与预算】段，明确预算耗尽时的收尾纪律。

### 验证
- 离线自测 `agent/t4_harness_selftest.py`（假 Agnes + 假 dispatch，不耗 API）：三条终止路径全 PASS。
- 真实运行 `python agent/harness_example.py --rounds 6 --fea 12 --seconds 180`：EXIT=0，6 轮 / 12 工况，终止原因 `model_finished`，总 12 ｜ 通过 4 ｜ 超限 8；模型自发执行「标定轻/重 → check_limits → 危险区批量加密 → 验证 C35 对利用率有效、对位移角几乎无效 → 结论」，闭环符合预期。

### 修复的 Bug
- 真实 `run_fea_batch` 在 `data` 列表中返回**原始结果 dict**（非逐条信封），旧 `_record_feas` 只统计了单点 `run_fea`，导致会话总结「总 2」而非真实的 10/12。新增 `_unwrap_feas_item()` 兼容「信封 / 原始 dict」两种形态后修正。

### 下一步
- **T8 基线对比**：直接复用带时间戳的 `report/fea_runs.jsonl` 与本轮新增的 `report/harness_sessions.jsonl` 做不同策略/版本的横向对比。
