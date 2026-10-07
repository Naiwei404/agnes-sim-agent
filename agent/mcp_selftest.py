# -*- coding: utf-8 -*-
"""
mcp_selftest.py —— 不依赖任何 UI，直接用 JSON-RPC 2.0 over stdio 联调 MCP 服务器。

作用：模拟 Cherry Studio 等 MCP 客户端的握手与调用流程，验证
  initialize -> notifications/initialized -> tools/list -> tools/call
在真实子进程下是否跑通，尤其是现在 PyNite 已装好的 run_fea / run_fea_batch 真实 FEA。

运行（项目 venv python，需能 import pynite）：
  venv\\Scripts\\python.exe agent\\mcp_selftest.py
"""
import os
import sys
import json
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
SERVER = os.path.join(_PROJECT, "mcp", "server.py")
PY = os.path.join(_PROJECT, "venv", "Scripts", "python.exe")

REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "mcp_selftest", "version": "1.0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "list_param_space", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "check_limits",
                "arguments": {"max_drift": 0.0025, "max_util": 1.2}}},
    # 真实 FEA：3 层框架，取空间里一个合法组合（beam 只能是 [500,250] 或 [600,300]）
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "run_fea",
                "arguments": {"stories": 3, "col_mm": 500, "beam": [600, 300],
                              "conc": "C30", "live_load": 3.0, "lateral": "M"}}},
    # 批量 FEA：2 个合法工况
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "run_fea_batch", "arguments": {"params_list": [
         {"stories": 4, "col_mm": 400, "beam": [500, 250],
          "conc": "C30", "live_load": 7.0, "lateral": "L"},
         {"stories": 5, "col_mm": 600, "beam": [600, 300],
          "conc": "C35", "live_load": 2.0, "lateral": "S"},
     ]}}},
    # 入参非法：验证信封化错误（不应让进程崩溃，应返回结构化 invalid_arguments）
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "run_fea",
                "arguments": {"stories": 3, "col_mm": 450, "beam": [300, 600],
                              "conc": "C30", "live_load": 3, "lateral": "S"}}},
]


def _unwrap(resp_msg):
    """从 tools/call 响应里取出 (is_error, data_dict)。

    服务器把 dispatch 的统一信封原样作为 text 返回；真正的结果在 data 字段内。
    """
    if not resp_msg or "result" not in resp_msg:
        return (True, {})
    res = resp_msg["result"]
    is_err = bool(res.get("isError"))
    try:
        body = json.loads(res["content"][0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError):
        return (is_err, {})
    data = body.get("data")
    if isinstance(data, dict):
        return (is_err, data)
    # run_fea_batch 的 data 是列表；包一层以便统一处理
    if isinstance(data, list):
        return (is_err, {"__list__": data})
    return (is_err, body)


def main():
    if not os.path.exists(PY):
        print(f"[FAIL] venv python 不存在: {PY}")
        return 1
    if not os.path.exists(SERVER):
        print(f"[FAIL] server 不存在: {SERVER}")
        return 1

    proc = subprocess.Popen(
        [PY, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", text=True,
    )
    stdin_data = "\n".join(json.dumps(r, ensure_ascii=False) for r in REQUESTS) + "\n"
    try:
        out, err = proc.communicate(input=stdin_data, timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("[FAIL] 服务器超时（run_fea 可能卡住或 PyNite 仍有问题）")
        return 1

    responses = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg and ("result" in msg or "error" in msg):
            responses[msg.get("id")] = msg

    ok = True

    # 1) initialize
    init = responses.get(1)
    if not init or "result" not in init:
        print("[FAIL] initialize 无响应"); ok = False
    else:
        pv = init["result"].get("protocolVersion")
        print(f"[ok] initialize -> protocolVersion={pv}")

    # 2) tools/list
    tl = responses.get(2)
    tools = []
    if tl and "result" in tl:
        tools = [t.get("name") for t in tl["result"].get("tools", [])]
        print(f"[ok] tools/list -> {tools}")
    else:
        print("[FAIL] tools/list 无响应"); ok = False

    expected = {"list_param_space", "check_limits", "run_fea", "run_fea_batch"}
    missing = expected - set(tools)
    if missing:
        print(f"[FAIL] 缺少工具: {missing}"); ok = False

    # 3) list_param_space
    is_err, data = _unwrap(responses.get(3))
    if not is_err and isinstance(data, dict) and data.get("stories"):
        print(f"[ok] list_param_space -> keys={list(data.keys())}, "
              f"beam候选={data.get('beam')}")
    else:
        print(f"[FAIL] list_param_space: {data}"); ok = False

    # 4) check_limits（应判定超限）
    is_err, data = _unwrap(responses.get(4))
    if not is_err and isinstance(data, dict) and data.get("passed") is False:
        print(f"[ok] check_limits -> passed={data.get('passed')}, "
              f"violations={len(data.get('violations', []))} 项")
    else:
        print(f"[FAIL] check_limits 应判定超限却返回 passed!={False}: {data}"); ok = False

    # 5) run_fea（真实 PyNite FEA，应收敛）
    is_err, data = _unwrap(responses.get(5))
    if not is_err and isinstance(data, dict) and data.get("converged") is True:
        print(f"[ok] run_fea -> max_drift={data.get('max_drift')}, "
              f"max_util={data.get('max_util')}, converged={data.get('converged')}")
    else:
        print(f"[FAIL] run_fea 未收敛/出错: is_err={is_err} data={data}"); ok = False

    # 6) run_fea_batch（2 个合法工况，data 为列表）
    is_err, data = _unwrap(responses.get(6))
    lst = data.get("__list__") if isinstance(data, dict) else None
    if not is_err and isinstance(lst, list) and len(lst) == 2:
        conv = [bool(it.get("converged")) for it in lst if isinstance(it, dict)]
        print(f"[ok] run_fea_batch -> 返回 {len(lst)} 工况, converged={conv}")
    else:
        print(f"[FAIL] run_fea_batch: {data}"); ok = False

    # 7) run_fea 非法入参（应返回结构化错误，且进程不崩）
    rf = responses.get(7)
    if rf and "result" in rf and rf["result"].get("isError"):
        txt = rf["result"]["content"][0]["text"]
        body = json.loads(txt)
        print(f"[ok] run_fea(非法入参) -> isError=true, "
              f"error_type={body.get('error_type')} (进程未崩溃，信封化生效)")
    else:
        print(f"[FAIL] run_fea 非法入参未正确报错: {rf}"); ok = False

    err_lines = [l for l in err.splitlines() if l.strip()]
    if err_lines:
        print(f"\n[stderr 末 5 行]\n" + "\n".join(err_lines[-5:]))

    print("\n==== 结论 ====")
    print("ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
