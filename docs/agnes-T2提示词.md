# 交给 agnes-3.0-flash 的提示词（T2：真实 FEA 接入）

> 用法：把下面「提示词正文」整段发给 agnes-3.0-flash。
> 当前阻塞：`fea/frame_model.py` 代码已写完，但 `import PyNite` 在本机报 ModuleNotFoundError，
> 需要 agnes 定位导入问题 + 跑通验证 + 接进 harness。

---

## 提示词正文

你要帮我完成一个结构工程仿真 Agent 项目的 T2 任务。项目在 Windows 11 上，用 Python + PyNite 做真实有限元分析，模型后端是 agnes-3.0-flash（也就是你自己），通过 OpenAI 兼容 API 调用你来做工具决策。

### 项目背景

- 项目目录：`D:\WorkBuddy\AI工作区\03-代码项目\agnes-sim-agent`
- 用途：江苏省高校 AI+黑客松（赛道 B3，科学计算与仿真）。目标是用尽可能少的 FEA 次数，找出不满足规范限值的危险工况。
- 虚拟环境：项目内 `venv\`（Python 3.13，已装 PyNiteFEA 3.2.0、openai 3.24、numpy、scipy、pandas、matplotlib、scikit-learn）

已有文件：
- `config.py`：读取 `.env` 里的 `AGNES_API_KEY` / `AGNES_BASE_URL` / `AGNES_MODEL`
- `test_api.py`：连通性测试（普通对话 + function calling），已跑通
- `agent/harness_example.py`：function calling 工具循环，工具为 `list_param_space` / `run_fea` / `check_limits`
- `fea/frame_model.py`：我已写好的真实 FEA 封装（本次要修的就是它）
- `README.md`：使用说明

### 我已经确认的 PyNite API 事实（这些是准确的，可直接用）

- 全局坐标 **Y 是竖直方向**，重力为全局 `FY` 负向；X、Z 是水平方向。
- `model.add_node(name, X, Y, Z)`
- `model.def_support(name, DX, DY, DZ, RX, RY, RZ)` 六个 bool
- `model.add_material(name, E, G, nu, rho, fy=None)`，`rho` 是质量密度 kg/m³
- `model.add_section(name, A, Iy, Iz, J)`，单位 m² / m⁴
- `model.add_member(name, i_node, j_node, material, section, rotation=0.0)`，rotation 单位为度
- `model.add_member_dist_load(member, direction, w1, w2, x1=None, x2=None, case='Case 1')`，direction 大写为全局坐标（`'FX'/'FY'/'FZ'`），小写为梁局部坐标
- `model.add_node_load(node, direction, P, case='Case 1')`
- `model.add_load_combo(name, factors)`，factors 形如 `{'Case 1': 1.0}`
- `model.analyze_linear(log=False, check_stability=True)`
- 结果取用：
  - 节点位移：`model.nodes['节点名'].DX['Combo 1']`（`.DY` / `.DZ` 同理）
  - 节点反力：`node.RxnFX['Combo 1']` 等
  - 构件轴力：`member.max_axial('Combo 1')` / `member.min_axial('Combo 1')`（**是方法，要加括号和参数**）
  - 构件弯矩：`member.max_moment('Mz'|'My', 'Combo 1')`
  - 构件剪力：`member.max_shear('Fy'|'Fz', 'Combo 1')`
  - 遍历构件：`model.members` 是 `{name: member}` 字典
- **注意**：模型对象访问是 `model.nodes[...]` 和 `model.members[...]`（小写属性），没有 `GetNode()` 这类方法。
- **注意**：`max_axial` 等是方法不是属性，写成 `member.max_axial`（不加括号）会拿到方法对象而不是数值。

### 当前阻塞问题（你的首要任务）

`fea/frame_model.py` 第 27 行 `import PyNite` 抛 `ModuleNotFoundError: No module named 'PyNite'`。

已排除的可能：
- 包**确实存在**：`venv\Lib\site-packages\Pynite\` 目录完整（有 `__init__.py`、`FEModel3D.py`、`Member3D.py` 等），且 `pynitefea-3.2.0.dist-info` 的 `top_level.txt` 内容是 `Pynite`。
- `venv\Lib\site-packages` **确实在 `sys.path` 里**（我打印过）。
- `pip list` 显示 `PyNiteFEA 3.2.0` 已安装。
- `__init__.py` 内容是 `from Pynite.FEModel3D import FEModel3D` 等，说明包内绝对导入是自洽的。
- 已清空所有 `__pycache__`，仍失败。
- 循环 5 次导入，5 次全部失败（**不是偶发，是稳定失败**）。
- 我判断的「中文路径 Unicode 归一化」假设**尚未被证实**——因为我用 `os.path.realpath` 归一化后插到 `sys.path[0]` 依然失败。
- 奇怪之处：有一次我在调试脚本里执行 `import PyNite` **成功**了并打印出正确 `__file__`（`...venv\Lib\site-packages\Pynite\__init__.py`），但同一台机器上正式脚本里就失败。请你独立复现并给出确定结论，不要采信我的任何推测。

项目路径含中文（`D:\WorkBuddy\AI工作区\...`），这是重要嫌疑，但请自行验证。venv 解释器绝对路径：
`D:\WorkBuddy\AI工作区\03-代码项目\agnes-sim-agent\venv\Scripts\python.exe`

### 需要你交付的内容

1. **定位并修复导入问题**，让 `import PyNite` 在本机稳定成功。可尝试方向（不限于）：
   - 用 `importlib.util.spec_from_file_location` 从绝对路径加载包
   - 在纯英文路径（如 `D:\pynite_ws`）重建 venv 或把项目迁到英文路径
   - 检查是否 `pyvenv.cfg` / `sys.prefix` 有异常
   请选一个**能真正跑通**的方案，并说明为什么有效。

2. **跑通 `fea/frame_model.py` 的自测**（文件末尾 `__main__` 里有 3 个算例）。确认能输出合理的 `max_drift` / `top_disp` / `max_util` / `base_shear`。请检查数值是否符合结构力学常识：
   - 3 层框架在常规荷载下，`max_drift`（层间位移角）通常在 1/1000 ~ 1/300 量级
   - `top_disp` 应为毫米级到厘米级
   - 若出现 0 或数量级明显不对，说明提取逻辑有问题，请修正

3. **自测通过后**，用真实 FEA 跑一次完整 harness 闭环：
   ```
   venv\Scripts\python.exe agent\harness_example.py
   ```
   确认 Agnes 调 `run_fea` 时拿到的是**真实 PyNite 计算结果**（不是 mock），且 `check_limits` 能基于真实结果判定。注意：**这个命令会调用 agnes API，需要走代理** `http://127.0.0.1:54476`（设 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量）。

