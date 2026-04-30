"""
tokyo_tower.py — Blender 4.x bpy スクリプト
東京タワー（リッチ版）基本構造
  - トラス骨組み: ラチス格子 + 4面 X 字ブレーシング
  - 大展望台（メインデッキ 150 m）
  - 特別展望台（トップデッキ 249.6 m）
  - 頂部アンテナ角柱（307 m → 332.9 m）

task: t002 / mission: 20260430-blender
リファレンス: reference.md (t001 Anika)
"""

import bpy
import bmesh
from mathutils import Vector
import math


# ============================================================
# 1. 定数（t001 reference.md §1–§5 より）
# ============================================================

TOWER_HEIGHT    = 332.9   # 全高 (m)
MAIN_DECK_H     = 150.0   # 大展望台 高さ (m)
TOP_DECK_H      = 249.6   # 特別展望台 高さ (m)
ANTENNA_BASE_H  = 307.0   # アンテナ支柱根元 (m)
ANTENNA_LEN     = 25.9    # アンテナ長さ (m, 307 → 332.9)

# 断面外寸半幅の参照点 [(高さ m, 半幅 m), ...]
WIDTH_KP = [
    (0.0,          40.0),   # フットプリント 80m → 半幅 40m
    (MAIN_DECK_H,  17.5),   # 大展望台付近 35m → 半幅 17.5m
    (TOP_DECK_H,    5.5),   # 特別展望台付近 11m → 半幅 5.5m
    (TOWER_HEIGHT,  2.5),   # 頂点 5m → 半幅 2.5m
]

COLOR_ORANGE = (1.0, 0.40, 0.0, 1.0)  # インターナショナルオレンジ (RGBA)
COLOR_WHITE  = (1.0, 1.0,  1.0, 1.0)  # 白 (RGBA)

UPPER_BANDS  = 7  # 大展望台以上の色帯分割数（航空法規定）


# ============================================================
# 2. ヘルパー関数
# ============================================================

def half_width(z: float) -> float:
    """高さ z における断面外寸半幅（線形補間）"""
    for i in range(len(WIDTH_KP) - 1):
        z0, w0 = WIDTH_KP[i]
        z1, w1 = WIDTH_KP[i + 1]
        if z <= z1:
            t = (z - z0) / (z1 - z0)
            return w0 + t * (w1 - w0)
    return WIDTH_KP[-1][1]


def color_at(z: float) -> tuple:
    """高さ z の塗装色を返す（reference.md §2 の塗り分けルール）"""
    if z <= MAIN_DECK_H:
        return COLOR_ORANGE
    band_h = (TOWER_HEIGHT - MAIN_DECK_H) / UPPER_BANDS
    band = min(int((TOWER_HEIGHT - z) / band_h), UPPER_BANDS - 1)
    return COLOR_ORANGE if band % 2 == 0 else COLOR_WHITE


def panel_heights() -> list:
    """トラスパネルの高さレベルリストを生成する"""
    hs: set = set()
    h = 0.0
    while h <= MAIN_DECK_H + 0.001:    # 下部 (0–150 m): 6 m ピッチ
        hs.add(round(h, 3)); h += 6.0
    h = MAIN_DECK_H
    while h <= TOP_DECK_H + 0.001:     # 中部 (150–250 m): 4 m ピッチ
        hs.add(round(h, 3)); h += 4.0
    h = TOP_DECK_H
    while h <= TOWER_HEIGHT + 0.001:   # 上部 (250–333 m): 3 m ピッチ
        hs.add(round(h, 3)); h += 3.0
    for anchor in (0.0, MAIN_DECK_H, TOP_DECK_H, TOWER_HEIGHT):
        hs.add(anchor)
    return sorted(hs)


# ============================================================
# 3. シーン初期化
# ============================================================

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)


# ============================================================
# 4. マテリアル
# ============================================================

def new_mat(name: str, color: tuple,
            metallic: float = 0.85, roughness: float = 0.25) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    bsdf.inputs['Base Color'].default_value = color
    bsdf.inputs['Metallic'].default_value   = metallic
    bsdf.inputs['Roughness'].default_value  = roughness
    return mat


mat_orange = new_mat('Mat_Orange', COLOR_ORANGE)
mat_white  = new_mat('Mat_White',  COLOR_WHITE)


# ============================================================
# 5. トラス骨組み（Curve + bevel_depth で鋼管表現）
# ============================================================

truss_curve = bpy.data.curves.new('TrussData', type='CURVE')
truss_curve.dimensions       = '3D'
truss_curve.fill_mode        = 'FULL'
truss_curve.bevel_depth      = 0.35    # 鋼管半径 ~0.35 m（縮尺に準じた部材径）
truss_curve.bevel_resolution = 1       # 8角形断面（軽量・視認性良好）


def beam(p1: Vector, p2: Vector) -> None:
    """2 点間の直線ビームをベジェスプライン（VECTOR ハンドル）で追加"""
    sp = truss_curve.splines.new('BEZIER')
    sp.bezier_points.add(1)            # 既存の1点 + 追加1点 = 合計2点
    for pt, co in zip(sp.bezier_points, (p1, p2)):
        pt.co                 = co
        pt.handle_left_type   = 'VECTOR'
        pt.handle_right_type  = 'VECTOR'


