"""Shared teleop display helpers; no task loading, scoring or recording.

Virtual camera pose changes are restored by the render caller.
"""
import numpy as np

TABLE_BODIES = ('table', 'living_room_table', 'living_room_table_col', 'study_table', 'kitchen_table')

def targets_of(row):
    task = row["task"]
    return list(task.get("targets") or [task["target_object"]["instance"]])


def region_for(row, target):
    region = row["goal_region"]
    return dict(region, **region["per_target"][target]) if region.get("per_target") else region


class VirtualCameraMixin:
    # 仮想カメラ: 既存のワールドカメラ（birdview）の姿勢を描画時だけ書き換え、グリッパの横から把持部を映す。
    # model の cam_pos/cam_quat を触るだけで、state・XML・保存物には入らない。
    VIRTUAL = {'gripper_side': 'birdview'}
    default_small = ('gripper_side',)

    def virtual_camera(self, name):
        real = self.VIRTUAL.get(name)
        if real is None:
            return name
        sim = self.raw.sim; m = sim.model
        cid = m.camera_name2id(real)
        eef = sim.data.site_xpos[m.site_name2id('gripper0_grip_site')].copy()
        lf = sim.data.body_xpos[m.body_name2id('gripper0_leftfinger')]; rf = sim.data.body_xpos[m.body_name2id('gripper0_rightfinger')]
        u = rf - lf; u[2] = 0.0
        if np.linalg.norm(u) < 1e-4:
            u = np.array([0., 1., 0.])
        u /= np.linalg.norm(u)
        up = np.array([0., 0., 1.]); z = np.cross(u, up)            # 右手系: x=u（画像の右）, y=上, z=x×y（カメラ手前）
        pos = eef + z * 0.30 - up * 0.010                           # 視線は -z。grip site より 1 cm 下から水平に見る（把持線がほぼ画面中心）
        Rm = np.stack([u, up, z], axis=1)
        from robosuite.utils.transform_utils import mat2quat
        q = mat2quat(Rm)                                           # xyzw
        m.cam_pos[cid] = pos; m.cam_quat[cid] = [q[3], q[0], q[1], q[2]]; m.cam_fovy[cid] = 30.0
        sim.forward()
        return real


def _project(sim, cam, h, w, points):
    from robosuite.utils.camera_utils import get_camera_transform_matrix
    M = get_camera_transform_matrix(sim, cam, h, w)
    pts = np.concatenate([points, np.ones((len(points), 1))], axis=1) @ M.T
    ok = pts[:, 2] > 1e-6
    pix = pts[:, :2] / np.where(ok[:, None], pts[:, 2:3], 1.0)
    return pix, ok    # pix = (x=col, y=row)


