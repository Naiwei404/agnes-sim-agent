"""结构工程仿真工具集（MCP 与 function calling 共用）。

四个工具：
  list_param_space()                返回可遍历参数空间
  run_fea(params)                   真实 PyNite FEA（来自 fea.frame_model）
  run_fea_batch(params_list)        批量 FEA，减少多轮往返
  check_limits(max_drift, max_util) 对照规范 proxy 阈值判定超限

工具层强化（T3 缺口 1 / 2）：
  - 入参校验：每个工具调用先做类型 / 取值范围 / 枚举 / 必填校验，失败时返回结构化错误
    {"ok": false, "error_type": "invalid_arguments", ...}，不再把原始 TypeError 甩给模型。
  - 统一返回信封：所有工具经 dispatch 返回 {"ok", "tool", "data", [warning]}，
    失败再附 error_type / message / invalid_fields，error_type 区分
    invalid_arguments / unknown_tool / internal_error；非收敛 FEA 视为成功并加 warning="not_converged"。

注意：check_limits 采用扁平的 max_drift / max_util 入参（与 MCP / function calling
的 JSON Schema 一致）。每次工具调用都会以 JSONL 追加持久化到 report/fea_runs.jsonl，
供 T3 智能采样 / 危险工况排序复用。
"""

import os
import json
import time
from datetime import datetime

# 规范 proxy 阈值（弹性层间位移角限值、构件承载力利用率限值）
LIMIT_DRIFT = 1.0 / 550.0
LIMIT_UTIL = 1.0

# ---- 工具调用落盘（function calling 与 MCP 共用同一份持久化） ----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # agent/tools.py -> 项目根
_RUNS_JSONL = os.path.join(_ROOT, "report", "fea_runs.jsonl")


