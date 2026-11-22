"""late_seam_spine · 迟到缝点（衔接边）导航与核证

架构选定【甲】: 以缝点记录导航为准。  ``late_seam_points`` 是跨缝 as-of 点查的唯一导航结构：
  某个 as_of 落在迟到缝入段（stitched hist）时，必须经缝点记录定位；
  缝点记录缺失或与实链分叉 → 点查失败（不做「裸扫 valid 区间」兜底）。

两种可行架构在全仓只贯彻【甲】。乙（hist 区间扫描为准，缝点仅留痕）不采用，
故此模块的点查在删去缝点行后必然失败——这正是与乙可区分的验收口径。

只读接口：
  - :func:`spines_of`    —— 列出缝点及前后邻接实链，标 linked/spine_ready
  - :func:`attest_seams` —— 缝点核证：无点缝入 / as_of 分叉 / 前后分叉
  - :func:`lookup_as_of` —— 跨缝点查：经缝点记录导航
"""

from app.modules.late_snapshot_apply import _INF, _load_chain


def _chain_index(chain):
    return {seg["id"]: i for i, seg in enumerate(chain)}


def _seam_row(row, chain, index):
    """把一条缝点记录展开为含邻接实链与对齐判定的视图。"""
    i = index.get(row["this_hist_id"])
    if i is None:
        return {
            "seam_id": row["id"],
            "store_id": row["store_id"],
            "late_as_of": row["late_as_of_ts"],
            "prev_hist_id": row["prev_hist_id"],
            "this_hist_id": row["this_hist_id"],
            "next_hist_id": row["next_hist_id"],
            "this_valid_from": None,
            "this_valid_to": None,
            "spine_ready": False,
            "errors": ["this_hist_missing"],
            "created_at": row["created_at"],
        }
    this = chain[i]
    prev_seg = chain[i - 1] if i > 0 else None
    next_seg = chain[i + 1] if i + 1 < len(chain) else None
    expected_prev = prev_seg["id"] if prev_seg else None
    expected_next = next_seg["id"] if next_seg else None
    errs = []
    if this["is_current"] != 0:
        errs.append("late_row_is_current")
    if this["from"] != row["late_as_of_ts"]:
        errs.append("as_of_valid_from_diverged")
    if row["prev_hist_id"] != expected_prev:
        errs.append("prev_hist_diverged")
    if row["next_hist_id"] != expected_next:
        errs.append("next_hist_diverged")
    if prev_seg is not None and prev_seg["to"] != this["from"]:
        errs.append("prev_boundary_broken")
    if next_seg is not None and this["to"] != next_seg["from"]:
        errs.append("next_boundary_broken")
    return {
        "seam_id": row["id"],
        "store_id": row["store_id"],
        "late_as_of": row["late_as_of_ts"],
        "prev_hist_id": row["prev_hist_id"],
        "this_hist_id": row["this_hist_id"],
        "next_hist_id": row["next_hist_id"],
        "this_valid_from": this["from"],
        "this_valid_to": this["to"],
        "spine_ready": not errs,
        "errors": errs,
        "created_at": row["created_at"],
    }


