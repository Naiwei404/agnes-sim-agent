"""T4 harness 闭环离线自测：用假 Agnes 客户端 + 假 dispatch 验证预算/终止/总结逻辑。

不消耗真实 API / 不跑真实 FEA。
运行：venv\\Scripts\\python.exe agent/t4_harness_selftest.py
"""
import os
import sys
import json
import types

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)
import agent.harness_example as H  # noqa: E402


# ---- 假 OpenAI 客户端 ----
class _TC:
    def __init__(self, name, args):
        self.id = "call_" + name
        self.function = types.SimpleNamespace(name=name, arguments=json.dumps(args))


class _Msg:
    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content


class _Resp:
    def __init__(self, msg):
        self.choices = [types.SimpleNamespace(message=msg)]


class _FakeCompletions:
    def __init__(self, script):
        self._script = list(script)

    def create(self, **kw):
        if self._script:
            return self._script.pop(0)
        return _Resp(_Msg(None, "完成"))


class _FakeClient:
    def __init__(self, script):
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(script))


# ---- 假 dispatch（确定性信封，不跑真实 FEA）----
def fake_dispatch(name, args):
    if name == "list_param_space":
        return {"ok": True, "tool": name, "data": {
            "stories": [3, 4, 5], "col_mm": [400, 500, 600],
            "beam": [[500, 250], [600, 300]], "conc": ["C30", "C35"],
            "live_load": [2, 3, 5, 7], "lateral": ["S", "M", "L"]}}
    if name == "run_fea":
        ll = args.get("live_load", 2)
        util = 0.5 if ll <= 3 else 1.5
        drift = 0.0005 if ll <= 3 else 0.003
        return {"ok": True, "tool": name, "data": {
            "max_drift": drift, "top_disp": 0.01, "max_util": util,
            "base_shear": 100.0, "converged": True, "err": None}}
    if name == "run_fea_batch":
        pl = args.get("params_list", [])
        data = []
        for p in pl:
            ll = p.get("live_load", 2)
            util = 0.5 if ll <= 3 else 1.5
            drift = 0.0005 if ll <= 3 else 0.003
            data.append({"ok": True, "tool": "run_fea", "data": {
                "max_drift": drift, "top_disp": 0.01, "max_util": util,
                "base_shear": 100.0, "converged": True, "err": None}})
        return {"ok": True, "tool": name, "data": data,
                "summary": {"total": len(pl), "invalid": 0,
                            "ran": len(pl), "ok": len(pl), "failed": 0}}
    if name == "check_limits":
        return {"ok": True, "tool": name, "data": {"passed": True, "violations": []}}
    return {"ok": False, "tool": name, "error_type": "unknown_tool", "message": "?"}


# ---- 工具 ----
SINGLE = {"stories": 3, "col_mm": 600, "beam": [600, 300], "conc": "C35",
          "live_load": 2, "lateral": "S"}
BATCH3 = [
    {"stories": 3, "col_mm": 600, "beam": [600, 300], "conc": "C35", "live_load": 2, "lateral": "S"},
    {"stories": 4, "col_mm": 500, "beam": [500, 250], "conc": "C35", "live_load": 3, "lateral": "M"},
    {"stories": 5, "col_mm": 400, "beam": [500, 250], "conc": "C30", "live_load": 2, "lateral": "L"},
]


def run_scenario(name, script, **budget):
    H.dispatch = fake_dispatch
    H.require_key = lambda: None
    H.OpenAI = lambda **kw: _FakeClient(script)
    save = os.path.join(_PROJ, "report", f"_t4_selftest_{name}.jsonl")
    if os.path.exists(save):
        os.remove(save)
    summary = H.main(save_path=save, **budget)
    return summary


def check(cond, label):
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        check.failed += 1


check.failed = 0

# 场景 A：模型主动结束（model_finished）
sA = [
    _Resp(_Msg([_TC("list_param_space", {}), _TC("run_fea", SINGLE)])),
    _Resp(_Msg([_TC("run_fea_batch", {"params_list": BATCH3})])),
    _Resp(_Msg(None, "结论：轻工况通过，重工况超限，建议加大截面。")),
]
sumA = run_scenario("A", sA, max_rounds=12, max_fea=30)
check(sumA["term_reason"] == "model_finished", "A 终止原因=model_finished")
check(sumA["fea_cases"] == 4, f"A FEA 次数=4 (实际 {sumA['fea_cases']})")
check(sumA["n_total"] == 4, f"A 采样总数=4 (实际 {sumA['n_total']})")
check(sumA["n_pass"] == 4 and sumA["n_fail"] == 0, "A 全部通过")
check(sumA["best_pass"] is not None, "A 有最优通过设计")

# 场景 B：FEA 预算耗尽（fea_budget_exhausted）
sB = [
    _Resp(_Msg([_TC("run_fea", SINGLE)])),
    _Resp(_Msg([_TC("run_fea_batch", {"params_list": BATCH3 + BATCH3})])),  # 6 工况 > 剩余预算
]
sumB = run_scenario("B", sB, max_rounds=12, max_fea=3)
check(sumB["term_reason"] == "fea_budget_exhausted", "B 终止原因=fea_budget_exhausted")
check(sumB["fea_cases"] == 1, f"B FEA 次数=1 (实际 {sumB['fea_cases']})")
check(sumB["n_total"] == 1, f"B 采样总数=1 (实际 {sumB['n_total']})")

# 场景 C：轮次预算耗尽（round_budget_exhausted）
sC = [
    _Resp(_Msg([_TC("run_fea", SINGLE)])),  # 第 0 轮仍在调用工具
]
sumC = run_scenario("C", sC, max_rounds=1, max_fea=30)
check(sumC["term_reason"] == "round_budget_exhausted", "C 终止原因=round_budget_exhausted")
check(sumC["rounds"] == 1, f"C 轮次=1 (实际 {sumC['rounds']})")

# 清理临时文件
for n in ("A", "B", "C"):
    p = os.path.join(_PROJ, "report", f"_t4_selftest_{n}.jsonl")
    if os.path.exists(p):
        os.remove(p)

print()
if check.failed == 0:
    print("ALL PASS —— T4 闭环逻辑（预算/终止/总结）验证通过")
else:
    print(f"有 {check.failed} 项 FAIL")
    sys.exit(1)
