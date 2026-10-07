"""agent/export_runs.py —— 把 report/fea_runs.jsonl 转成 02-数据表格 的 CSV / 归一化 JSONL。

兼容两种持久化格式：
  - 旧格式：record["result"] 为工具原始返回（run_fea dict / batch list）
  - 新格式：record["result"] 为统一信封 {ok, tool, data, [warning]}
run_fea_batch 的多工况会被展开成多行（batch_idx 标注）。

运行：venv\\Scripts\\python.exe agent/export_runs.py
"""
import os
import sys
import json
import csv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_ROOT, "report", "fea_runs.jsonl")
OUT_DIR = "D:/WorkBuddy/AI工作区/02-数据表格"
LIMIT_DRIFT = 1.0 / 550.0
LIMIT_UTIL = 1.0

FIELDS = [
    "tool", "batch_idx", "stories", "col_mm", "beam_w", "beam_h", "conc",
    "live_load", "lateral", "max_drift", "top_disp_m", "max_util",
    "base_shear_kN", "converged", "limit_drift", "limit_util", "passed",
    "err", "envelope_ok", "warning",
]


def _is_envelope(r):
    return isinstance(r, dict) and ("ok" in r)


def _norm_result(result):
    """统一成 (ok, data, warning)。"""
    if _is_envelope(result):
        return result.get("ok"), result.get("data"), result.get("warning")
    return True, result, None


def _row_from_single(params, data, ok, warning, batch_idx=None):
    if not isinstance(data, dict):
        return None
    beam = params.get("beam") or []
    bw = beam[0] if len(beam) > 0 else None
    bh = beam[1] if len(beam) > 1 else None
    md = data.get("max_drift")
    mu = data.get("max_util")
    passed = ""
    if md is not None and mu is not None and ok is not False:
        passed = (md <= LIMIT_DRIFT and mu <= LIMIT_UTIL)
    return {
        "tool": "run_fea_batch" if batch_idx is not None else "run_fea",
        "batch_idx": batch_idx if batch_idx is not None else "",
        "stories": params.get("stories"),
        "col_mm": params.get("col_mm"),
        "beam_w": bw,
        "beam_h": bh,
        "conc": params.get("conc"),
        "live_load": params.get("live_load"),
        "lateral": params.get("lateral"),
        "max_drift": md,
        "top_disp_m": data.get("top_disp"),
        "max_util": mu,
        "base_shear_kN": data.get("base_shear"),
        "converged": data.get("converged"),
        "limit_drift": LIMIT_DRIFT,
        "limit_util": LIMIT_UTIL,
        "passed": passed,
        "err": data.get("err"),
        "envelope_ok": ok,
        "warning": warning,
    }


def _extract(tool, args, result):
    rows = []
    ok, data, warning = _norm_result(result)
    if tool == "run_fea":
        rows.append(_row_from_single(args or {}, data, ok, warning))
    elif tool == "run_fea_batch":
        params_list = (args or {}).get("params_list", []) if isinstance(args, dict) else []
        if isinstance(data, list):
            for idx, item in enumerate(data):
                pa = params_list[idx] if idx < len(params_list) else {}
                if _is_envelope(item):
                    it_ok, it_data, it_warn = item.get("ok"), item.get("data"), item.get("warning")
                else:
                    it_ok, it_data, it_warn = True, item, None
                rows.append(_row_from_single(pa, it_data, it_ok, it_warn, batch_idx=idx))
        elif data is not None:
            rows.append(_row_from_single({}, data, ok, warning))
    # list_param_space / check_limits 不计入 FEA 结果表
    return rows


def main():
    # 可选参数: YYYY-MM-DD，决定输出文件名；默认 2026-10-04（向后兼容，不覆盖既有文件）。
    date_tag = sys.argv[1] if len(sys.argv) > 1 else "2026-10-04"
    if not os.path.exists(SRC):
        print("未找到", SRC)
        return 1
    rows = []
    with open(SRC, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rows.extend(_extract(rec.get("tool"), rec.get("args"), rec.get("result")))
    rows = [r for r in rows if r is not None]

    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUT_DIR, f"agnes-harness-fea结果-{date_tag}.csv")
    jsonl_path = os.path.join(OUT_DIR, f"agnes-harness-fea结果-{date_tag}.jsonl")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    n_pass = sum(1 for r in rows if r["passed"] is True)
    n_fail = sum(1 for r in rows if r["passed"] is False)
    print(f"rows={len(rows)}  PASS={n_pass}  FAIL={n_fail}")
    print(f"csv  -> {csv_path}")
    print(f"jsonl-> {jsonl_path}")
    return 0


if __name__ == "__main__":
    main()
