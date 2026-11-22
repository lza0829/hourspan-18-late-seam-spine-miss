"""late_snapshot_apply · 迟到快照缝链

落库形态（题面二选一，取【甲】）：独立迟到审计表 ``late_snapshot_audit``
  —— 记录快照 id、原镜 as_of、被切开 hist_id、缝入 hist_id；
  不向 hist 增加不可变列，禁止静默改镜，禁止无审计丢弃。

失败闭（题面二选一，取【甲】）：缝链、审计与缝点在调用方同一事务内提交；
  写入前先把整批迟到快照在内存草稿上推演并校验区间，
  任一区间重叠/链断裂即抛 :class:`LateApplyError`，整笔回滚；
  写库后再做一次区间与缝点复验，杜绝「审计成功但区间重叠」与「缝成功而无缝点」。

缝点（衔接边）``late_seam_points`` 与缝链同生共死：迟到应用一旦标成功，
  必留下 store_id / late_as_of / 前段 hist / 本段 hist / 后段 hist(可空)，
  供「跨缝点查」按缝点导航（架构甲，见 :mod:`app.modules.late_seam_spine`）。

铁律：
  1. 迟到判定：``snapshot.as_of_ts < current_mirror.as_of_ts``（严格早于）。
  2. 迟到快照禁止盖写当前镜；镜 as_of 永不被迟到拖回，镜仍只指时间轴最新段。
  3. 按 as_of 切开被跨越旧段（旧段 valid_to 收至迟到点），插入迟到段
     （is_current=0），迟到段 valid_to 重接原后续边界，链保持连续无重叠。
  4. 每条迟到痕迹必落审计与缝点；本模块不自行 commit，参与入档批事务。
"""

import json
from uuid import uuid4

from app.seed import now_iso

# 无穷远上界：is_current 段 valid_to 为 NULL，比较时用哨兵
_INF = "￿"


class LateApplyError(Exception):
    """迟到缝链失败闭：区间重叠 / 链已损坏 / 非迟到快照误入 / 缝点缺失。整笔须回滚。"""


def classify(conn, snapshot_ids):
    """入档分流：按门店当前镜 as_of 把贴源快照分为 normal / late 两组。

    严格早于镜 as_of 才判迟到；无镜或 as_of 相等/更新走正常检测链路。
    """
    normal, late = [], []
    for sid in snapshot_ids:
        snap = conn.execute(
            "SELECT * FROM source_snapshots WHERE id=?", (sid,)
        ).fetchone()
        if not snap:
            continue
        mirror = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?",
            (snap["store_id"],),
        ).fetchone()
        if mirror and snap["as_of_ts"] < mirror["as_of_ts"]:
            late.append(snap)
        else:
            normal.append(snap)
    return normal, late


def _load_chain(conn, store_id):
    """取门店全链，按 valid_from 排序；返回可原地改写的区间字典列表。"""
    rows = conn.execute(
        """SELECT id, valid_from, valid_to, is_current
           FROM dim_store_hours_hist
           WHERE store_id=? ORDER BY valid_from, id""",
        (store_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "from": r["valid_from"],
            "to": r["valid_to"],  # None = 无穷远（当前段）
            "is_current": r["is_current"],
        }
        for r in rows
    ]


def _assert_chain_intact(chain, store_id):
    """失败闭前置：既有链本身必须连续、无重叠、恰一个当前段。"""
    if not chain:
        raise LateApplyError(f"门店 {store_id} 无历史链，无法缝入迟到段")
    current = [seg for seg in chain if seg["is_current"] == 1]
    if len(current) != 1:
        raise LateApplyError(
            f"门店 {store_id} is_current 段数={len(current)}，链已损坏，拒绝缝链"
        )
    if current[0] is not chain[-1] or chain[-1]["to"] is not None:
        raise LateApplyError(f"门店 {store_id} 当前段不在链尾或 valid_to 非空，链已损坏")
    for prev, nxt in zip(chain, chain[1:]):
        if prev["from"] >= nxt["from"]:
            raise LateApplyError(
                f"门店 {store_id} 区间重叠：{prev['id']}({prev['from']}) !< "
                f"{nxt['id']}({nxt['from']})"
            )
        if prev["to"] != nxt["from"]:
            raise LateApplyError(
                f"门店 {store_id} 链断裂：{prev['id']}.valid_to={prev['to']} "
                f"!= {nxt['id']}.valid_from={nxt['from']}"
            )
        if prev["is_current"] != 0:
            raise LateApplyError(f"门店 {store_id} 非尾段 {prev['id']} 标记为当前段")


