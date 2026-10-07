"""T2：真实 PyNite 3D 混凝土框架 FEA 封装。

对外提供 run_fea(params)，替换 harness_example.py 里的 mock。
建模约定（与 PyNite 一致）：
  - 全局坐标 Y 为竖向（重力 = 全局 FY 负向），X / Z 为水平方向。
  - 单位统一为 SI：长度 m、力 N、应力 Pa、密度 kg/m³。
  - 竖向承重 3 层 × 3 跨（X 向 3 跨 = 4 条柱列；Z 向 2 跨 = 3 条柱列），
    柱沿 Y 布置，梁沿 X / Z 双向布置，底层节点固接。

参数（来自 harness 的 tool 定义）：
  stories : int        层数（3/4/5）
  col_mm  : int        柱截面边长（mm，正方形）
  beam    : [int,int]  梁 [宽,高]（mm）
  conc    : str        混凝土等级 "C30"/"C35"
  live_load: number    楼面活荷载（kN/m²）
  lateral : str        水平荷载等级 "S"/"M"/"L"

返回（与 mock 同构，便于 harness 直接替换）：
  max_drift   弹性层间位移角（无量纲）
  top_disp    顶点最大水平位移（m）
  max_util    最大构件承载力利用率（无量纲，>1 即超限）
  base_shear  基底剪力（kN）
  converged   是否收敛
  err         错误信息（None 表示正常）
"""
# PyNiteFEA 在部分安装 / 平台下包目录名为 "Pynite"（小写 n），
# 而历史代码习惯写 "PyNite"。Windows 上的 import 大小写匹配不稳定，
# 故做兼容导入，确保两种命名都能跑通。
try:
    import PyNite
except ModuleNotFoundError:
    import Pynite as PyNite

FEModel3D = PyNite.FEModel3D

# ---- 几何常量（m） ----
STORY_H = 3.0          # 层高
BAY_X = 6.0            # X 向柱距（3 跨）
BAY_Z = 6.0            # Z 向柱距（2 跨）
NX = 4                 # X 向柱列数（= 跨数 + 1）
NZ = 3                 # Z 向柱列数（= 跨数 + 1）
SLAB_DEAD = 4.5        # 楼面恒载（kN/m²：120mm 板 + 面层）
CONC_DENSITY = 2500.0  # 混凝土质量密度（kg/m³）

# 水平荷载（每层合力，N）：S/M/L
LAT_PER_FLOOR = {"S": 30e3, "M": 90e3, "L": 180e3}
# 混凝土弹性模量（GB 50010）：Ec = 1e5 / (2.2 + 34.7/fck)  MPa
_FCK = {"C30": 30, "C35": 35}


def concrete_E_pa(grade: str) -> float:
    fck = _FCK.get(grade, 30)
    return 1e5 / (2.2 + 34.7 / fck) * 1e6  # Pa


def _section_props(b_mm: float, h_mm: float):
    """返回 (A, Iy, Iz, J)，单位 m² / m⁴。矩形 b×h，绕强轴(z)弯曲=Iz=b*h³/12。"""
    b, h = b_mm / 1000.0, h_mm / 1000.0
    A = b * h
    Iz = b * h ** 3 / 12.0   # 强轴
    Iy = h * b ** 3 / 12.0   # 弱轴
    J = Iy + Iz              # 近似扭转常数
    return A, Iy, Iz, J


def _req_rho_beam(M: float, b: float, d: float, fck: float, fy: float) -> float:
    """单筋矩形截面在弯矩 M 下的需求配筋率 ρ（无量纲）。

    M = As·fy·(d − a/2)，a = As·fy/(0.85·fck·b) → 对 X=As·fy 的二次方程：
        X² − 2·(0.85·fck·b)·d·X + 2·(0.85·fck·b)·M = 0
    取小根（适筋）。M≤0 返回 0；M 超过平衡配筋时 ρ 被上限夹住（不发散）。
    """
    if M <= 0:
        return 0.0
    k = 0.85 * fck * b
    kd = k * d
    disc = kd * kd - 2.0 * k * M
    if disc < 0:
        disc = 0.0
    X = kd - disc ** 0.5
    if X < 0:
        X = 0.0
    return (X / fy) / (b * d)


