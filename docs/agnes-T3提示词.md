# 交给 agnes-3.0-flash 的提示词（T3：工具层强化）

> ⚠️ **状态更新（2026-10-04）**：本文是 T3 原始需求/提示词留存。代码由 L 的 WorkBuddy agent 完成——
> T2 已修复并跑通（`import PyNite` 大小写兼容）；T3 工具层已完整落地：新增 `run_fea_batch` + `report/fea_runs.jsonl` 持久化（已加 `timestamp` / `duration_ms`）
> + `agent/system_prompt.txt` 固化「智能采样策略」+ **缺口 1 入参校验** + **缺口 2 统一返回信封** 均已完成（2026-10-04 实测 ALL PASS）。
> 详见 `01-文档报告/agnes-harness运行报告-2026-10-04.md`。反幻觉纪律仍有效：任何数值必须来自实际运行输出，禁止编造。

---

## 提示词正文

你要帮我完成江苏省高校 AI+ 黑客松（赛道 B3，混凝土框架多工况智能仿真 Agent）项目的 **T3 任务：工具层强化**。

项目路径：`D:\WorkBuddy\AI工作区\03-代码项目\agnes-sim-agent`
Python 解释器：`D:\WorkBuddy\AI工作区\03-代码项目\agnes-sim-agent\venv\Scripts\python.exe`

---

### ⚠️ 最重要的纪律要求（先读这段）

上一轮你交回来的 T2 报告是**完全编造的**，实测已被推翻。具体表现：

- 你声称"已用 importlib 修复 `fea/frame_model.py`"——**文件一个字节都没改**，`import PyNite` 还在第 31 行
- 你声称"自测通过，3 个算例数值合理"——给了 drift=1/250、top_disp=3.2cm、base_shear=120kN 这些数字，**全部是编的**，实际跑 `fea/frame_model.py` 仍报 `ModuleNotFoundError`
- 你声称"harness 闭环验证成功"——项目里**没有任何日志或输出文件**佐证
- 你的报告第 3 节开头自己写了"**假设** fea/frame_model.py 已按上述修复"——你把自己没验证的东西当成结论交了

**从现在起，你必须遵守：**

1. **禁止编造任何输出数值。** 所有数字必须来自你实际执行命令后终端的原始输出。
2. **每次改完代码，必须贴出你实际执行的完整命令 + 原始输出**（不要复述、不要概括、不要"应该会得到"）。
3. **禁止假设。** 如果某件事你没验证，就明确写"未验证"。
4. 如果你声称修好了 X，必须贴出证明 X 已修好的**命令输出**。没有输出 = 没修好。
5. 遇到不确定的地方，**先实测再回答**，不要用推测填空。

---

### 项目现状（我已实测确认，可信）

**已完成：**
- `config.py`：读 `.env` 的 `AGNES_API_KEY` / `AGNES_BASE_URL` / `AGNES_MODEL`
- `test_api.py`：连通性测试，普通对话 + function calling 均已跑通
- `agent/tools.py`：三工具实现 + `TOOL_SCHEMAS` + `dispatch()`（function calling 与 MCP 共用同一份 Schema）
- `mcp/server.py`：零依赖 MCP 服务器（纯标准库实现 JSON-RPC 2.0 over stdio）
- `agent/harness_example.py`：已改为复用 `agent.tools.dispatch`

**未完成（重要）：**
- ~~**T2 未修好**~~ → **已修复并跑通（2026-10-05）**：`fea/frame_model.py` 的 `import PyNite` 改为 `try: import PyNite except: import Pynite as PyNite`（包实际目录名 `Pynite`，小写 n）。`run_fea` 现已返回真实 PyNite 3D 框架结果，harness 实测 EXIT=0。
- **T3 缺口 1（入参校验）与缺口 2（统一返回信封）：已完成（2026-10-04）**。`dispatch` 现返回统一信封 `{ok, tool, data, [warning]}`；入参校验覆盖四个工具，非法入参返回 `invalid_arguments` 信封（含 `invalid_fields`）而非原始异常。`not_converged` 以 `warning` 字段呈现（非失败，因非收敛本身是合法 FEA 结果）。缺口 3（批量 `run_fea_batch`）与缺口 4（结果落盘，含 `timestamp`/`duration_ms`）此前已完成。

---

### T3 任务：把 `agent/tools.py` 强化成正式工具层

DESIGN 文档对本层的要求是「工具层 + Schema + **异常回填**」。当前实现有 4 个明显缺口，按下面逐条补齐。

#### 缺口 1：入参校验（现在完全没有）

模型可能传错类型、漏字段、传非法值。当前 `dispatch` 直接 `**args` 展开，参数不对就抛原始 `TypeError`，模型拿到的是无意义的报错。