def _plan_one(chain, as_of, store_id):
    """在内存链上推演一次迟到缝入，返回 (plan, 新链)。

    新段 id 在草稿阶段即分配，供同批后续迟到点再次切开本段时正确定位
    （否则 split_id 为 None，写库收不上 valid_to，链会断）。

    重叠判定：as_of 与任一段起点重合 → 重叠（含同批两个迟到点相同）。
    """
    containing = None
    containing_idx = None
    for idx, seg in enumerate(chain):
        end = seg["to"] if seg["to"] is not None else _INF
        if as_of == seg["from"]:
            raise LateApplyError(
                f"门店 {store_id} 迟到 as_of={as_of} 与既有段 {seg['id']} "
                f"边界重合，区间重叠，整笔拒绝"
            )
        if seg["from"] < as_of < end:
            containing = seg
            containing_idx = idx
            break

    new_id = f"HIST-LATE-{uuid4().hex[:10]}"

    if containing is None:
        # 早于链首：前置新段，valid_to 重接链首边界，无需切开旧段
        head = chain[0]
        if as_of >= head["from"]:  # 理论不可达，防御
            raise LateApplyError(
                f"门店 {store_id} as_of={as_of} 无法定位缝入点"
            )
        plan = {
            "kind": "prepend",
            "split_id": None,
            "prev_hist_id": None,
            "this_hist_id": new_id,
            "next_hist_id": head["id"],
            "stitch_to": head["from"],
        }
        new_seg = {"id": new_id, "from": as_of, "to": head["from"], "is_current": 0}
        return plan, [new_seg, *chain]

    # 切开包含段：旧段 valid_to 收至 as_of，新迟到段承接原上界
    if containing["to"] is None:
        # 不可达防御：当前段起点=镜 as_of，迟到 as_of 严格更早，不可能落入当前段
        raise LateApplyError(
            f"门店 {store_id} 迟到点 {as_of} 落入当前段 {containing['id']}，"
            "镜/链 invariant 已破坏，拒绝缝链"
        )
    next_seg = chain[containing_idx + 1] if containing_idx + 1 < len(chain) else None
    plan = {
        "kind": "split",
        "split_id": containing["id"],
        "prev_hist_id": containing["id"],
        "this_hist_id": new_id,
        "next_hist_id": next_seg["id"] if next_seg else None,
        "stitch_to": containing["to"],
    }
    new_seg = {"id": new_id, "from": as_of, "to": containing["to"], "is_current": 0}
    containing["to"] = as_of
    return plan, [*chain[: containing_idx + 1], new_seg, *chain[containing_idx + 1 :]]


def _build_plans(conn, late_snaps):
    """整批草稿推演：跨门店分组、组内按 as_of 排序，全部校验通过才允许写库。"""
    grouped = {}
    for snap in late_snaps:
        grouped.setdefault(snap["store_id"], []).append(snap)

    plans = []
    for store_id, snaps in grouped.items():
        chain = _load_chain(conn, store_id)
        _assert_chain_intact(chain, store_id)
        mirror = conn.execute(
            "SELECT as_of_ts FROM dim_store_hours_current WHERE store_id=?",
            (store_id,),
        ).fetchone()
        mirror_as_of = mirror["as_of_ts"]
        for snap in sorted(snaps, key=lambda r: (r["as_of_ts"], r["id"])):
            if snap["as_of_ts"] >= mirror_as_of:
                # 分流与执行间的防御性校验，失败闭不误缝
                raise LateApplyError(
                    f"快照 {snap['id']} as_of={snap['as_of_ts']} 不早于镜 "
                    f"as_of={mirror_as_of}，不得走迟到缝链"
                )
            plan, chain = _plan_one(chain, snap["as_of_ts"], store_id)
            plan.update(
                {
                    "store_id": store_id,
                    "snapshot_id": snap["id"],
                    "late_as_of": snap["as_of_ts"],
                    "mirror_as_of_before": mirror_as_of,
                }
            )
            plans.append((plan, snap))
        # 推演后链仍须完整（连续 + 单当前段）
        _assert_chain_intact(chain, store_id)
    return plans