def _seam_rows(conn, store_id=None):
    if store_id:
        return conn.execute(
            "SELECT * FROM late_seam_points WHERE store_id=? ORDER BY late_as_of_ts, id",
            (store_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM late_seam_points ORDER BY store_id, late_as_of_ts, id"
    ).fetchall()


def spines_of(conn, store_id: str | None = None) -> dict:
    """缝点（衔接边）列表：每条含前段/本段/后段 hist_id 与对齐判定。"""
    store_ids = [store_id] if store_id else [
        r["store_id"]
        for r in conn.execute("SELECT DISTINCT store_id FROM late_seam_points ORDER BY store_id").fetchall()
    ]
    rows = []
    for sid in store_ids:
        chain = _load_chain(conn, sid)
        index = _chain_index(chain)
        for r in _seam_rows(conn, sid):
            rows.append(_seam_row(r, chain, index))
    linked = sum(1 for r in rows if r["spine_ready"])
    return {"spines": rows, "linked": linked, "total": len(rows)}


def _store_files(conn, store_id):
    """门店全部迟到审计 + 缝点，返回 (audits, seams_by_stitched)。"""
    audits = conn.execute(
        "SELECT * FROM late_snapshot_audit WHERE store_id=? ORDER BY id", (store_id,)
    ).fetchall()
    seams = {r["this_hist_id"]: r for r in _seam_rows(conn, store_id)}
    return audits, seams


def attest_seams(conn, store_id: str | None = None) -> dict:
    """缝点核证（只读）：缝成功必有点，点必与实链/审计对齐（架构甲口径）。"""
    stores = [store_id] if store_id else [
        r["store_id"]
        for r in conn.execute(
            "SELECT DISTINCT store_id FROM late_snapshot_audit "
            "UNION SELECT DISTINCT store_id FROM late_seam_points ORDER BY store_id"
        ).fetchall()
    ]

    results = []
    for sid in stores:
        chain = _load_chain(conn, sid)
        index = _chain_index(chain)
        audits, seams = _store_files(conn, sid)
        errs = []

        # 缝入必有缝点：每条审计的 stitched_hist_id 须有对应缝点
        for a in audits:
            seam = seams.get(a["stitched_hist_id"])
            if not seam:
                errs.append(f"seam_point_missing:{a['stitched_hist_id']}")
                continue
            if seam["late_as_of_ts"] != a["late_as_of_ts"]:
                errs.append(f"seam_as_of_mismatch:{seam['id']}")
            if seam["prev_hist_id"] != a["split_hist_id"]:
                errs.append(f"seam_prev_vs_audit_mismatch:{seam['id']}")

        # 缝点自身须与实缝区间一致
        spine_rows = []
        for s in _seam_rows(conn, sid):
            view = _seam_row(s, chain, index)
            spine_rows.append(view)
            for e in view["errors"]:
                errs.append(f"{e}:{view['seam_id']}")
            if view["prev_hist_id"] is not None and view["prev_hist_id"] not in index:
                errs.append(f"prev_hist_missing:{view['seam_id']}")
            if view["next_hist_id"] is not None and view["next_hist_id"] not in index:
                errs.append(f"next_hist_missing:{view['seam_id']}")

        results.append(
            {
                "store_id": sid,
                "seams": spine_rows,
                "audit_count": len(audits),
                "seam_count": len(spine_rows),
                "linked": sum(1 for v in spine_rows if v["spine_ready"]),
                "valid": 0 if errs else 1,
                "errors": errs,
            }
        )
    return {
        "checked": len(results),
        "failed": sum(1 for r in results if not r["valid"]),
        "results": results,
    }


def lookup_as_of(conn, store_id: str, as_of: str) -> dict:
    """跨缝点查（架构甲）：经缝点记录导航定位 as_of 所属历史段。

    - as_of 落在普通段：直接返回该段（无需缝点）。
    - as_of 落在迟到缝入段：必须存在缝点记录，且记录与实链对齐；
      缺记录 / 记录分叉 → found=False（点查失败），不裸扫区间兜底。
    """
    chain = _load_chain(conn, store_id)
    target = None
    for seg in chain:
        end = seg["to"] if seg["to"] is not None else _INF
        if seg["from"] <= as_of < end:
            target = seg
            break
    if target is None:
        return {
            "found": False,
            "store_id": store_id,
            "as_of": as_of,
            "reason": "no_segment_covers_as_of",
        }

    audit = conn.execute(
        "SELECT id FROM late_snapshot_audit WHERE stitched_hist_id=?",
        (target["id"],),
    ).fetchone()
    seam = conn.execute(
        "SELECT * FROM late_seam_points WHERE this_hist_id=?",
        (target["id"],),
    ).fetchone()

    is_late_segment = audit is not None or seam is not None
    if is_late_segment:
        # 跨缝段：必须经缝点记录导航
        if seam is None:
            return {
                "found": False,
                "store_id": store_id,
                "as_of": as_of,
                "reason": "seam_point_missing",
                "hist_id": target["id"],
            }
        view = _seam_row(seam, chain, _chain_index(chain))
        if not view["spine_ready"]:
            return {
                "found": False,
                "store_id": store_id,
                "as_of": as_of,
                "reason": "seam_point_diverged",
                "hist_id": target["id"],
                "errors": view["errors"],
            }
        return {
            "found": True,
            "store_id": store_id,
            "as_of": as_of,
            "hist_id": target["id"],
            "via": "seam_point",
            "seam_id": view["seam_id"],
            "prev_hist_id": view["prev_hist_id"],
            "next_hist_id": view["next_hist_id"],
            "valid_from": view["this_valid_from"],
            "valid_to": view["this_valid_to"],
        }

    return {
        "found": True,
        "store_id": store_id,
        "as_of": as_of,
        "hist_id": target["id"],
        "via": "hist_scan",
        "seam_id": None,
        "prev_hist_id": None,
        "next_hist_id": None,
        "valid_from": target["from"],
        "valid_to": target["to"],
    }
