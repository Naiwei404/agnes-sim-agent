# 混凝土框架多工况智能仿真 Agent —— 环境与使用指南

本目录是按 DESIGN.md（资料库文档 `3SfJhYnTtK1TTKdTjTjnan`）搭建的**运行环境 + 配置**。
Agnes 在本项目里不是用 `agnes-harness` 源码，而是作为**模型后端**：通过 OpenAI 兼容 API 调用
`agnes-3.0-flash`。

## 1. 环境

- Python 虚拟环境：`venv/`（由 `config.py` 使用的依赖已装好）
- 依赖：`openai`、`PyNiteFEA`、`numpy`、`pandas`、`matplotlib`、`scikit-learn`
- 模型网关（中国区）：`https://api.agnes-ai.cn/v1`（OpenAI 兼容）
  - ⚠️ `apihub.agnes-ai.com` 是**国际站**，**会拒绝中国 Key**，别用。
  - 上述结论来自 agnes-harness 源码注释与官方文档双重确认。
- 模型：`agnes-3.0-flash`（已实测支持 function calling）
- 参赛编号：**U018**

## 2. 配置 Agnes Key

密钥写在 `.env`（已被 `.gitignore` 忽略，**绝不提交**）：

```
AGNES_API_KEY=sk-...        # 邮件里的参赛 Key
AGNES_BASE_URL=https://api.agnes-ai.cn/v1
AGNES_MODEL=agnes-3.0-flash
```

`config.py` 会自动读取系统环境变量，未设置时回退读取项目根 `.env`。

## 3. 跑通验证（T1）

```powershell
venv\Scripts\python.exe test_api.py
```

预期：打印普通对话返回 + 一次 function calling 往返，结尾 `==> Agnes API 连通 OK`。

## 4. Agnes 怎么用（核心模式）

见 `agent/harness_example.py`：系统提示(结构工程仿真助手) → `agnes-3.0-flash` 决策 →
function calling 调本地工具(`run_fea`/`check_limits`/`list_param_space`) → 本地执行(PyNite) →
结果回灌 → 下一轮，直到预算用尽 / 无新危险工况。

工具层 JSON Schema 与异常处理见 DESIGN.md §5。智能采样策略见 §6（A 主做 / B 加分）。

## 5. 你接下来要做（按 DESIGN T2–T9）

- T2 `fea/frame_model.py`：用 `PyNite.FEModel3D` 建 3D 混凝土框架，真实替换 `run_fea` 的 mock
- T3 工具层：补 `run_fea`/`check_limits`/`list_param_space` + Schema + 异常回填
- T4 `agent/harness.py`：把 `harness_example.py` 扩成正式闭环（记忆 / 重试）
- T5 采样策略 A；T6 GP 代理模型(加分)；T7 报告；T8 基线对比+日志；T9 提交材料
