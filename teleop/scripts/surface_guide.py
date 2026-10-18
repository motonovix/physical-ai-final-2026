"""Display-only vertical surface queries. Never modify simulation/model arrays."""
import mujoco
import numpy as np


def _excluded_bodies(model, exclude_bodies):
    excluded = np.zeros(model.nbody, dtype=bool)
    for bid in range(1, model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, bid) or ""
        excluded[bid] = excluded[model.body_parentid[bid]] or bid in exclude_bodies or name.startswith(("robot", "gripper"))
    return excluded


def _ray_geom(model, data, gid, origin, direction):
    kind = model.geom_type[gid]
    if kind == mujoco.mjtGeom.mjGEOM_MESH:
        return mujoco.mj_rayMesh(model, data, gid, origin, direction)
    if kind == mujoco.mjtGeom.mjGEOM_HFIELD:
        return mujoco.mj_rayHfield(model, data, gid, origin, direction)
    return mujoco.mju_rayGeom(data.geom_xpos[gid], data.geom_xmat[gid],
                              model.geom_size[gid], origin, direction, kind)


def _ray_hits(sim, origin, direction, exclude_bodies=()):
    """(geom id, distance) for every collidable non-robot geom the ray hits, nearest first."""
    model = getattr(sim.model, "_model", sim.model)
    data = getattr(sim.data, "_data", sim.data)
    origin = np.asarray(origin, dtype=np.float64)
    excluded = _excluded_bodies(model, exclude_bodies)
    hits = []
    for gid in range(model.ngeom):
        if excluded[model.geom_bodyid[gid]] or not (model.geom_contype[gid] or model.geom_conaffinity[gid]):
            continue
        distance = _ray_geom(model, data, gid, origin, direction)
        if distance >= 0:
            hits.append((gid, float(distance)))
    return sorted(hits, key=lambda h: h[1])


def surface_below(sim, origin, exclude_bodies=()):
    """Nearest collision surface below origin, excluding robot and explicitly supplied body trees."""
    hits = _ray_hits(sim, origin, np.array([0., 0., -1.]), exclude_bodies)
    return None if not hits else float(np.asarray(origin)[2] - hits[0][1])


def enclosing_top(sim, origin, exclude_bodies=()):
    """origin が物体の geom の内側にあるとき、その物体の上面の z（鉛直に上へ抜けた境界）。

    上向きと下向きの両方のレイが同じ geom に当たれば内側と見る（凸形状・メッシュとも
    レイ関数は内側からでも面までの距離を返す）。把持中心がボトルの中に入った場面で、
    グリッパの垂線を物体の上端まで伸ばして境界に丸を打つための表示専用の問い合わせ。
    複数の geom に囲まれていれば最も高い上面を返す。外側なら None。
    """
    up = {gid: d for gid, d in _ray_hits(sim, origin, np.array([0., 0., 1.]), exclude_bodies)}
    down = {gid for gid, _ in _ray_hits(sim, origin, np.array([0., 0., -1.]), exclude_bodies)}
    inside = [d for gid, d in up.items() if gid in down]
    if not inside:
        return None
    return float(np.asarray(origin)[2] + max(inside))