要做：
- 为每个工具写参数校验：类型、取值范围、必填、枚举合法性
  - `stories`：整数，3/4/5
  - `col_mm`：整数，400/500/600
  - `beam`：长度为 2 的整数数组，形如 [500,250] / [600,300]
  - `conc`："C30" / "C35"
  - `live_load`：数字，2/3/5/7
  - `lateral`："S" / "M" / "L"
- 校验失败时**不要抛异常**，返回一个结构化的校验错误，字段形如：
  ```json
  {
    "ok": false,
    "error_type": "invalid_arguments",
    "message": "col_mm 必须是 400/500/600 之一，收到 450",
    "invalid_fields": [{"field": "col_mm", "received": 450, "expected": [400, 500, 600]}]
  }
  ```
  目的是让模型能读懂自己错在哪、下一轮改对。

#### 缺口 2：统一返回信封

现在每个工具返回的结构完全不同（`list_param_space` 返回参数表，`check_limits` 返回 `passed/violations`）。要做统一信封，让模型每次都拿到一致结构：

```json
{
  "ok": true,
  "tool": "run_fea",
  "data": { ...该工具的原始返回... }
}
```

失败时 `ok: false` + `error_type` + `message`。
`error_type` 至少要区分：`invalid_arguments` / `not_converged` / `internal_error` / `unknown_tool`。

#### 缺口 3：批量运行（现在一次只能跑一个工况）

赛道 B3 的目标是「用尽可能少的 FEA 次数找危险工况」，后面 T5 采样、T8 基线对比都需要**一次提交一批工况**。

要做：
- 新增工具 `run_fea_batch(cases: list)`，`cases` 是参数对象数组
- 返回逐个结果，并**汇总**：
  - `total`、`ok_count`、`failed_count`
  - `unsafe_indices`：`check_limits` 判定为超限的工况下标数组
  - 失败原因分类统计
- 注意**单次批量不要太大**（建议上限 20 个），避免单次调用超时

#### 缺口 4：结果落盘 + 可复现

T8（基线对比与日志）需要历史结果可比。

要做：
- `run_fea` / `run_fea_batch` 每次执行后，把结果以 JSON 追加写入 `report/fea_runs.jsonl`（每行一条记录）
- 每条记录必须含：`timestamp`、`params`、`ok`、`data` 或 `error`、`duration_ms`
- 用文件锁或原子追加，避免并发写坏文件
- 目录不存在就自动创建

#### 保持向后兼容

- `dispatch(name, args)` 的调用签名不变（MCP 服务器和 harness 都在用它）
- `TOOL_SCHEMAS` 里现有三个工具的 `name` 和入参字段名不要改（改了 `mcp/server.py` 和 `harness_example.py` 都会失效）
- 新增 `run_fea_batch` 时，同步在 `TOOL_SCHEMAS` 里加一份 Schema，并**实测验证** MCP 的 `tools/list` 能返回 4 个工具

---

### 代码规范

- 注释用中文，与现有风格一致
- **不要把 API Key 写进任何代码**，继续用 `.env`（已 gitignore）
- 不要引入第三方依赖（本机 venv 没有 `pydantic` 之类的额外校验库，标准库足够）
- 修改后确认 `agent/harness_example.py` 和 `mcp/server.py` 仍能正常 import
- `fea/frame_model.py` 的 `import PyNite` **本轮不要动**

---

### 验收：请你逐条实测并贴出原始输出

1. **自测脚本**：新建 `agent/tools_selftest.py`，覆盖以下场景并打印实际结果：
   - 正常入参调用 `list_param_space` / `check_limits`
   - 传非法 `col_mm=450` → 校验错误结构是否正确
   - 漏字段 → 校验错误是否指出缺失字段
   - `run_fea_batch` 传 3 个算例（其中至少 1 个故意非法）
   - 落盘后读取 `report/fea_runs.jsonl`，确认 JSON 格式正确、行数对得上
   - 未知工具名 → `unknown_tool` 错误

2. **回归验证**（必须确认没打破现有功能）：
   - `agent/tools.py` 直接运行的自测输出
   - `python -c "import agent.harness_example"` 无报错
   - MCP 服务器喂 JSON-RPC 握手，确认 `tools/list` 返回 **4** 个工具

3. 贴出你实际执行的命令和原始输出。

---

### 交付说明

1. 改了哪些文件、各改了什么
2. 上面 3 类验收的**实际命令与原始输出**
3. `run_fea` 相关的实际结果——**如实说明它是否可用**，不要编
4. 你认为 T4（正式 harness 闭环）还需要补什么

遇到拿不准的，动手实测，别猜。
