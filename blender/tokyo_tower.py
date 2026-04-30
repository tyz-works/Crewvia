"""
tokyo_tower.py — Blender 4.x bpy スクリプト
東京タワー（リッチ版）: 基本構造 + マテリアル・テクスチャ
  t002: トラス骨組み（ラチス格子 + 4面 X 字ブレーシング）
        大展望台（150 m）・特別展望台（249.6 m）・頂部アンテナ角柱
  t003: マテリアル・テクスチャ設定
        - 色帯分割トラス（オレンジ/白を高さ帯ごとに別カーブオブジェクト）
        - 鉄骨 Principled BSDF（高金属感・低粗さ）
        - 展望台ガラス窓帯（Transmission + Alpha Blend）
        - 展望台構造体（コンクリート調）・アンテナ（高光沢鉄骨）

task: t003 / mission: 20260430-blender
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

UPPER_BANDS = 7  # 大展望台以上の色帯分割数（航空法規定）


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
    """高さ z の塗装色（reference.md §2 の塗り分けルール）"""
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
# 4. マテリアル定義（t003: 鉄骨・ガラス・構造体）
# ============================================================

def _bsdf(mat: bpy.types.Material):
    """Principled BSDF ノードを返すヘルパー"""
    return mat.node_tree.nodes.get('Principled BSDF')


def make_steel(name: str, color: tuple,
               metallic: float = 0.9,
               roughness: float = 0.2) -> bpy.types.Material:
    """鉄骨用マテリアル（高金属感・低粗さで光沢表現）"""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = _bsdf(mat)
    bsdf.inputs['Base Color'].default_value = color
    bsdf.inputs['Metallic'].default_value   = metallic
    bsdf.inputs['Roughness'].default_value  = roughness
    return mat


def make_glass(name: str,
               tint: tuple = (0.75, 0.9, 1.0, 1.0),
               roughness: float = 0.0,
               ior: float = 1.45,
               alpha: float = 0.12) -> bpy.types.Material:
    """展望台ガラス窓用マテリアル（Transmission + Alpha Blend）"""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = _bsdf(mat)
    bsdf.inputs['Base Color'].default_value = tint
    bsdf.inputs['Roughness'].default_value  = roughness
    bsdf.inputs['IOR'].default_value        = ior
    bsdf.inputs['Alpha'].default_value      = alpha
    # Blender 4.x は 'Transmission Weight'、旧版は 'Transmission'
    for key in ('Transmission Weight', 'Transmission'):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = 0.95
            break
    # EEVEE 向けブレンドモード設定（AttributeError は Cycles 専用環境で無視）
    try:
        mat.blend_method  = 'BLEND'
        mat.shadow_method = 'NONE'
    except AttributeError:
        pass
    return mat


def make_concrete(name: str,
                  color: tuple = (0.72, 0.72, 0.68, 1.0),
                  roughness: float = 0.75) -> bpy.types.Material:
    """展望台構造体用マテリアル（コンクリート調・不透明）"""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = _bsdf(mat)
    bsdf.inputs['Base Color'].default_value = color
    bsdf.inputs['Metallic'].default_value   = 0.0
    bsdf.inputs['Roughness'].default_value  = roughness
    return mat


# マテリアルインスタンス
M_ORANGE  = make_steel('Mat_Orange',  COLOR_ORANGE)           # 鉄骨オレンジ帯
M_WHITE   = make_steel('Mat_White',   COLOR_WHITE,            # 鉄骨白帯
                       metallic=0.85, roughness=0.28)
M_GLASS   = make_glass('Mat_Glass_Obs')                       # 展望台ガラス
M_DECK    = make_concrete('Mat_Deck')                         # 展望台構造体
M_ANT     = make_steel('Mat_Antenna', COLOR_ORANGE,           # アンテナ（高光沢）
                       metallic=0.95, roughness=0.12)


# ============================================================
# 5. トラス骨組み（t003: 色帯ごとに別カーブオブジェクトで塗り分け）
# ============================================================

def _new_truss_curve(name: str, mat: bpy.types.Material,
                     radius: float = 0.35) -> bpy.types.Object:
    """bevel_depth 付きカーブオブジェクトを生成してシーンに追加"""
    cd = bpy.data.curves.new(name + '_data', type='CURVE')
    cd.dimensions       = '3D'
    cd.fill_mode        = 'FULL'
    cd.bevel_depth      = radius
    cd.bevel_resolution = 1       # 8角形断面（軽量・視認性良好）
    obj = bpy.data.objects.new(name, cd)
    obj.data.materials.append(mat)
    bpy.context.collection.objects.link(obj)
    return obj


def _add_beam(curve_data, p1: Vector, p2: Vector) -> None:
    """2点間の直線ビームをベジェスプライン（VECTOR ハンドル）で追加"""
    sp = curve_data.splines.new('BEZIER')
    sp.bezier_points.add(1)
    for pt, co in zip(sp.bezier_points, (p1, p2)):
        pt.co                = co
        pt.handle_left_type  = 'VECTOR'
        pt.handle_right_type = 'VECTOR'


# 塗り分け用: オレンジと白で 2 本のカーブオブジェクト
truss_orange = _new_truss_curve('TrussOrange', M_ORANGE)
truss_white  = _new_truss_curve('TrussWhite',  M_WHITE)

heights = panel_heights()
levels = []
for z in heights:
    hw = half_width(z)
    levels.append([
        Vector(( hw,  hw, z)),   # A: +x +y
        Vector((-hw,  hw, z)),   # B: -x +y
        Vector((-hw, -hw, z)),   # C: -x -y
        Vector(( hw, -hw, z)),   # D: +x -y
    ])

SIDES = [(0, 1), (1, 2), (2, 3), (3, 0)]   # 4 面それぞれの隣接コーナーペア


def beam(p1: Vector, p2: Vector) -> None:
    """梁の中点高さで色帯を判断し、対応するカーブオブジェクトに追加"""
    mid_z  = (p1.z + p2.z) * 0.5
    target = truss_orange if color_at(mid_z) == COLOR_ORANGE else truss_white
    _add_beam(target.data, p1, p2)


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


# ============================================================
# 6. 展望台ボックス（t003: 構造体 + ガラス窓帯の 2 層構造）
# ============================================================

def solid_box(name: str, z_bot: float, w: float, d: float,
              h: float, mat: bpy.types.Material) -> bpy.types.Object:
    """z_bot を底面として w × d × h のソリッドボックスを生成"""
    hw, hd = w * 0.5, d * 0.5
    verts = [
        (-hw, -hd, z_bot), ( hw, -hd, z_bot),
        ( hw,  hd, z_bot), (-hw,  hd, z_bot),
        (-hw, -hd, z_bot + h), ( hw, -hd, z_bot + h),
        ( hw,  hd, z_bot + h), (-hw,  hd, z_bot + h),
    ]
    faces = [
        (0, 3, 2, 1),   # bottom (法線: -z)
        (4, 5, 6, 7),   # top    (法線: +z)
        (0, 1, 5, 4),   # front  (法線: -y)
        (2, 3, 7, 6),   # back   (法線: +y)
        (0, 4, 7, 3),   # left   (法線: -x)
        (1, 2, 6, 5),   # right  (法線: +x)
    ]
    mesh = bpy.data.meshes.new(name + '_mesh')
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(mat)
    bpy.context.collection.objects.link(obj)
    return obj


# 大展望台（150 m）: 構造体 145–155 m + ガラス窓帯 148–152 m
MAIN_W = (half_width(MAIN_DECK_H) + 3.5) * 2   # ≈ 42 m
solid_box('MainDeck_Structure', MAIN_DECK_H - 5.0, MAIN_W,       MAIN_W,       10.0, M_DECK)
solid_box('MainDeck_Glass',     MAIN_DECK_H - 2.0, MAIN_W + 0.5, MAIN_W + 0.5,  4.0, M_GLASS)

# 特別展望台（249.6 m）: 構造体 246.6–253.6 m + ガラス窓帯 248.1–251.6 m
TOP_W = (half_width(TOP_DECK_H) + 3.5) * 2     # ≈ 18 m
solid_box('TopDeck_Structure', TOP_DECK_H - 3.0, TOP_W,       TOP_W,       7.0, M_DECK)
solid_box('TopDeck_Glass',     TOP_DECK_H - 1.5, TOP_W + 0.5, TOP_W + 0.5, 3.5, M_GLASS)


# ============================================================
# 7. アンテナ（角柱 307 m → 332.9 m）
# ============================================================

solid_box('Antenna', ANTENNA_BASE_H, 0.6, 0.6, ANTENNA_LEN, M_ANT)


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

n_orange = len(truss_orange.data.splines)
n_white  = len(truss_white.data.splines)
panels   = len(heights) - 1

print("=" * 60)
print("  東京タワー 基本構造 + マテリアル 生成完了")
print(f"  全高           : {TOWER_HEIGHT} m")
print(f"  パネルレベル   : {len(heights)} 段 / パネル数: {panels}")
print(f"  スプライン(橙) : {n_orange} 本")
print(f"  スプライン(白) : {n_white} 本")
print(f"  スプライン合計 : {n_orange + n_white} 本")
print(f"  大展望台       : {MAIN_DECK_H} m  幅 {MAIN_W:.1f} m")
print(f"  特別展望台     : {TOP_DECK_H} m  幅 {TOP_W:.1f} m")
print(f"  アンテナ       : {ANTENNA_BASE_H} m ～ {ANTENNA_BASE_H + ANTENNA_LEN} m")
print(f"  マテリアル     : 鉄骨(橙/白)・ガラス・構造体・アンテナ")
print("=" * 60)