def draw_goal_overlay(env, row, im, cam, metrics=None):
    import cv2
    sim = env.sim; region = row['goal_region']; mode = region.get('mode', 'site_xy')
    if mode == 'articulation' or (region.get('site') is None and mode != 'on_object'):
        return   # 開閉・点火だけの行: 描く領域が無い
    if region.get('per_target') and not region.get('_drawing_one'):
        # 対象ごとの領域を順に描く（共通部分の線は最初の対象で描く）
        for k, tg in enumerate(targets_of(row)):
            sub = dict(row, goal_region=dict(region_for(row, tg), _drawing_one=True, _only_target=tg, _first=(k == 0)))
            draw_goal_overlay(env, sub, im, cam, metrics)
        return
    def _top(geoms):
        return max(float(sim.data.geom_xpos[i, 2] + sim.data.geom_xmat[i].reshape(3, 3)[2] @ sim.model.geom_aabb[i, :3]
                         + np.abs(sim.data.geom_xmat[i].reshape(3, 3)[2]) @ sim.model.geom_aabb[i, 3:]) for i in geoms)
    if mode == 'on_object':
        # 物体（皿）の上: 物体中心に半径 r の円。基準高さは物体の衝突 geom の最高点
        bid = env.env.obj_body_id[region['object']]; bp = sim.data.body_xpos[bid]
        og = [i for i in range(sim.model.ngeom) if sim.model.geom_bodyid[i] == bid and (sim.model.geom_contype[i] or sim.model.geom_conaffinity[i])]
        top_z = (_top(og) if og else float(bp[2])) + 0.002
        c = np.array([bp[0], bp[1], top_z]); R = np.eye(3); hx = hy = float(region.get('radius', 0.03)); hz = 0.
    else:
        sid = sim.model.site_name2id(region['site'])
        c = sim.data.site_xpos[sid]; R = sim.data.site_xmat[sid].reshape(3, 3); hx, hy, hz = sim.model.site_size[sid, :3]
        support = tuple(region.get('support_bodies') or ())
        geoms = [i for i in range(sim.model.ngeom) if sim.model.body_id2name(sim.model.geom_bodyid[i]) in support
                 and (sim.model.geom_contype[i] or sim.model.geom_conaffinity[i])]
        top_z = (_top(geoms) + 0.002) if geoms else float(c[2] - hz)
    # 机の天板の高さ。支持面が家具の天板（RT02）のときは、その site の XY の外では机の高さへ落とす。
    table_geoms = [i for i in range(sim.model.ngeom) if sim.model.body_id2name(sim.model.geom_bodyid[i]) in TABLE_BODIES
                   and (sim.model.geom_contype[i] or sim.model.geom_conaffinity[i])]
    table_z = (max(float(sim.data.geom_xpos[i, 2] + sim.data.geom_xmat[i].reshape(3, 3)[2] @ sim.model.geom_aabb[i, :3]
                         + np.abs(sim.data.geom_xmat[i].reshape(3, 3)[2]) @ sim.model.geom_aabb[i, 3:]) for i in table_geoms) + 0.002) if table_geoms else top_z

    def surface_z(xy):
        d = R.T @ np.array([xy[0] - c[0], xy[1] - c[1], 0.])
        return top_z if (abs(d[0]) < hx and abs(d[1]) < hy) else min(top_z, table_z)
    corners = np.array([c + R @ np.array([sx * hx, sy * hy, 0.]) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
    corners[:, 2] = top_z; center = np.array([[c[0], c[1], top_z]])
    h, w = im.shape[:2]
    pix, ok = _project(sim, cam, h, w, np.concatenate([corners, center]))
    inside = bool(metrics and metrics.get('in_goal_region')); reached = bool(metrics and metrics.get('goal'))
    color = (60, 220, 60) if not inside else ((255, 200, 0) if reached else (255, 255, 0))
    if mode == 'contain':
        # 引き出し内などの箱型 region: 3D の箱の 12 辺を描く
        box = np.array([c + R @ (np.array([sx, sy, sz]) * [hx, hy, hz]) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
        bp, bok = _project(sim, cam, h, w, box)
        for a_, b_ in ((0, 1), (0, 2), (1, 3), (2, 3), (4, 5), (4, 6), (5, 7), (6, 7), (0, 4), (1, 5), (2, 6), (3, 7)):
            if bok[a_] and bok[b_]:
                cv2.line(im, tuple(np.round(bp[a_]).astype(int)), tuple(np.round(bp[b_]).astype(int)), color, 1, cv2.LINE_AA)
    elif mode == 'on_object':
        ang = np.linspace(0, 2 * np.pi, 25)
        ring = np.stack([c[0] + hx * np.cos(ang), c[1] + hy * np.sin(ang), np.full_like(ang, top_z)], 1)
        rp, rok = _project(sim, cam, h, w, ring)
        for a_ in range(24):
            if rok[a_] and rok[a_ + 1]:
                cv2.line(im, tuple(np.round(rp[a_]).astype(int)), tuple(np.round(rp[a_ + 1]).astype(int)), color, 2, cv2.LINE_AA)
    elif ok[:4].all():
        cv2.polylines(im, [pix[:4].astype(np.int32).reshape(-1, 1, 2)], True, color, 2, cv2.LINE_AA)
    if ok[4]:
        x, y = int(round(pix[4, 0])), int(round(pix[4, 1])); r = max(6, h // 60)
        cv2.line(im, (x - r, y), (x + r, y), color, 2, cv2.LINE_AA); cv2.line(im, (x, y - r), (x, y + r), color, 2, cv2.LINE_AA)
    # 対象中心から支持面への垂線と、グリッパ中心へつながる高さ・水平位置のガイド。
    orange = (0, 160, 255)
    only = region.get('_only_target')
    for target in ([only] if only else targets_of(row)):
        opos = sim.data.body_xpos[env.env.obj_body_id[target]].copy()
        origin = opos.copy()
        draw_vertical_guide(env, im, cam, origin, surface_z, orange,
                            target_center=opos, exclude_bodies=(env.env.obj_body_id[target],))
        draw_target_gripper_connection(env, im, cam, opos)
    if region.get('_drawing_one') and not region.get('_first'):
        return
    draw_gripper_guides(env, im, cam, surface_z)


def draw_target_gripper_connection(env, im, cam, target):
    """World-vertical target line meets a world-horizontal line to grip center."""
    import cv2
    sim = env.sim
    try:
        eef = sim.data.site_xpos[sim.model.site_name2id('gripper0_grip_site')].copy()
    except (KeyError, ValueError):
        return
    elbow = np.array([target[0], target[1], eef[2]])
    points, visible = _project(sim, cam, *im.shape[:2], np.array([target, elbow, eef]))
    for a, b, color in ((0, 1, (0, 160, 255)), (1, 2, (255, 220, 0))):
        if visible[a] and visible[b]:
            cv2.line(im, tuple(np.round(points[a]).astype(int)),
                     tuple(np.round(points[b]).astype(int)), color, 1, cv2.LINE_AA)
    if visible[1]:
        cv2.circle(im, tuple(np.round(points[1]).astype(int)), 3,
                   (255, 220, 0), -1, cv2.LINE_AA)


def draw_vertical_guide(env, im, cam, origin, surface_z, color,
                        target_center=None, exclude_bodies=()):
    """Shared solid upper line, dashed lower line and surface/table markers."""
    import cv2
    sim = env.sim; h, w = im.shape[:2]
    from surface_guide import surface_below
    table_surface = table_surface_z(env)
    foot_z = (table_surface or surface_z)(origin[:2])
    if origin[2] <= foot_z:
        return
    hit_z = surface_below(sim, origin if target_center is None else target_center,
                          exclude_bodies=exclude_bodies)
    interrupted = hit_z is not None and foot_z + 0.003 < hit_z < origin[2]

    def vertical_line(z_start, z_end, faint=False):
        n = 24 if faint else 1
        pts = np.array([[origin[0], origin[1], z] for z in np.linspace(z_start, z_end, n + 1)])
        pix, valid = _project(sim, cam, h, w, pts)
        layer = im.copy() if faint else im
        for k in range(0, n, 2):
            if valid[k] and valid[k + 1]:
                cv2.line(layer, tuple(np.round(pix[k]).astype(int)),
                         tuple(np.round(pix[k + 1]).astype(int)), color, 1, cv2.LINE_AA)
        if faint:
            cv2.addWeighted(layer, 0.55, im, 0.45, 0, dst=im)

    if interrupted:
        vertical_line(hit_z, foot_z, faint=True)
        vertical_line(origin[2], hit_z)
    else:
        vertical_line(origin[2], foot_z)
    points = [[origin[0], origin[1], foot_z]]
    if interrupted:
        points.append([origin[0], origin[1], hit_z])
    pix, valid = _project(sim, cam, h, w, np.array(points))
    if valid[0]:
        # 机は丸、途中の表面は菱形。同じ色・濃さで区別する。
        cv2.circle(im, tuple(np.round(pix[0]).astype(int)), max(4, h // 90), color, 2, cv2.LINE_AA)
    if interrupted and valid[1]:
        x, y = np.round(pix[1]).astype(int); r = max(5, h // 80)
        diamond = np.array([[x, y-r], [x+r, y], [x, y+r], [x-r, y]], dtype=np.int32)
        cv2.polylines(im, [diamond], True, color, 2, cv2.LINE_AA)
    if target_center is not None:
        cp, visible = _project(sim, cam, h, w, np.array([target_center]))
        if visible[0]:
            cv2.circle(im, tuple(np.round(cp[0]).astype(int)), max(3, h // 120),
                       color, -1, cv2.LINE_AA)



def draw_gripper_guides(env, im, cam, surface_z):
    """グリッパの鉛直線・着地点・把持線。表示だけで state・保存物には入らない。全 suite 共通で使う。"""
    import cv2
    sim = env.sim; h, w = im.shape[:2]
    # グリッパ中心から机へ垂線を投影し、途中の表面も示す。
    try:
        eef = sim.data.site_xpos[sim.model.site_name2id('gripper0_grip_site')].copy()
    except Exception:
        return
    magenta = (255, 80, 255)
    draw_vertical_guide(env, im, cam, eef, surface_z, magenta)
    # 把持中心が物体の中（ボトルを掴む姿勢など）なら、垂線を物体の上端まで伸ばして
    # 境界に丸を打つ（2026-09-16 ユーザー要望。下向きは従来から途中の面を菱形で示す）。
    from surface_guide import enclosing_top
    top_z = enclosing_top(sim, eef)
    if top_z is not None and top_z > eef[2] + 1e-4:
        pix, ok = _project(sim, cam, h, w, np.array([eef, [eef[0], eef[1], top_z]]))
        if ok.all():
            P = [tuple(np.round(q).astype(int)) for q in pix]
            cv2.line(im, P[0], P[1], magenta, 1, cv2.LINE_AA)
            cv2.circle(im, P[1], max(4, h // 90), magenta, 2, cv2.LINE_AA)
    # 把持線: 両指を結ぶ向き（3D）を grip site 中心に置き、開き幅の内側だけを描く。
    # 指の位置ベクトルを使うので、手首の回転・傾きにそのまま追従する。中心に点を打つ。
    try:
        lf = sim.data.body_xpos[sim.model.body_name2id('gripper0_leftfinger')]
        rf = sim.data.body_xpos[sim.model.body_name2id('gripper0_rightfinger')]
    except Exception:
        return
    axis = rf - lf; width = float(np.linalg.norm(axis))
    if width < 1e-4:
        return
    u = axis / width; half = width / 2
    pts = np.array([eef - u * half, eef, eef + u * half])
    gp, gok = _project(sim, cam, h, w, pts)
    cyan = (255, 220, 0)
    if gok.all():
        P = [tuple(np.round(q).astype(int)) for q in gp]
        cv2.line(im, P[0], P[2], cyan, 2, cv2.LINE_AA)            # 指の間（開き幅）
        cv2.circle(im, P[1], max(3, h // 120), cyan, -1, cv2.LINE_AA)   # 把持中心


def row_from_goal(env):
    """BDDL の :goal（On / In）から、overlay 用の row（対象と戻し先）を作る。revtasks 以外の suite で使う。"""
    sim = env.sim; targets, per = [], {}
    for g in env.env.parsed_problem.get('goal_state', []):
        if len(g) < 3 or g[0].lower() not in ('on', 'in'):
            continue
        obj, dest = g[1], g[2]
        if obj not in env.env.objects_dict:
            continue
        if dest in env.env.object_sites_dict:
            sid = sim.model.site_name2id(dest); parent = sim.model.body_id2name(sim.model.site_bodyid[sid]) or ''
            region = dict(site=dest, mode='contain' if g[0].lower() == 'in' else 'site_xy', support_bodies=[parent] + [b for b in TABLE_BODIES if b != parent])
        elif dest in env.env.objects_dict:
            region = dict(site=None, mode='on_object', object=dest, radius=0.03, support_bodies=[])
        else:
            continue
        targets.append(obj); per[obj] = region
    if not targets:
        return None
    goal_region = dict(per[targets[0]], reference_objects=[], ranges=None)
    if len(targets) > 1:
        goal_region['per_target'] = per
    return dict(task=dict(targets=targets, target_object=dict(instance=targets[0])), goal_region=goal_region)


def table_surface_z(env):
    sim = env.sim
    geoms = [i for i in range(sim.model.ngeom) if sim.model.body_id2name(sim.model.geom_bodyid[i]) in TABLE_BODIES and (sim.model.geom_contype[i] or sim.model.geom_conaffinity[i])]
    if not geoms:
        # Floor arenas may have no named table body.
        import mujoco
        geoms = [i for i in range(sim.model.ngeom)
                 if sim.model.geom_type[i] == mujoco.mjtGeom.mjGEOM_PLANE
                 and (sim.model.geom_contype[i] or sim.model.geom_conaffinity[i])]
        if not geoms:
            return None
        z = max(float(sim.data.geom_xpos[i, 2]) for i in geoms) + 0.002
        return lambda xy: z
    z = max(float(sim.data.geom_xpos[i, 2] + sim.data.geom_xmat[i].reshape(3, 3)[2] @ sim.model.geom_aabb[i, :3] + np.abs(sim.data.geom_xmat[i].reshape(3, 3)[2]) @ sim.model.geom_aabb[i, 3:]) for i in geoms) + 0.002
    return lambda xy: z


class GuideEnv(VirtualCameraMixin):
    """revtasks 以外の suite に、同じ表示補助（ゴール領域・グリッパ鉛直線・把持線・gripper_side）を付ける薄い wrapper。
    判定や記録には関与しない（state・XML・保存物は raw env のまま）。"""

    def __init__(self, raw):
        self.raw = raw
        self._row = None
        self._row_model = None

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def draw_goal_overlay(self, im, cam):
        cam = self.VIRTUAL.get(cam, cam)
        model_id = id(self.raw.sim.model)
        if self._row_model != model_id:
            try:
                self._row = row_from_goal(self.raw)
            except Exception:
                self._row = None
            self._row_model = model_id
        try:
            if self._row:
                draw_goal_overlay(self.raw, self._row, im, cam, None)
            else:
                surface = table_surface_z(self.raw)
                if surface:
                    draw_gripper_guides(self.raw, im, cam, surface)
        except Exception:
            pass   # 表示補助が原因で操作を止めない
