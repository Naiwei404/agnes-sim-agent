"""Agnes 用法示范（参考实现，非参赛最终代码）。T4 正式 harness 闭环。

闭环构成：
  系统提示(结构工程仿真助手) -> Agnes(agnes-3.0-flash) 决策 ->
  function calling 调本地工具 -> 本地执行(dispatch) -> 结果回灌 -> 下一轮。

T4 新增（相对旧版固定 6 轮）：
  - 参数化硬预算：最大轮次 max_rounds / 最大 FEA 次数 max_fea / 最大墙钟秒数 max_seconds。
  - 硬终止：任意预算耗尽即停（fea_budget_exhausted / round_budget_exhausted / time_budget_exhausted）；
    模型主动停止工具调用也视为收敛（model_finished）。
  - 预算拦截：FEA 调用若会超出 max_fea，返回 harness 级 error_type="budget_exhausted" 信封，
    要求模型立即收尾，不再发起新采样。
  - 会话级总结：每轮累积真实 FEA 数据点，终止时算出 通过/超限计数、最优通过设计、最危险工况、
    危险区分布，并落盘到 report/harness_sessions.jsonl。

运行：
  venv\\Scripts\\python.exe agent/harness_example.py                 # 默认预算
  venv\\Scripts\\python.exe agent/harness_example.py --fea 20 --rounds 10 --seconds 120
依赖：openai、PyNiteFEA（run_fea 真实调用时）、config(.env 中 AGNES_API_KEY)
"""
import sys
import os
import json
import time
import argparse
import datetime
from collections import Counter

# 保证无论从项目根目录还是 agent/ 子目录运行，都能 import 到根目录的 config / fea 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL, require_key

# 工具实现统一收敛到 agent.tools（与 MCP 服务器共用同一份逻辑）
from agent.tools import dispatch
from agent.tools import TOOL_SCHEMAS

# 统一为 OpenAI function calling 需要的 {"type":"function","function":{...}} 结构
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["inputSchema"],
        },
    }
    for s in TOOL_SCHEMAS
]

# 系统提示词统一从 system_prompt.txt 读取，避免与文档漂移（单一来源）。
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")
with open(_PROMPT_PATH, encoding="utf-8") as _fh:
    SYSTEM_PROMPT = _fh.read()

# 规范限值（与 export_runs.py / tools.py 保持一致，单一事实来源）
LIMIT_DRIFT = 1.0 / 550.0   # 弹性层间位移角限值 1/550
LIMIT_UTIL = 1.0            # 构件承载力利用率限值

# 会话级总结落盘路径（项目内 report/）
_SESSIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "report", "harness_sessions.jsonl",
)


def _json_default(o):
    """JSON 序列化兜底：numpy 标量转 native（.item()），其余退回 str 而非报错。"""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


# ---- 本地工具执行：统一走 agent.tools.dispatch ----
def local_exec(name, args):
    return dispatch(name, args)


def _cost_of(name, args):
    """一次工具调用折算的「真实 FEA 工况数」（物理仿真成本）。"""
    if name == "run_fea":
        return 1
    if name == "run_fea_batch":
        pl = (args or {}).get("params_list") or []
        return max(1, len(pl))
    return 0


def _push_fea(feas, params, d):
    md = d.get("max_drift")
    mu = d.get("max_util")
    if md is None or mu is None:
        return
    conv = d.get("converged")
    # 注意：frame_model 返回的是 numpy.bool / numpy.float64；numpy.bool 不是 Python bool 的子类，
    # json 会退回 default 导致写成字符串 "True"。显式转 native 类型避免该问题。
    passed = bool((conv is not False) and (md <= LIMIT_DRIFT) and (mu <= LIMIT_UTIL))
    feas.append({
        "params": params,
        "max_drift": md,
        "max_util": mu,
        "converged": conv,
        "passed": passed,
        "drift_ratio": md / LIMIT_DRIFT,
        "util_ratio": mu / LIMIT_UTIL,
    })


