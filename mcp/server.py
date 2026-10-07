"""零依赖 MCP 服务器（JSON-RPC 2.0 over stdio），把结构工程仿真工具暴露给 MCP 客户端。

不依赖 `mcp` / `fastmcp` 等第三方包，纯标准库实现 MCP 协议要点：
  - initialize：协商 protocolVersion，回报 capabilities.tools
  - notifications/initialized：握手确认（通知无返回值）
  - tools/list：返回工具清单
  - tools/call：按 name + arguments 执行并返回文本内容

供 Cherry Studio（或其他 MCP 客户端）通过 stdio 拉起。
配置示例见同目录 mcp_config_snippet.json。
"""
import json
import os
import sys

# 保证能 import 到项目内的 agent.tools
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.tools import TOOL_SCHEMAS, dispatch  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agnes-sim-agent", "version": "0.1.0"}


def _log(msg: str):
    """日志写 stderr，避免污染 stdout 的 JSON-RPC 通道。"""
    print(f"[agnes-mcp] {msg}", file=sys.stderr, flush=True)


def _force_utf8():
    """强制标准流使用 UTF-8 且不因编码错误崩溃。

    Windows 下 MCP 客户端通常以 UTF-8 与本进程通信，而 Python 在部分宿主环境
    （如 Electron 拉起的无控制台子进程）中可能把 stdout/stderr 设成 GBK，
    一旦写入非 ASCII 工具描述就会抛 UnicodeEncodeError 导致进程退出，
    客户端侧表现为「MCP error -32000: Connection closed」。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 老版本或已被包装的流没有 reconfigure，忽略即可（后面仍有兜底）
            pass


def _dumps(obj) -> str:
    """序列化 JSON-RPC 消息。

    固定 ensure_ascii=True，使输出为纯 ASCII：任何宿主编码下都不会因中文
    而失败，客户端仍能正确解析（\\uXXXX 是合法 JSON 转义）。
    """
    return json.dumps(obj, ensure_ascii=True)


def _render_tool_result(result):
    """把 dispatch 返回的统一信封渲染成 (is_error, text)。

    - ok=false 的信封（校验失败 / 未知工具 / 内部错误）一律记为 MCP 错误，并原样回传信封，
      让客户端（如 Cherry Studio）能看到结构化的 error_type / invalid_fields。
    - 单工况 FEA 非收敛（data.converged=false）记为 MCP 错误，回传精简错误体。
    - run_fea_batch 中任一工况失败（data 列表内 converged=false 或 per-case ok=false）也记为错误。
    """
    if not isinstance(result, dict):
        return (False, _dumps(result))
    if result.get("ok") is False:
        return (True, _dumps(result))
    data = result.get("data")
    if isinstance(data, dict) and data.get("converged") is False:
        return (True, _dumps({"error": "FEA did not converge", "detail": data.get("err")}))
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and (it.get("ok") is False or it.get("converged") is False):
                return (True, _dumps(result))
    return (False, _dumps(result))


def handle(req: dict):
    """处理单个 JSON-RPC 请求，返回响应 dict；通知类返回 None。"""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    # ---- 通知（无 id，不回响应） ----
    if method and method.startswith("notifications/"):
        _log(f"收到通知: {method}")
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_SCHEMAS},
        }

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = dispatch(name, args)
            is_err, text = _render_tool_result(result)
        except Exception as e:  # noqa: BLE001
            is_err = True
            text = _dumps({"error": f"tool {name} failed", "detail": f"{type(e).__name__}: {e}"})
            _log(text)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_err,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    # 未知方法
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main():
    _force_utf8()
    _log(f"start {SERVER_INFO['name']} v{SERVER_INFO['version']} (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _log(f"skip invalid JSON line: {line[:120]}")
            continue
        except Exception as e:  # noqa: BLE001
            _log(f"skip undecodable line: {type(e).__name__}")
            continue
        try:
            resp = handle(req)
        except Exception as e:  # noqa: BLE001
            # 单个请求处理异常不能让整个服务器进程退出
            _log(f"handle error: {type(e).__name__}: {e}")
            req_id = req.get("id") if isinstance(req, dict) else None
            if req_id is not None:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": f"Internal error: {e}"},
                }
            else:
                resp = None
        if resp is not None:
            sys.stdout.write(_dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