def _seam_points(conn, store_id):
    return conn.execute(
        """SELECT * FROM late_seam_points
           WHERE store_id=? ORDER BY late_as_of_ts, id""",
        (store_id,),
    ).fetchall()


def _seam_point_errors(conn, store_id):
    """缝点核证（只读）：返回错误串列表，空列表即全通过。

    铁律：
      - 禁止缝成功而无缝点记录（每条审计须有对应缝点）；
      - 禁止缝点 as_of 与本段 valid_from 分叉；
      - 禁止前后 hist 与邻接实链分叉。
    """
    errs = []
    chain = _load_chain(conn, store_id)
    if not chain:
        return [f"门店 {store_id} 无历史链"]
    index = {seg["id"]: i for i, seg in enumerate(chain)}
    seams = _seam_points(conn, store_id)
    audits = conn.execute(
        "SELECT * FROM late_snapshot_audit WHERE store_id=? ORDER BY id",
        (store_id,),
    ).fetchall()

    seam_by_this = {}
    for s in seams:
        if s["this_hist_id"] in seam_by_this:
            errs.append(f"缝点重复：{s['this_hist_id']}")
        seam_by_this[s["this_hist_id"]] = s
        i = index.get(s["this_hist_id"])
        if i is None:
            errs.append(f"缝点 {s['id']} 本段 hist 不在链中：{s['this_hist_id']}")
            continue
        this = chain[i]
        prev_seg = chain[i - 1] if i > 0 else None
        next_seg = chain[i + 1] if i + 1 < len(chain) else None
        if this["is_current"] != 0:
            errs.append(f"缝点 {s['id']} 本段 hist 标记为当前段")
        if this["from"] != s["late_as_of_ts"]:
            errs.append(
                f"缝点 {s['id']} as_of 与本段 valid_from 分叉："
                f"{s['late_as_of_ts']} != {this['from']}"
            )
        expected_prev = prev_seg["id"] if prev_seg else None
        expected_next = next_seg["id"] if next_seg else None
        if s["prev_hist_id"] != expected_prev:
            errs.append(
                f"缝点 {s['id']} 前段 hist 与实链分叉："
                f"{s['prev_hist_id']} != {expected_prev}"
            )
        if s["next_hist_id"] != expected_next:
            errs.append(
                f"缝点 {s['id']} 后段 hist 与实链分叉："
                f"{s['next_hist_id']} != {expected_next}"
            )
        if prev_seg is not None and prev_seg["to"] != this["from"]:
            errs.append(f"缝点 {s['id']} 前段 valid_to 未接本段 valid_from")
        if next_seg is not None and this["to"] != next_seg["from"]:
            errs.append(f"缝点 {s['id']} 本段 valid_to 未接后段 valid_from")

    for a in audits:
        s = seam_by_this.get(a["stitched_hist_id"])
        if not s:
            errs.append(
                f"审计 {a['id']} 缝成功但无缝点记录：{a['stitched_hist_id']}"
            )
            continue
        if s["late_as_of_ts"] != a["late_as_of_ts"]:
            errs.append(f"审计 {a['id']} 缝点 as_of 与审计分叉")
        if a["split_hist_id"] is not None and s["prev_hist_id"] != a["split_hist_id"]:
            errs.append(f"审计 {a['id']} 缝点前段与切开 hist 分叉")
    return errs