def _unwrap_feas_item(it):
    """批量项可能是信封 {ok:true, data:{...}}（自测）或原始结果 {max_drift,...}（真实 dispatch）。

    返回可用的结果 dict；非法/无数值则返回 None。
    """
    if not isinstance(it, dict):
        return None
    if it.get("ok") is True and isinstance(it.get("data"), dict):
        return it.get("data")
    if "max_drift" in it or "max_util" in it:
        return it
    return None


def _record_feas(env, args, feas):
    """从工具信封中累积真实 FEA 数据点，供终止时总结。"""
    if not isinstance(env, dict) or env.get("ok") is not True:
        return
    tool = env.get("tool")
    if tool == "run_fea":
        d = env.get("data")
        if isinstance(d, dict):
            _push_fea(feas, args, d)
    elif tool == "run_fea_batch":
        items = env.get("data") or []
        pl = (args or {}).get("params_list") or []
        for idx, it in enumerate(items):
            pa = pl[idx] if idx < len(pl) else {}
            d = _unwrap_feas_item(it)
            if d is not None:
                _push_fea(feas, pa, d)


def _margin(f):
    """安全裕度：距限值越近比值越接近 1，越小越危险；取位移角/利用率两者中更紧的一条。"""
    return min(f["drift_ratio"], f["util_ratio"])


def _summarize(feas, rounds_done, fea_used, term_reason, elapsed, budget,
               started_at, ended_at):
    total = len(feas)
    passes = [f for f in feas if f["passed"]]
    fails = [f for f in feas if not f["passed"]]
    best = max(passes, key=_margin) if passes else None
    worst = (max(fails, key=lambda f: max(f["drift_ratio"], f["util_ratio"]))
             if fails else None)
    by_lateral = Counter(f["params"].get("lateral")
                         for f in fails if isinstance(f["params"], dict))
    top_danger = sorted(fails, key=lambda f: -max(f["drift_ratio"], f["util_ratio"]))[:3]
    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "rounds": rounds_done,
        "fea_cases": fea_used,
        "term_reason": term_reason,
        "elapsed_seconds": round(elapsed, 1),
        "n_total": total,
        "n_pass": len(passes),
        "n_fail": len(fails),
        "best_pass": best,
        "worst_fail": worst,
        "dangerous_by_lateral": dict(by_lateral),
        "top_danger": top_danger,
        "budget": budget,
    }


def _print_report(summary):
    bar = "=" * 56
    print(bar)
    print("闭环总结 harness session")
    print(bar)
    print(f"终止原因 : {summary['term_reason']}")
    print(f"轮次/FEA : {summary['rounds']} 轮 / {summary['fea_cases']} 工况 "
          f"({summary['elapsed_seconds']}s)")
    print(f"结果统计 : 总 {summary['n_total']} ｜ 通过 {summary['n_pass']} ｜ "
          f"超限 {summary['n_fail']}")
    bp = summary["best_pass"]
    if bp:
        p = bp["params"]
        print(f"最优通过 : {p.get('stories')}层/{p.get('col_mm')}柱/{p.get('conc')}"
              f"/活载{p.get('live_load')}/{p.get('lateral')}向  "
              f"drift={bp['max_drift']:.2e} util={bp['max_util']:.3f}")
    wf = summary["worst_fail"]
    if wf:
        p = wf["params"]
        print(f"最危险   : {p.get('stories')}层/{p.get('col_mm')}柱/{p.get('conc')}"
              f"/活载{p.get('live_load')}/{p.get('lateral')}向  "
              f"drift比={wf['drift_ratio']:.2f} util比={wf['util_ratio']:.2f}")
    dl = summary["dangerous_by_lateral"]
    if dl:
        print("危险区分布(按水平荷载): " + "  ".join(f"{k}={v}" for k, v in sorted(dl.items())))
    print(bar)


