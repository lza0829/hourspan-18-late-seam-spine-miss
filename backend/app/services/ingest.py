import json
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.modules.late_snapshot_apply import LateApplyError, apply_late, classify
from app.seed import now_iso, row_hash


def run_batch(fixture_name: str = "ingest_change_batch.json"):
    """贴源入档批；迟到快照（as_of 严格早于镜 as_of）走 late_snapshot_apply 缝链。

    失败闭：整批一个事务，缝链/审计与贴源写入同生共死；区间重叠即回滚，
    不产生「审计成功但区间重叠」，也不静默改镜或无审计丢弃。
    """
    path = FIXTURES_DIR / fixture_name
    if not path.exists():
        return {"batch_id": None, "accepted": 0, "message": "无夹具"}
    data = json.loads(path.read_text(encoding="utf-8"))
    snaps = data.get("snapshots", [])
    batch_id = f"B-{uuid4().hex[:8]}"
    now = now_iso()
    conn = connect()
    accepted = 0
    skipped = 0
    landed_ids = []
    try:
        for s in snaps:
            store = conn.execute("SELECT id FROM stores WHERE id=?", (s["store_id"],)).fetchone()
            if not store:
                skipped += 1
                continue
            ot, ct = s.get("open_time"), s.get("close_time")
            wm = s.get("weekday_mask", "1111100")
            st = s.get("biz_status", "open")
            rh = row_hash(ot, ct, wm, st)
            snap_id = s.get("id") or f"SNAP-{uuid4().hex[:10]}"
            cur = conn.execute(
                """INSERT OR IGNORE INTO source_snapshots(
                  id, batch_id, store_id, as_of_ts, open_time, close_time,
                  weekday_mask, biz_status, payload_json, row_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap_id,
                    batch_id,
                    s["store_id"],
                    s["as_of_ts"],
                    ot,
                    ct,
                    wm,
                    st,
                    json.dumps(s.get("payload", {}), ensure_ascii=False),
                    rh,
                ),
            )
            if cur.rowcount:
                accepted += 1
                landed_ids.append(snap_id)
            else:
                skipped += 1

        # 入档分流：严格早于当前镜 as_of 的快照进入迟到缝链
        normal, late = classify(conn, landed_ids)
        late_audit = []
        if late:
            # 同事务缝链 + 审计；草稿校验/写后复验失败即抛错，整笔回滚
            late_audit = apply_late(conn, [s["id"] for s in late], batch_id)

        report = {
            "accepted": accepted,
            "skipped": skipped,
            "total": len(snaps),
            "routed_normal": [s["id"] for s in normal],
            "routed_late": [s["id"] for s in late],
            "late_audit": late_audit,
        }
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, now, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"batch_id": batch_id, **report}
    except LateApplyError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