def apply_late(conn, late_snapshot_ids, batch_id):
    """在调用方事务内执行迟到缝链 + 审计 + 缝点落库；不 commit、不动当前镜。

    返回审计摘要列表。任何区间/缝点问题抛 :class:`LateApplyError`，由调用方回滚。
    """
    late_snaps = [
        conn.execute("SELECT * FROM source_snapshots WHERE id=?", (sid,)).fetchone()
        for sid in late_snapshot_ids
    ]
    late_snaps = [s for s in late_snaps if s is not None]
    if not late_snaps:
        return []

    plans = _build_plans(conn, late_snaps)
    created_at = now_iso()
    audit_summary = []

    for plan, snap in plans:
        new_hist_id = plan["this_hist_id"]
        if plan["kind"] == "split":
            # 切开被跨越旧段：仅收 valid_to，行身份/内容不动
            conn.execute(
                "UPDATE dim_store_hours_hist SET valid_to=? WHERE id=?",
                (plan["late_as_of"], plan["split_id"]),
            )
        # 插入迟到段：永不 is_current；valid_to 重接原后续边界
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
              id, store_id, open_time, close_time, weekday_mask, biz_status,
              valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_hist_id,
                snap["store_id"],
                snap["open_time"],
                snap["close_time"],
                snap["weekday_mask"],
                snap["biz_status"],
                plan["late_as_of"],
                plan["stitch_to"],
                0,
                snap["id"],
                snap["row_hash"],
                None,
            ),
        )
        detail = {
            "form": "independent_audit_table",
            "failure_closure": "same_transaction_draft_validated",
            "stitch": plan["kind"],
        }
        audit_id = f"LATE-{uuid4().hex[:10]}"
        conn.execute(
            """INSERT INTO late_snapshot_audit(
              id, batch_id, store_id, snapshot_id, late_as_of_ts,
              mirror_as_of_before, split_hist_id, stitched_hist_id,
              detail_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                audit_id,
                batch_id,
                snap["store_id"],
                snap["id"],
                plan["late_as_of"],
                plan["mirror_as_of_before"],
                plan["split_id"],
                new_hist_id,
                json.dumps(detail, ensure_ascii=False),
                created_at,
            ),
        )
        plan["audit_id"] = audit_id
        audit_summary.append(
            {
                "audit_id": audit_id,
                "store_id": snap["store_id"],
                "snapshot_id": snap["id"],
                "late_as_of_ts": plan["late_as_of"],
                "mirror_as_of_before": plan["mirror_as_of_before"],
                "split_hist_id": plan["split_id"],
                "stitched_hist_id": new_hist_id,
            }
        )

    # 缝点：以「落库后最终实链」为准重算前/后段，保证与实缝区间一致；
    # 同处一个事务，任一缝点缺失/分叉即抛错整笔回滚。
    by_store = {}
    for plan, snap in plans:
        by_store.setdefault(plan["store_id"], []).append((plan, snap))

    for store_id, items in by_store.items():
        chain = _load_chain(conn, store_id)
        _assert_chain_intact(chain, store_id)
        index = {seg["id"]: i for i, seg in enumerate(chain)}
        for plan, _snap in items:
            i = index[plan["this_hist_id"]]
            this = chain[i]
            prev_seg = chain[i - 1] if i > 0 else None
            next_seg = chain[i + 1] if i + 1 < len(chain) else None
            if this["from"] != plan["late_as_of"]:
                raise LateApplyError(
                    f"门店 {store_id} 缝点 as_of={plan['late_as_of']} 与本段 "
                    f"valid_from={this['from']} 分叉，整笔回滚"
                )
            if prev_seg is not None and prev_seg["to"] != plan["late_as_of"]:
                raise LateApplyError(
                    f"门店 {store_id} 缝点前段 valid_to={prev_seg['to']} 未接 "
                    f"as_of={plan['late_as_of']}，整笔回滚"
                )
            if next_seg is not None and this["to"] != next_seg["from"]:
                raise LateApplyError(
                    f"门店 {store_id} 缝点本段 valid_to={this['to']} 未接后段 "
                    f"valid_from={next_seg['from']}，整笔回滚"
                )
            conn.execute(
                """INSERT INTO late_seam_points(
                  id, store_id, late_as_of_ts, prev_hist_id, this_hist_id,
                  next_hist_id, batch_id, audit_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    f"SEAM-{uuid4().hex[:10]}",
                    store_id,
                    plan["late_as_of"],
                    prev_seg["id"] if prev_seg else None,
                    plan["this_hist_id"],
                    next_seg["id"] if next_seg else None,
                    batch_id,
                    plan["audit_id"],
                    created_at,
                ),
            )

    # 写后复验：真实落库链不得重叠/断裂，且每条迟到缝必有对齐的缝点
    for store_id in by_store:
        _assert_chain_intact(_load_chain(conn, store_id), store_id)
        seam_errs = _seam_point_errors(conn, store_id)
        if seam_errs:
            raise LateApplyError(
                f"门店 {store_id} 缝点核证失败，整笔回滚：" + "；".join(seam_errs)
            )
    return audit_summary


