"""agent/tools_selftest.py —— T3 工具层（缺口 1/2：入参校验 + 统一信封）自测。

覆盖验收场景：
  1) 正常调用 list_param_space / check_limits（信封 ok）
  2) 非法 col_mm=450 -> invalid_arguments，且 invalid_fields 指出 col_mm
  3) 缺字段 -> invalid_arguments，且 invalid_fields 指出缺失字段
  4) run_fea_batch 混合 2 合法 + 1 非法 -> summary 正确，非法项本身是信封
  5) 未知工具 -> unknown_tool
  6) 落盘 report/fea_runs.jsonl 全部行可解析为 JSON

运行：venv\\Scripts\\python.exe agent/tools_selftest.py
"""
import os
import json
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.tools import dispatch, _RUNS_JSONL  # noqa: E402


def _count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _check(cond, label):
    print(("PASS" if cond else "FAIL") + " - " + label)
    return cond


def main():
    ok_all = True

    # 1) 正常 list_param_space
    r = dispatch("list_param_space", {})
    ok_all &= _check(r.get("ok") is True and "data" in r, "list_param_space 信封成功")

    # 2) 正常 check_limits（通过）
    r = dispatch("check_limits", {"max_drift": 1 / 600, "max_util": 0.9})
    ok_all &= _check(r.get("ok") is True and r["data"]["passed"] is True, "check_limits 安全工况 passed=true")

    # 3) check_limits 超限
    r = dispatch("check_limits", {"max_drift": 1 / 400, "max_util": 1.2})
    ok_all &= _check(r.get("ok") is True and r["data"]["passed"] is False, "check_limits 超限工况 passed=false")

    # 4) 非法 col_mm=450 -> invalid_arguments
    r = dispatch("run_fea", {"stories": 3, "col_mm": 450, "beam": [500, 250], "conc": "C30", "live_load": 3, "lateral": "S"})
    ok_all &= _check(r.get("ok") is False and r.get("error_type") == "invalid_arguments", "run_fea 非法 col_mm -> invalid_arguments")
    bad_fields = {f["field"] for f in r.get("invalid_fields", [])}
    ok_all &= _check("col_mm" in bad_fields, "  invalid_fields 含 col_mm")

    # 5) 缺字段 -> 指出 missing
    r = dispatch("run_fea", {"stories": 3, "col_mm": 500})
    ok_all &= _check(r.get("ok") is False and r.get("error_type") == "invalid_arguments", "run_fea 缺字段 -> invalid_arguments")
    miss = {f["field"] for f in r.get("invalid_fields", []) if f.get("expected") == "必填"}
    ok_all &= _check(len(miss) > 0, "  invalid_fields 指出缺失字段: " + str(miss))

    # 6) run_fea_batch 混合：2 合法 + 1 非法
    cases = [
        {"stories": 3, "col_mm": 500, "beam": [500, 250], "conc": "C30", "live_load": 3, "lateral": "S"},
        {"stories": 5, "col_mm": 400, "beam": [600, 300], "conc": "C35", "live_load": 7, "lateral": "L"},
        {"stories": 3, "col_mm": 450, "beam": [500, 250], "conc": "C30", "live_load": 3, "lateral": "S"},  # 非法
    ]
    before = _count_lines(_RUNS_JSONL)
    r = dispatch("run_fea_batch", {"params_list": cases})
    after = _count_lines(_RUNS_JSONL)
    ok_all &= _check(r.get("ok") is True, "run_fea_batch 信封成功")
    summary = r.get("summary", {})
    ok_all &= _check(
        summary.get("total") == 3 and summary.get("invalid") == 1 and summary.get("ran") == 2,
        "  summary 正确: " + json.dumps(summary, ensure_ascii=False))
    ok_all &= _check(after == before + 1, "  batch 仅落盘 1 行 (before=%d after=%d)" % (before, after))
    data = r.get("data", [])
    ok_all &= _check(isinstance(data[0], dict) and "max_drift" in data[0], "  合法工况 data 含 max_drift")
    ok_all &= _check(data[2].get("error_type") == "invalid_arguments", "  非法工况 entry 为 invalid_arguments 信封")

    # 7) 未知工具 -> unknown_tool
    r = dispatch("no_such_tool", {})
    ok_all &= _check(r.get("ok") is False and r.get("error_type") == "unknown_tool", "未知工具 -> unknown_tool")

    # 8) 落盘 JSON 格式校验
    parse_ok = True
    with open(_RUNS_JSONL, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except Exception:
                parse_ok = False
                print("  行 %d 解析失败" % i)
    ok_all &= _check(parse_ok, "fea_runs.jsonl 全部行可解析为 JSON")

    print("\n==> 自测结果:", "ALL PASS" if ok_all else "有 FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