def _req_rho_col(N: float, A: float, fck: float, fy: float, phi: float = 0.65) -> float:
    """柱轴压需求配筋率 ρ（无量纲）：N/φ − 0.85·fck·A 由钢筋承担，除以 fy 与 A。"""
    if N <= 0:
        return 0.0
    Ast_req = max(0.0, (N / phi - 0.85 * fck * A) / fy)
    return Ast_req / A


# 允许配筋率上限（proxy 判据）：梁 2.5%、柱 4%（规范常见配筋率上限量级）。
# util = 需求配筋率 / 允许配筋率；>1 表示连最大配筋都不够，必须加大截面。
RHO_ALLOW_BEAM = 0.025
RHO_ALLOW_COL = 0.040


def build_and_analyze(params: dict) -> dict:
    stories = int(params["stories"])
    col_mm = int(params["col_mm"])
    bw_mm, h_mm = int(params["beam"][0]), int(params["beam"][1])
    conc = str(params.get("conc", "C30"))
    live_load = float(params["live_load"])
    lateral = str(params.get("lateral", "S"))

    # ---- 材料 / 截面 ----
    E = concrete_E_pa(conc)
    nu = 0.2
    G = E / (2.0 * (1.0 + nu))
    rho = CONC_DENSITY

    A_col, Iy_col, Iz_col, J_col = _section_props(col_mm, col_mm)
    A_beam, Iy_beam, Iz_beam, J_beam = _section_props(bw_mm, h_mm)

    model = FEModel3D()
    model.add_material("conc", E, G, nu, rho, fy=400e6)  # fy 仅存储，弹性分析不用
    model.add_section("col", A_col, Iy_col, Iz_col, J_col)
    model.add_section("beam", A_beam, Iy_beam, Iz_beam, J_beam)

    # ---- 节点 ----
    Ys = [j * STORY_H for j in range(stories + 1)]
    for j, y in enumerate(Ys):
        for i in range(NX):
            x = i * BAY_X
            for k in range(NZ):
                z = k * BAY_Z
                model.add_node(f"N{i}_{k}_{j}", x, y, z)

    # ---- 支座：底层全固接 ----
    for i in range(NX):
        for k in range(NZ):
            model.def_support(f"N{i}_{k}_0", True, True, True, True, True, True)

    # ---- 构件：柱（竖向）+ 梁（X/Z 双向） ----
    for i in range(NX):
        for k in range(NZ):
            for j in range(stories):
                model.add_member(
                    f"C{i}_{k}_{j}", f"N{i}_{k}_{j}", f"N{i}_{k}_{j+1}", "conc", "col"
                )
    for k in range(NZ):                      # X 向梁（位于某 Z 列）
        for j in range(1, stories + 1):
            for i in range(NX - 1):
                model.add_member(
                    f"BX{i}_{k}_{j}", f"N{i}_{k}_{j}", f"N{i+1}_{k}_{j}", "conc", "beam"
                )
    for i in range(NX):                      # Z 向梁（位于某 X 列）
        for j in range(1, stories + 1):
            for k in range(NZ - 1):
                model.add_member(
                    f"BZ{i}_{k}_{j}", f"N{i}_{k}_{j}", f"N{i}_{k+1}_{j}", "conc", "beam"
                )

    # ---- 重力荷载（楼面恒载 + 活载 + 梁自重），全局 FY 负向 ----
    w_self = bw_mm / 1000.0 * h_mm / 1000.0 * 25.0 * 1000.0  # N/m
    # X 向梁的 Z 向 tributary
    for k in range(NZ):
        trib = BAY_Z if 0 < k < NZ - 1 else BAY_Z / 2.0
        w = -((SLAB_DEAD + live_load) * 1000.0 * trib + w_self)
        for j in range(1, stories + 1):
            for i in range(NX - 1):
                model.add_member_dist_load(f"BX{i}_{k}_{j}", "FY", w, w, case="Case 1")
    # Z 向梁的 X 向 tributary
    for i in range(NX):
        trib = BAY_X if 0 < i < NX - 1 else BAY_X / 2.0
        w = -((SLAB_DEAD + live_load) * 1000.0 * trib + w_self)
        for j in range(1, stories + 1):
            for k in range(NZ - 1):
                model.add_member_dist_load(f"BZ{i}_{k}_{j}", "FY", w, w, case="Case 1")

    # ---- 水平荷载（等效楼层力，X 向），均分到该层各节点 ----
    pf = LAT_PER_FLOOR.get(lateral, 30e3)     # 每层合力（N）
    n_per_level = NX * NZ
    p_node = pf / n_per_level
    for j in range(1, stories + 1):
        for i in range(NX):
            for k in range(NZ):
                model.add_node_load(f"N{i}_{k}_{j}", "FX", p_node, case="Case 1")

    # ---- 荷载组合 + 求解 ----
    model.add_load_combo("Combo 1", {"Case 1": 1.0})
    model.analyze_linear(log=False, check_stability=True)

    combo = "Combo 1"

    # ---- 提取：层间位移角 / 顶点位移 ----
    max_x_by_level = {}
    for j in range(stories + 1):
        mx = 0.0
        for i in range(NX):
            for k in range(NZ):
                dx = abs(model.nodes[f"N{i}_{k}_{j}"].DX[combo])
                mx = max(mx, dx)
        max_x_by_level[j] = mx
    top_disp = max_x_by_level[stories]
    max_drift = 0.0
    for j in range(1, stories + 1):
        drift = (max_x_by_level[j] - max_x_by_level[j - 1]) / STORY_H
        max_drift = max(max_drift, abs(drift))

    # ---- 提取：构件承载力利用率（proxy：需求配筋率 / 允许配筋率） ----
    # 含义：该工况下构件需要的配筋率占「允许最大配筋率」的比例；>1 即连最大配筋都不够
    # （必须加大截面），从而安全 / 危险工况可被明显区分，而非一律判超。
    fck = _FCK.get(conc, 30) * 1e6            # Pa
    fy = 400e6
    cover = 0.04                              # 保护层（m）
    d_beam_h = h_mm / 1000.0 - cover          # 梁强轴有效高度（h 竖直）
    d_beam_b = bw_mm / 1000.0 - cover         # 梁弱轴有效高度（bw 竖直）
    b_beam = bw_mm / 1000.0

    max_util = 0.0
    for name, m in model.members.items():
        if name.startswith("C"):              # 柱：轴压
            Ncompr = max(0.0, -m.min_axial(combo))
            util = _req_rho_col(Ncompr, A_col, fck, fy) / RHO_ALLOW_COL
        else:                                 # 梁：强 / 弱轴抗弯，取大者（保守）
            Mz = abs(m.max_moment("Mz", combo))
            My = abs(m.max_moment("My", combo))
            rho_z = _req_rho_beam(Mz, b_beam, d_beam_h, fck, fy)          # 强轴
            rho_y = _req_rho_beam(My, h_mm / 1000.0, d_beam_b, fck, fy)   # 弱轴
            util = max(rho_z, rho_y) / RHO_ALLOW_BEAM
        max_util = max(max_util, util)

    return {
        "max_drift": max_drift,
        "top_disp": top_disp,
        "max_util": max_util,
        "base_shear": stories * pf / 1000.0,   # kN
        "converged": True,
        "err": None,
    }


def run_fea(params: dict) -> dict:
    """harness 调用的真实入口；异常时返回非收敛结果而非抛错。"""
    try:
        return build_and_analyze(params)
    except Exception as e:  # noqa: BLE001
        return {
            "max_drift": None,
            "top_disp": None,
            "max_util": None,
            "base_shear": None,
            "converged": False,
            "err": f"{type(e).__name__}: {e}",
        }


if __name__ == "__main__":
    import json
    samples = [
        {"stories": 3, "col_mm": 500, "beam": [500, 250], "conc": "C30", "live_load": 3, "lateral": "S"},
        {"stories": 3, "col_mm": 500, "beam": [500, 250], "conc": "C30", "live_load": 5, "lateral": "L"},
        {"stories": 5, "col_mm": 600, "beam": [600, 300], "conc": "C35", "live_load": 7, "lateral": "M"},
    ]
    for p in samples:
        print(json.dumps(p, ensure_ascii=False))
        print("  ->", json.dumps(run_fea(p), ensure_ascii=False))