def attest_late(conn, store_id=None):
    """只读核证：原镜 as_of、缝入 hist、现镜 as_of 三方互证 + 全链区间/缝点复验。"""
    if store_id:
        audits = conn.execute(
            "SELECT * FROM late_snapshot_audit WHERE store_id=? ORDER BY id",
            (store_id,),
        ).fetchall()
    else:
        audits = conn.execute(
            "SELECT * FROM late_snapshot_audit ORDER BY store_id, id"
        ).fetchall()

    chain_cache = {}
    seam_err_cache = {}

    def chain_of(sid):
        if sid not in chain_cache:
            chain_cache[sid] = _load_chain(conn, sid)
        return chain_cache[sid]

    def seam_errs_of(sid):
        if sid not in seam_err_cache:
            seam_err_cache[sid] = _seam_point_errors(conn, sid)
        return seam_err_cache[sid]

    results = []
    for a in audits:
        errs = []
        mirror = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?",
            (a["store_id"],),
        ).fetchone()
        if not mirror:
            errs.append("mirror_missing")
            current_mirror_as_of = None
            current_hist_id = None
        else:
            current_mirror_as_of = mirror["as_of_ts"]
            current_hist_id = mirror["hist_id"]
            # 铁律：镜 as_of 不得被迟到拖回
            if mirror["as_of_ts"] < a["mirror_as_of_before"]:
                errs.append("mirror_dragged_back")

        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?",
            (a["stitched_hist_id"],),
        ).fetchone()
        if not hist:
            errs.append("stitched_hist_missing")
        else:
            if hist["is_current"] != 0:
                errs.append("late_row_is_current")
            if hist["valid_from"] != a["late_as_of_ts"]:
                errs.append("late_as_of_mismatch")
            if hist["valid_to"] is None or hist["valid_to"] <= hist["valid_from"]:
                errs.append("stitched_interval_invalid")
            if hist["source_snapshot_id"] != a["snapshot_id"]:
                errs.append("snapshot_ref_mismatch")

        if a["split_hist_id"]:
            split_row = conn.execute(
                "SELECT valid_to FROM dim_store_hours_hist WHERE id=?",
                (a["split_hist_id"],),
            ).fetchone()
            if not split_row or split_row["valid_to"] != a["late_as_of_ts"]:
                errs.append("split_boundary_mismatch")

        # 缝点：缝成功必须留点，且点与审计/实链对齐（架构甲）
        seam = conn.execute(
            "SELECT * FROM late_seam_points WHERE this_hist_id=?",
            (a["stitched_hist_id"],),
        ).fetchone()
        if not seam:
            errs.append("seam_point_missing")
            seam_fields = {
                "seam_id": None,
                "prev_hist_id": None,
                "next_hist_id": None,
            }
        else:
            seam_fields = {
                "seam_id": seam["id"],
                "prev_hist_id": seam["prev_hist_id"],
                "next_hist_id": seam["next_hist_id"],
            }
            if seam["late_as_of_ts"] != a["late_as_of_ts"]:
                errs.append("seam_as_of_mismatch")
            if seam["prev_hist_id"] != a["split_hist_id"]:
                errs.append("seam_prev_mismatch")

        # 全链区间复验：重叠/断裂/多当前段一律失败
        try:
            _assert_chain_intact(chain_of(a["store_id"]), a["store_id"])
        except LateApplyError as exc:
            errs.append(f"chain_invalid:{exc}")

        # 门店级缝点核证（邻接实链、无点缝入等）
        for e in seam_errs_of(a["store_id"]):
            if e not in errs:
                errs.append(f"seam_invalid:{e}")

        # 镜仍指时间轴最新段（链尾当前段）
        if mirror and not errs:
            tail = chain_of(a["store_id"])[-1]
            if mirror["hist_id"] != tail["id"]:
                errs.append("mirror_not_latest_segment")

        results.append(
            {
                "audit_id": a["id"],
                "store_id": a["store_id"],
                "snapshot_id": a["snapshot_id"],
                "mirror_as_of_before": a["mirror_as_of_before"],
                "late_as_of_ts": a["late_as_of_ts"],
                "stitched_hist_id": a["stitched_hist_id"],
                "split_hist_id": a["split_hist_id"],
                "seam_id": seam_fields["seam_id"],
                "prev_hist_id": seam_fields["prev_hist_id"],
                "next_hist_id": seam_fields["next_hist_id"],
                "current_mirror_as_of": current_mirror_as_of,
                "current_hist_id": current_hist_id,
                "valid": 0 if errs else 1,
                "errors": errs,
                "created_at": a["created_at"],
            }
        )
    return results