### 建模参数（`run_fea` 接收）

| 参数 | 含义 | 取值 |
|---|---|---|
| `stories` | 层数 | 3 / 4 / 5 |
| `col_mm` | 柱截面边长(mm，正方形) | 400 / 500 / 600 |
| `beam` | 梁 [宽,高](mm) | [500,250] / [600,300] |
| `conc` | 混凝土等级 | "C30" / "C35" |
| `live_load` | 楼面活载(kN/m²) | 2 / 3 / 5 / 7 |
| `lateral` | 水平荷载等级 | "S" / "M" / "L" |

几何约定：层高 3.0m，X 向 3 跨（4 条柱列，柱距 6m），Z 向 2 跨（3 条柱列，柱距 6m），底层节点全固接。单位统一 SI（m / N / Pa）。

### 代码规范要求

- 混凝土弹性模量按 GB50010：`Ec = 1e5 / (2.2 + 34.7/fck) * 1e6` Pa（fck 取 30 或 35）
- 保留现有的工具返回契约（`max_drift` / `top_disp` / `max_util` / `base_shear` / `converged` / `err`），因为 `check_limits` 和 harness 依赖它
- `run_fea` 必须捕获异常返回 `converged: False`，**不能抛异常中断 agent 循环**
- 注释用中文，与现有代码风格一致
- 不要把 API Key 写进代码，继续用 `.env`（已 gitignore）

### 交付时请说明

1. 导入失败的**根本原因**（不要给猜测，给你验证过的结论）
2. 你采取的修复方案 + 为什么有效
3. 3 个自测算例的**实际输出数值**，以及你对数值合理性的判断
4. harness 闭环是否跑通，Agnes 实际调用 `run_fea` 拿到的是否为真实 FEA 结果
5. 过程中发现的任何其他问题

遇到拿不准的地方，直接动手实测验证，不要空谈。