def main(max_rounds=12, max_fea=30, max_seconds=0, model=None, save_path=None):
    require_key()
    model = model or AGNES_MODEL
    client = OpenAI(api_key=AGNES_API_KEY, base_url=AGNES_BASE_URL)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    feas = []          # 累积真实 FEA 数据点
    fea_used = 0       # 已消耗 FEA 工况数
    budget = {"max_rounds": max_rounds, "max_fea": max_fea, "max_seconds": max_seconds}
    start = time.monotonic()
    started_at = datetime.datetime.now().isoformat(timespec="seconds")
    term_reason = "unknown"
    budget_hit = False
    rounds_done = 0

    for i in range(max_rounds):
        rounds_done = i + 1
        # 墙钟预算
        if max_seconds and (time.monotonic() - start) > max_seconds:
            term_reason = "time_budget_exhausted"
            break
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto",
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            term_reason = "model_finished"
            messages.append(msg)
            print(f"[轮 {i}] Agnes 主动结束：{(msg.content or '')[:200]}")
            break
        messages.append(msg)  # 保留助手消息（含 tool_calls）
        for tc in msg.tool_calls:
            # 兼容 Agnes 偶发的包裹格式：function.name=="tool" 且 arguments 内含 tool_name
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            if name == "tool" and isinstance(args, dict) and "tool_name" in args:
                name = args.pop("tool_name")
            cost = _cost_of(name, args)
            # 预算拦截：FEA 调用会超出 max_fea 时，注入 harness 级预算耗尽信封
            if cost > 0 and fea_used + cost > max_fea:
                budget_hit = True
                remain = max_fea - fea_used
                print(f"[轮 {i}] 预算拦截 {name}（剩余 {remain} < 需要 {cost}）")
                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "name": name,
                    "content": json.dumps({
                        "ok": False, "tool": name, "error_type": "budget_exhausted",
                        "message": (f"FEA 预算已用尽（剩余 {remain} 次，需要 {cost} 次）。"
                                    "请停止采样，基于已得数据给出最终结论。"),
                    }, ensure_ascii=False),
                })
                continue
            print(f"[轮 {i}] 调用 {name}({args})")
            result = local_exec(name, args)
            if cost > 0:
                if name == "run_fea":
                    if isinstance(result, dict) and result.get("ok") is True:
                        fea_used += 1
                elif name == "run_fea_batch":
                    sm = result.get("summary", {}) if isinstance(result, dict) else {}
                    fea_used += sm.get("ran", 0)
            _record_feas(result, args, feas)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "name": name, "content": json.dumps(result, ensure_ascii=False),
            })
        if budget_hit:
            term_reason = "fea_budget_exhausted"
            break
    else:
        term_reason = "round_budget_exhausted"
        rounds_done = max_rounds

    elapsed = time.monotonic() - start
    ended_at = datetime.datetime.now().isoformat(timespec="seconds")
    summary = _summarize(feas, rounds_done, fea_used, term_reason, elapsed,
                         budget, started_at, ended_at)
    _print_report(summary)

    save_path = save_path or _SESSIONS_PATH
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False, default=_json_default) + "\n")
    print(f"会话总结已落盘 -> {save_path}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Agnes 仿真 Agent 闭环 harness")
    ap.add_argument("--rounds", type=int, default=12, help="最大轮次（默认 12）")
    ap.add_argument("--fea", type=int, default=30, help="最大 FEA 工况数（默认 30）")
    ap.add_argument("--seconds", type=int, default=0, help="最大墙钟秒数（0=不限制）")
    ap.add_argument("--model", type=str, default=None, help="覆盖模型名")
    ap.add_argument("--save", type=str, default=None, help="会话总结输出路径")
    args = ap.parse_args()
    main(max_rounds=args.rounds, max_fea=args.fea, max_seconds=args.seconds,
         model=args.model, save_path=args.save)
