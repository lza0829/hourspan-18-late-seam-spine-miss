"""late_snapshot_apply · 迟到快照缝链

落库形态（题面二选一，取【甲】）：独立迟到审计表 ``late_snapshot_audit``
  —— 记录快照 id、原镜 as_of、被切开 hist_id、缝入 hist_id；
  不向 hist 增加不可变列，禁止静默改镜，禁止无审计丢弃。

失败闭（题面二选一，取【甲】）：缝链与审计在调用方同一事务内提交；
  写入前先把整批迟到快照在内存草稿上推演并校验区间，
  任一区间重叠/链断裂即抛 :class:`LateApplyError`，整笔回滚；
  写库后再做一次区间复验，杜绝「审计成功但区间重叠」。

铁律：
  1. 迟到判定：``snapshot.as_of_ts < current_mirror.as_of_ts``（严格早于）。
  2. 迟到快照禁止盖写当前镜；镜 as_of 永不被迟到拖回，镜仍只指时间轴最新段。
  3. 按 as_of 切开被跨越旧段（旧段 valid_to 收至迟到点），插入迟到段
     （is_current=0），迟到段 valid_to 重接原后续边界，链保持连续无重叠。
  4. 每条迟到痕迹必落审计；本模块不自行 commit，参与入档批事务。
"""

import json
from uuid import uuid4

from app.seed import now_iso

# 无穷远上界：is_current 段 valid_to 为 NULL，比较时用哨兵
_INF = "￿"


class LateApplyError(Exception):
    """迟到缝链失败闭：区间重叠 / 链已损坏 / 非迟到快照误入。整笔须回滚。"""


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

    重叠判定：as_of 与任一段起点重合 → 重叠（含同批两个迟到点相同）。
    """
    containing = None
    for seg in chain:
        end = seg["to"] if seg["to"] is not None else _INF
        if as_of == seg["from"]:
            raise LateApplyError(
                f"门店 {store_id} 迟到 as_of={as_of} 与既有段 {seg['id']} "
                f"边界重合，区间重叠，整笔拒绝"
            )
        if seg["from"] < as_of < end:
            containing = seg
            break

    if containing is None:
        # 早于链首：前置新段，valid_to 重接链首边界，无需切开旧段
        head = chain[0]
        if as_of >= head["from"]:  # 理论不可达，防御
            raise LateApplyError(
                f"门店 {store_id} as_of={as_of} 无法定位缝入点"
            )
        plan = {"kind": "prepend", "split_id": None, "stitch_to": head["from"]}
        new_seg = {"id": None, "from": as_of, "to": head["from"], "is_current": 0}
        return plan, [new_seg, *chain]

    # 切开包含段：旧段 valid_to 收至 as_of，新迟到段承接原上界
    if containing["to"] is None:
        # 不可达防御：当前段起点=镜 as_of，迟到 as_of 严格更早，不可能落入当前段
        raise LateApplyError(
            f"门店 {store_id} 迟到点 {as_of} 落入当前段 {containing['id']}，"
            "镜/链 invariant 已破坏，拒绝缝链"
        )
    plan = {
        "kind": "split",
        "split_id": containing["id"],
        "stitch_to": containing["to"],
    }
    new_seg = {"id": None, "from": as_of, "to": containing["to"], "is_current": 0}
    containing["to"] = as_of
    idx = chain.index(containing)
    return plan, [*chain[: idx + 1], new_seg, *chain[idx + 1 :]]


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


def apply_late(conn, late_snapshot_ids, batch_id):
    """在调用方事务内执行迟到缝链 + 审计落库；不 commit、不动当前镜。

    返回审计摘要列表。任何区间问题抛 :class:`LateApplyError`，由调用方回滚。
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
        new_hist_id = f"HIST-LATE-{uuid4().hex[:10]}"
        rh = snap["row_hash"]
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
                rh,
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

    # 写后复验：真实落库链不得出现重叠/断裂，否则整笔回滚
    for store_id in {p["store_id"] for p, _ in plans}:
        _assert_chain_intact(_load_chain(conn, store_id), store_id)
    for a in audit_summary:
        conn.execute(
            "UPDATE late_snapshot_audit SET split_hist_id=NULL WHERE id=?",
            (a["audit_id"],),
        )
        a["split_hist_id"] = None
    return audit_summary


def attest_late(conn, store_id=None):
    """只读核证：原镜 as_of、缝入 hist、现镜 as_of 三方互证 + 全链区间复验。"""
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

    def chain_of(sid):
        if sid not in chain_cache:
            chain_cache[sid] = _load_chain(conn, sid)
        return chain_cache[sid]

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

        # 全链区间复验：重叠/断裂/多当前段一律失败
        try:
            _assert_chain_intact(chain_of(a["store_id"]), a["store_id"])
        except LateApplyError as exc:
            errs.append(f"chain_invalid:{exc}")

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
                "current_mirror_as_of": current_mirror_as_of,
                "current_hist_id": current_hist_id,
                "valid": 0 if errs else 1,
                "errors": errs,
                "created_at": a["created_at"],
            }
        )
    return results