heights = panel_heights()

# 各レベルの 4 コーナー頂点を生成
# コーナー順: A(+x,+y) / B(-x,+y) / C(-x,-y) / D(+x,-y)
levels = []
for z in heights:
    hw = half_width(z)
    levels.append([
        Vector(( hw,  hw, z)),
        Vector((-hw,  hw, z)),
        Vector((-hw, -hw, z)),
        Vector(( hw, -hw, z)),
    ])

SIDES = [(0, 1), (1, 2), (2, 3), (3, 0)]   # 4 面それぞれの隣接コーナーペア

for i in range(len(heights) - 1):
    vk, vk1 = levels[i], levels[i + 1]

    for j in range(4):          # 縦材（各脚柱）
        beam(vk[j], vk1[j])

    for a, b in SIDES:          # 横材（パネル下端の水平リング）
        beam(vk[a], vk[b])

    for a, b in SIDES:          # 斜材（各面に X 字ラチスブレーシング）
        beam(vk[a], vk1[b])
        beam(vk[b], vk1[a])

for a, b in SIDES:              # 最上端リング
    beam(levels[-1][a], levels[-1][b])

truss_obj = bpy.data.objects.new('TowerTruss', truss_curve)
bpy.context.collection.objects.link(truss_obj)
truss_obj.data.materials.append(mat_orange)


# ============================================================
# 6. 展望台ボックス（t003 でマテリアル詳細化予定）
# ============================================================

def box_obj(name: str, z_bot: float, width: float, depth: float,
            height: float, mat: bpy.types.Material) -> bpy.types.Object:
    """z_bot を底面として width × depth × height のソリッドボックスを生成"""
    hw, hd = width / 2, depth / 2
    verts = [
        (-hw, -hd, z_bot), ( hw, -hd, z_bot),
        ( hw,  hd, z_bot), (-hw,  hd, z_bot),
        (-hw, -hd, z_bot + height), ( hw, -hd, z_bot + height),
        ( hw,  hd, z_bot + height), (-hw,  hd, z_bot + height),
    ]
    faces = [
        (0, 3, 2, 1),   # bottom
        (4, 5, 6, 7),   # top
        (0, 1, 5, 4),   # front  (-y)
        (2, 3, 7, 6),   # back   (+y)
        (0, 4, 7, 3),   # left   (-x)
        (1, 2, 6, 5),   # right  (+x)
    ]
    mesh = bpy.data.meshes.new(name + '_mesh')
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(mat)
    bpy.context.collection.objects.link(obj)
    return obj


# 大展望台（150 m）: 145 m ～ 155 m（高さ 10 m、幅 ~42 m）
main_w = (half_width(MAIN_DECK_H) + 3.5) * 2   # 17.5 + 3.5 = 21 → 幅 42 m
box_obj('MainDeck', MAIN_DECK_H - 5.0, main_w, main_w, 10.0, mat_white)

# 特別展望台（249.6 m）: 246.6 m ～ 253.6 m（高さ 7 m、幅 ~18 m）
top_w = (half_width(TOP_DECK_H) + 3.5) * 2     # 5.5 + 3.5 = 9 → 幅 18 m
box_obj('TopDeck', TOP_DECK_H - 3.0, top_w, top_w, 7.0, mat_white)


# ============================================================
# 7. アンテナ（角柱 307 m → 332.9 m）
# ============================================================

box_obj('Antenna', ANTENNA_BASE_H, 0.6, 0.6, ANTENNA_LEN, mat_orange)


# ============================================================
# 8. カメラ・太陽光
# ============================================================

bpy.ops.object.camera_add(
    location=(300.0, -280.0, 170.0),
    rotation=(math.radians(74), 0.0, math.radians(47)),
)
cam = bpy.context.active_object
cam.name = 'Camera_Tower'
bpy.context.scene.camera = cam
cam.data.lens = 35.0   # 35mm 標準画角

bpy.ops.object.light_add(
    type='SUN',
    location=(120.0, -80.0, 350.0),
    rotation=(math.radians(38), 0.0, math.radians(32)),
)
sun = bpy.context.active_object
sun.name = 'Sun_Main'
sun.data.energy = 4.0


# ============================================================
# 9. 確認サマリー
# ============================================================

n_splines = len(truss_curve.splines)
panels    = len(heights) - 1

print("=" * 56)
print("  東京タワー 基本構造 生成完了")
print(f"  全高         : {TOWER_HEIGHT} m")
print(f"  パネルレベル  : {len(heights)} 段 / パネル数: {panels}")
print(f"  スプライン本数: {n_splines}")
print(f"  大展望台      : {MAIN_DECK_H} m  幅 {main_w:.1f} m")
print(f"  特別展望台    : {TOP_DECK_H} m  幅 {top_w:.1f} m")
print(f"  アンテナ      : {ANTENNA_BASE_H} m ～ {ANTENNA_BASE_H + ANTENNA_LEN} m")
print("=" * 56)
