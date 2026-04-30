"""
tokyo_tower.py — Blender 4.x bpy スクリプト
東京タワー（リッチ版）: 基本構造 + マテリアル + ライティング・夜景
  t002: トラス骨組み（ラチス格子 + 4面 X 字ブレーシング）
        大展望台（150 m）・特別展望台（249.6 m）・頂部アンテナ角柱
  t003: マテリアル・テクスチャ設定
        色帯分割トラス（橙/白別カーブ）・鉄骨 Principled BSDF・
        展望台ガラス窓帯・構造体コンクリート調
  t004: ライティング・夜景シーン
        グラウンドスポットライト・航空障害灯（赤）・室内発光窓・
        夜空バックグラウンド・EEVEE/Cycles レンダリング設定

task: t004 / mission: 20260430-blender
リファレンス: reference.md (t001 Anika)

使い方:
  Blender の「スクリプトエディタ」または「テキストエディタ」にこのファイルを開き
  「スクリプトを実行」ボタン（▶）を押すと東京タワーが生成されます。
  ENABLE_NIGHT を True/False で昼景 / 夜景を切り替えられます。
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
UPPER_BANDS  = 7                        # 大展望台以上の色帯分割数（航空法規定）

# ── ライティング設定 ──────────────────────────────────────────
ENABLE_NIGHT = True   # True: 夜景モード / False: 昼景モード

SPOT_DISTANCE = 130.0           # グラウンドスポット距離 (m)
SPOT_HEIGHT   =  10.0           # グラウンドスポット高さ (m)
AIM_HEIGHT    = 180.0           # 主照射高さ (m)
SPOT_ENERGY   = 3000.0          # スポットライト輝度 (W)
SPOT_COLOR    = (1.0, 0.92, 0.75)  # 温白色スポット
WINDOW_EMIT   = 3.5             # 窓ガラス発光強度
AVL_ENERGY    = 60.0            # 航空障害灯輝度 (W)
NIGHT_SKY_RGB = (0.005, 0.005, 0.02)  # 夜空の背景色

# ── レンダリング設定 ──────────────────────────────────────────
RENDER_ENGINE  = 'BLENDER_EEVEE'   # 'BLENDER_EEVEE' または 'CYCLES'
RENDER_W, RENDER_H = 1920, 1080
CYCLES_SAMPLES  = 256


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
    for key in ('Transmission Weight', 'Transmission'):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = 0.95
            break
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


M_ORANGE = make_steel('Mat_Orange', COLOR_ORANGE)
M_WHITE  = make_steel('Mat_White',  COLOR_WHITE, metallic=0.85, roughness=0.28)
M_GLASS  = make_glass('Mat_Glass_Obs')
M_DECK   = make_concrete('Mat_Deck')
M_ANT    = make_steel('Mat_Antenna', COLOR_ORANGE, metallic=0.95, roughness=0.12)


# ============================================================
# 5. トラス骨組み（t003: 色帯ごとに別カーブオブジェクトで塗り分け）
# ============================================================

def _new_truss_curve(name: str, mat: bpy.types.Material,
                     radius: float = 0.35) -> bpy.types.Object:
    cd = bpy.data.curves.new(name + '_data', type='CURVE')
    cd.dimensions       = '3D'
    cd.fill_mode        = 'FULL'
    cd.bevel_depth      = radius
    cd.bevel_resolution = 1
    obj = bpy.data.objects.new(name, cd)
    obj.data.materials.append(mat)
    bpy.context.collection.objects.link(obj)
    return obj


def _add_beam(curve_data, p1: Vector, p2: Vector) -> None:
    sp = curve_data.splines.new('BEZIER')
    sp.bezier_points.add(1)
    for pt, co in zip(sp.bezier_points, (p1, p2)):
        pt.co                = co
        pt.handle_left_type  = 'VECTOR'
        pt.handle_right_type = 'VECTOR'


truss_orange = _new_truss_curve('TrussOrange', M_ORANGE)
truss_white  = _new_truss_curve('TrussWhite',  M_WHITE)

heights = panel_heights()
levels = []
for z in heights:
    hw = half_width(z)
    levels.append([
        Vector(( hw,  hw, z)),
        Vector((-hw,  hw, z)),
        Vector((-hw, -hw, z)),
        Vector(( hw, -hw, z)),
    ])

SIDES = [(0, 1), (1, 2), (2, 3), (3, 0)]


def beam(p1: Vector, p2: Vector) -> None:
    """梁の中点高さで色帯を判断し対応カーブに追加"""
    mid_z  = (p1.z + p2.z) * 0.5
    target = truss_orange if color_at(mid_z) == COLOR_ORANGE else truss_white
    _add_beam(target.data, p1, p2)


for i in range(len(heights) - 1):
    vk, vk1 = levels[i], levels[i + 1]
    for j in range(4):
        beam(vk[j], vk1[j])
    for a, b in SIDES:
        beam(vk[a], vk[b])
    for a, b in SIDES:
        beam(vk[a], vk1[b])
        beam(vk[b], vk1[a])

for a, b in SIDES:
    beam(levels[-1][a], levels[-1][b])


# ============================================================
# 6. 展望台ボックス（t003: 構造体 + ガラス窓帯の 2 層構造）
# ============================================================

def solid_box(name: str, z_bot: float, w: float, d: float,
              h: float, mat: bpy.types.Material) -> bpy.types.Object:
    hw, hd = w * 0.5, d * 0.5
    verts = [
        (-hw, -hd, z_bot), ( hw, -hd, z_bot),
        ( hw,  hd, z_bot), (-hw,  hd, z_bot),
        (-hw, -hd, z_bot + h), ( hw, -hd, z_bot + h),
        ( hw,  hd, z_bot + h), (-hw,  hd, z_bot + h),
    ]
    faces = [
        (0, 3, 2, 1), (4, 5, 6, 7),
        (0, 1, 5, 4), (2, 3, 7, 6),
        (0, 4, 7, 3), (1, 2, 6, 5),
    ]
    mesh = bpy.data.meshes.new(name + '_mesh')
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(mat)
    bpy.context.collection.objects.link(obj)
    return obj


MAIN_W = (half_width(MAIN_DECK_H) + 3.5) * 2
solid_box('MainDeck_Structure', MAIN_DECK_H - 5.0, MAIN_W,       MAIN_W,       10.0, M_DECK)
solid_box('MainDeck_Glass',     MAIN_DECK_H - 2.0, MAIN_W + 0.5, MAIN_W + 0.5,  4.0, M_GLASS)

TOP_W = (half_width(TOP_DECK_H) + 3.5) * 2
solid_box('TopDeck_Structure', TOP_DECK_H - 3.0, TOP_W,       TOP_W,       7.0, M_DECK)
solid_box('TopDeck_Glass',     TOP_DECK_H - 1.5, TOP_W + 0.5, TOP_W + 0.5, 3.5, M_GLASS)


# ============================================================
# 7. アンテナ（角柱 307 m → 332.9 m）
# ============================================================

solid_box('Antenna', ANTENNA_BASE_H, 0.6, 0.6, ANTENNA_LEN, M_ANT)


# ============================================================
# 8. 昼景カメラ・太陽光
# ============================================================

bpy.ops.object.camera_add(
    location=(300.0, -280.0, 170.0),
    rotation=(math.radians(74), 0.0, math.radians(47)),
)
cam_day = bpy.context.active_object
cam_day.name = 'Camera_Day'
cam_day.data.lens = 35.0

bpy.ops.object.light_add(
    type='SUN',
    location=(120.0, -80.0, 350.0),
    rotation=(math.radians(38), 0.0, math.radians(32)),
)
sun = bpy.context.active_object
sun.name = 'Sun_Main'
sun.data.energy = 4.0

# 昼景モードでは昼景カメラをアクティブに
if not ENABLE_NIGHT:
    bpy.context.scene.camera = cam_day


# ============================================================
# 9. 夜景シーン（t004: グラウンドスポット・航空障害灯・室内光）
# ============================================================

def _apply_emission(mat: bpy.types.Material,
                    color: tuple, strength: float) -> None:
    """Principled BSDF に Emission を設定する（夜景発光）"""
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    if not bsdf:
        return
    for key in ('Emission Color', 'Emission'):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = color
            break
    bsdf.inputs['Emission Strength'].default_value = strength


def _add_spot_light(loc: tuple, aim: Vector,
                    energy: float, spot_deg: float,
                    color: tuple, name: str) -> bpy.types.Object:
    """スポットライトを配置し aim に向ける"""
    bpy.ops.object.light_add(type='SPOT', location=loc)
    obj = bpy.context.active_object
    obj.name              = name
    obj.data.energy       = energy
    obj.data.spot_size    = math.radians(spot_deg)
    obj.data.spot_blend   = 0.25
    obj.data.color        = color
    # -Z 軸を aim 方向に向ける（mathutils の track_quat 利用）
    direction = aim - Vector(loc)
    direction.normalize()
    obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    return obj


def _add_point_light(loc: tuple, energy: float,
                     color: tuple, name: str) -> bpy.types.Object:
    bpy.ops.object.light_add(type='POINT', location=loc)
    obj = bpy.context.active_object
    obj.name            = name
    obj.data.energy     = energy
    obj.data.color      = color
    obj.data.shadow_soft_size = 0.3
    return obj


if ENABLE_NIGHT:
    # 太陽光を夜景モードで無効化
    sun.data.energy = 0.0

    # ── 夜空バックグラウンド ──────────────────────────────────────
    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new('World')
        bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get('Background')
    if bg:
        bg.inputs['Color'].default_value    = (*NIGHT_SKY_RGB, 1.0)
        bg.inputs['Strength'].default_value = 0.05

    # ── グラウンドスポットライト（6 方向）────────────────────────
    aim_pt = Vector((0.0, 0.0, AIM_HEIGHT))
    D, H = SPOT_DISTANCE, SPOT_HEIGHT
    D45 = D * math.sqrt(0.5)
    spot_locs = [
        ( D,    0, H), (-D,    0, H),
        ( 0,    D, H), ( 0,   -D, H),
        ( D45,  D45, H), (-D45, -D45, H),
    ]
    for i, loc in enumerate(spot_locs):
        _add_spot_light(loc, aim_pt, SPOT_ENERGY, spot_deg=20,
                        color=SPOT_COLOR, name=f'Spot_{i:02d}')

    # 展望台真下からの補助スポット（より高い位置の照射を補完）
    _add_spot_light((0, 0, 1), Vector((0, 0, TOWER_HEIGHT * 0.7)),
                    energy=1500, spot_deg=15,
                    color=(0.9, 0.8, 0.6), name='Spot_Center')

    # ── 展望台ガラス窓の室内発光 ────────────────────────────────
    _apply_emission(M_GLASS, (1.0, 0.85, 0.5, 1.0), strength=WINDOW_EMIT)

    # ── 航空障害灯（赤色点灯）: 主要高さ × 4 方向 ──────────────
    aviation_heights = [45, 90, MAIN_DECK_H, 200, TOP_DECK_H, 280, 307, 325]
    for zh in aviation_heights:
        hw = half_width(zh) + 1.2
        for xi, (xo, yo) in enumerate([(hw, 0), (-hw, 0), (0, hw), (0, -hw)]):
            _add_point_light((xo, yo, zh), AVL_ENERGY,
                             color=(1.0, 0.0, 0.0),
                             name=f'AvLight_{zh:.0f}_{xi}')

    # ── 夜景カメラ（ローアングル・広角・ドラマチック）────────────
    bpy.ops.object.camera_add(
        location=(200.0, -180.0, 55.0),
        rotation=(math.radians(83), 0.0, math.radians(48)),
    )
    cam_night = bpy.context.active_object
    cam_night.name       = 'Camera_Night'
    cam_night.data.lens  = 24.0   # 広角レンズで存在感を強調
    bpy.context.scene.camera = cam_night


# ============================================================
# 10. レンダリング設定（EEVEE / Cycles 共通）
# ============================================================

scene = bpy.context.scene
scene.render.engine        = RENDER_ENGINE
scene.render.resolution_x  = RENDER_W
scene.render.resolution_y  = RENDER_H
scene.render.film_transparent = False

# EEVEE 固有の設定（EEVEE Next や Cycles では一部無視される）
try:
    ee = scene.eevee
    ee.use_bloom             = True
    ee.bloom_intensity       = 0.9
    ee.bloom_threshold       = 0.7
    ee.bloom_radius          = 4.0
    ee.use_ambient_occlusion = True
    ee.ao_distance           = 3.0
    ee.taa_render_samples    = 64
except AttributeError:
    pass

# Cycles 設定（engine='CYCLES' に切り替えた場合に有効）
scene.cycles.samples       = CYCLES_SAMPLES
scene.cycles.use_denoising = True


# ============================================================
# 11. 確認サマリー
# ============================================================

n_orange = len(truss_orange.data.splines)
n_white  = len(truss_white.data.splines)
panels   = len(heights) - 1

print("=" * 62)
print("  東京タワー 基本構造 + マテリアル + ライティング 生成完了")
print(f"  全高           : {TOWER_HEIGHT} m")
print(f"  パネルレベル   : {len(heights)} 段 / パネル数: {panels}")
print(f"  スプライン(橙) : {n_orange} 本")
print(f"  スプライン(白) : {n_white} 本")
print(f"  合計スプライン : {n_orange + n_white} 本")
print(f"  大展望台       : {MAIN_DECK_H} m  幅 {MAIN_W:.1f} m")
print(f"  特別展望台     : {TOP_DECK_H} m  幅 {TOP_W:.1f} m")
print(f"  アンテナ       : {ANTENNA_BASE_H} m ～ {ANTENNA_BASE_H + ANTENNA_LEN} m")
print(f"  モード         : {'夜景' if ENABLE_NIGHT else '昼景'}")
print(f"  レンダラー     : {RENDER_ENGINE}")
print(f"  解像度         : {RENDER_W} × {RENDER_H}")
print("=" * 62)