def _persist(record: dict) -> None:
    """把单次工具调用（含参数与结果）追加写入 report/fea_runs.jsonl。"""
    os.makedirs(os.path.dirname(_RUNS_JSONL), exist_ok=True)
    with open(_RUNS_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---- 参数取值范围（与 list_param_space / DESIGN 一致） ----
STORIES_ALLOWED = [3, 4, 5]
COL_ALLOWED = [400, 500, 600]
BEAM_ALLOWED = [(500, 250), (600, 300)]
CONC_ALLOWED = ["C30", "C35"]
LIVE_ALLOWED = [2, 3, 5, 7]
LATERAL_ALLOWED = ["S", "M", "L"]


def _as_int(v):
    """把 JSON 可能传来的整数（含 3.0 这种 float）归一为 int；非法返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _validate_param_object(params):
    """校验单个 FEA 工况参数对象。

    返回 (is_valid, invalid_fields, normalized_params)。
    invalid_fields 为 [{"field", "received", "expected"}]；校验通过时 normalized_params
    为类型归一化后的参数（供 run_fea 直接消费）。
    """
    invalid = []
    if not isinstance(params, dict):
        return (False, [{"field": "(root)", "received": type(params).__name__, "expected": "object"}], None)

    for f in ("stories", "col_mm", "beam", "conc", "live_load", "lateral"):
        if f not in params:
            invalid.append({"field": f, "received": "<missing>", "expected": "必填"})

    # 缺字段时无需继续逐项校验
    if invalid:
        return (False, invalid, None)

    # stories
    s = _as_int(params["stories"])
    if s is None or s not in STORIES_ALLOWED:
        invalid.append({"field": "stories", "received": params["stories"], "expected": STORIES_ALLOWED})
    # col_mm
    c = _as_int(params["col_mm"])
    if c is None or c not in COL_ALLOWED:
        invalid.append({"field": "col_mm", "received": params["col_mm"], "expected": COL_ALLOWED})
    # beam
    beam = params["beam"]
    if not (isinstance(beam, list) and len(beam) == 2 and all(_as_int(x) is not None for x in beam)):
        invalid.append({"field": "beam", "received": beam, "expected": BEAM_ALLOWED})
    else:
        btuple = (_as_int(beam[0]), _as_int(beam[1]))
        if btuple not in BEAM_ALLOWED:
            invalid.append({"field": "beam", "received": beam, "expected": BEAM_ALLOWED})
    # conc
    if params["conc"] not in CONC_ALLOWED:
        invalid.append({"field": "conc", "received": params["conc"], "expected": CONC_ALLOWED})
    # live_load
    ll = params["live_load"]
    ll_int = _as_int(ll) if isinstance(ll, (int, float)) and not isinstance(ll, bool) else None
    if ll_int is None or ll_int not in LIVE_ALLOWED:
        invalid.append({"field": "live_load", "received": ll, "expected": LIVE_ALLOWED})
    # lateral
    if params["lateral"] not in LATERAL_ALLOWED:
        invalid.append({"field": "lateral", "received": params["lateral"], "expected": LATERAL_ALLOWED})

    if invalid:
        return (False, invalid, None)
    normalized = {
        "stories": s,
        "col_mm": c,
        "beam": [_as_int(beam[0]), _as_int(beam[1])],
        "conc": params["conc"],
        "live_load": params["live_load"],
        "lateral": params["lateral"],
    }
    return (True, [], normalized)


def _validate_check_limits(args):
    """校验 check_limits 的扁平入参：max_drift / max_util 必须为数字（非 bool）。"""
    invalid = []
    if not isinstance(args, dict):
        return (False, [{"field": "(root)", "received": type(args).__name__, "expected": "object"}], None)
    for f in ("max_drift", "max_util"):
        v = args.get(f)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            invalid.append({"field": f, "received": v, "expected": "number"})
    if invalid:
        return (False, invalid, None)
    return (True, [], {"max_drift": float(args["max_drift"]), "max_util": float(args["max_util"])})


def _ok(tool, data, warning=None) -> dict:
    """成功信封。"""
    env = {"ok": True, "tool": tool, "data": data}
    if warning:
        env["warning"] = warning
    return env


def _invalid(tool, invalid_fields) -> dict:
    """校验失败信封（error_type=invalid_arguments）。"""
    msgs = "; ".join(
        f"{f['field']}(收到 {f['received']!r}, 期望 {f['expected']!r})" for f in invalid_fields
    )
    return {
        "ok": False,
        "tool": tool,
        "error_type": "invalid_arguments",
        "message": "入参校验失败: " + msgs,
        "invalid_fields": invalid_fields,
    }


def _dispatch_batch(name, args):
    """run_fea_batch 的信封封装：逐项校验，合法项跑 FEA，非法项返回 per-case 校验错误。"""
    params_list = args.get("params_list") if isinstance(args, dict) else None
    if not isinstance(params_list, list):
        return _invalid(name, [{"field": "params_list", "received": type(params_list).__name__, "expected": "array"}])
    run_fea = get_run_fea()
    items = []
    summary = {"total": len(params_list), "invalid": 0, "ran": 0, "ok": 0, "failed": 0}
    for p in params_list:
        ok, inv, norm = _validate_param_object(p)
        if not ok:
            summary["invalid"] += 1
            items.append(_invalid(name, inv))
            continue
        data = run_fea(norm)
        summary["ran"] += 1
        if isinstance(data, dict) and data.get("converged") is False:
            summary["failed"] += 1
        else:
            summary["ok"] += 1
        items.append(data)
    return {"ok": True, "tool": name, "data": items, "summary": summary}


def list_param_space() -> dict:
    """返回可遍历参数空间与可选水平。"""
    return {
        "stories": [3, 4, 5],
        "col_mm": [400, 500, 600],
        "beam": [[500, 250], [600, 300]],
        "conc": ["C30", "C35"],
        "live_load": [2, 3, 5, 7],
        "lateral": ["S", "M", "L"],
    }


def check_limits(max_drift: float, max_util: float) -> dict:
    """对照规范 proxy 阈值，判断一组结果是否超限。

    :param max_drift: 弹性层间位移角（无量纲）
    :param max_util: 最大构件承载力利用率（无量纲）
    """
    violations = []
    if max_drift is not None and max_drift > LIMIT_DRIFT:
        violations.append({
            "item": "弹性层间位移角",
            "value": max_drift,
            "limit": LIMIT_DRIFT,
        })
    if max_util is not None and max_util > LIMIT_UTIL:
        violations.append({
            "item": "构件承载力利用率",
            "value": max_util,
            "limit": LIMIT_UTIL,
        })
    return {"passed": len(violations) == 0, "violations": violations}


def get_run_fea():
    """延迟导入 fea.frame_model.run_fea。

    单独返回结果而不直接 import，是为了在 PyNite 不可用时仍能让
    list_param_space / check_limits 正常提供服务。
    """
    from fea.frame_model import run_fea
    return run_fea


def run_fea_batch(params_list: list) -> list:
    """批量跑 FEA，入参为参数 dict 列表；逐个调用 fea.frame_model.run_fea。

    frame_model.run_fea 自身已对异常做优雅降级（返回非收敛结果），故此处直接映射即可。
    返回的列表与入参顺序一一对应。
    """
    run_fea = get_run_fea()
    return [run_fea(p) for p in (params_list or [])]


def dispatch(name: str, args: dict) -> dict:
    """按名字分发工具调用，并把每次调用（含参数与结果）持久化到 fea_runs.jsonl。

    返回「统一信封」：成功 {"ok": true, "tool": name, "data": <原始返回>}；
    失败 {"ok": false, "tool": name, "error_type": ..., "message": ...}。
    error_type 区分 invalid_arguments / unknown_tool / internal_error；
    非收敛 FEA 仍记为成功，但在信封加 warning="not_converged"。
    """
    record_base = {"tool": name, "args": args}
    start = time.monotonic()
    try:
        if name == "list_param_space":
            env = _ok(name, list_param_space())
        elif name == "run_fea":
            ok, inv, norm = _validate_param_object(args)
            if not ok:
                env = _invalid(name, inv)
            else:
                data = get_run_fea()(norm)
                warn = "not_converged" if (isinstance(data, dict) and data.get("converged") is False) else None
                env = _ok(name, data, warning=warn)
        elif name == "run_fea_batch":
            env = _dispatch_batch(name, args)
        elif name == "check_limits":
            ok, inv, norm = _validate_check_limits(args)
            if not ok:
                env = _invalid(name, inv)
            else:
                env = _ok(name, check_limits(**norm))
        else:
            env = {"ok": False, "tool": name, "error_type": "unknown_tool",
                   "message": f"未知工具: {name}"}
    except Exception as e:  # noqa: BLE001
        # 任何未预期的异常都收口为 internal_error，不让原始栈信息直接甩给模型
        env = {"ok": False, "tool": name, "error_type": "internal_error",
               "message": f"{type(e).__name__}: {e}"}
    record = {
        **record_base,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "duration_ms": round((time.monotonic() - start) * 1000, 1),
        "result": env,
    }
    _persist(record)
    return env


# ---- 暴露给模型端的 JSON Schema（function calling 与 MCP 共用） ----
# 单个 FEA 工况的参数对象（run_fea 与 run_fea_batch 共用）
_PARAM_OBJECT = {
    "type": "object",
    "description": "一次 3D 混凝土框架 FEA 工况的参数",
    "properties": {
        "stories": {"type": "integer", "description": "层数"},
        "col_mm": {"type": "integer", "description": "柱截面边长(mm，正方形)"},
        "beam": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "梁截面 [宽,高](mm)",
        },
        "conc": {"type": "string", "description": "混凝土等级，如 C30/C35"},
        "live_load": {"type": "number", "description": "楼面活载(kN/m²)"},
        "lateral": {"type": "string", "description": "水平荷载等级 S/M/L"},
    },
    "required": ["stories", "col_mm", "beam", "conc", "live_load", "lateral"],
}

TOOL_SCHEMAS = [
    {
        "name": "list_param_space",
        "description": "返回可遍历参数空间与可选水平（层数/柱截面/梁截面/混凝土等级/活载/水平荷载等级）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_fea",
        "description": "用 PyNite 跑一次 3D 混凝土框架 FEA，返回层间位移角/顶点位移/承载力利用率/基底剪力",
        "inputSchema": _PARAM_OBJECT,
    },
    {
        "name": "run_fea_batch",
        "description": "批量跑多次 3D 框架 FEA（一次返回多工况结果），用于围绕危险区加密采样、减少多轮往返",
        "inputSchema": {
            "type": "object",
            "properties": {
                "params_list": {
                    "type": "array",
                    "items": _PARAM_OBJECT,
                    "description": "多个工况参数对象的列表",
                },
            },
            "required": ["params_list"],
        },
    },
    {
        "name": "check_limits",
        "description": "对照规范 proxy 阈值（层间位移角 1/550、利用率 1.0）判断是否超限",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_drift": {"type": "number", "description": "弹性层间位移角"},
                "max_util": {"type": "number", "description": "最大构件承载力利用率"},
            },
            "required": ["max_drift", "max_util"],
        },
    },
]


if __name__ == "__main__":
    import json
    print("list_param_space ->", json.dumps(dispatch("list_param_space", {}), ensure_ascii=False, indent=2))
    print("check_limits(ok) ->", json.dumps(dispatch("check_limits", {"max_drift": 1 / 600, "max_util": 0.9}), ensure_ascii=False))
    print("check_limits(超限) ->", json.dumps(dispatch("check_limits", {"max_drift": 1 / 400, "max_util": 1.2}), ensure_ascii=False))
    print("check_limits(类型错) ->", json.dumps(dispatch("check_limits", {"max_drift": "x", "max_util": 0.9}), ensure_ascii=False))
    print("run_fea(col_mm=450 非法) ->", json.dumps(
        dispatch("run_fea", {"stories": 3, "col_mm": 450, "beam": [500, 250], "conc": "C30", "live_load": 3, "lateral": "S"}),
        ensure_ascii=False))
