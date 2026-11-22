from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import STUBS
from app.modules.late_snapshot_apply import LateApplyError, attest_late
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    try:
        return ingest.run_batch(name)
    except LateApplyError as exc:
        raise HTTPException(409, f"迟到缝链失败闭，整笔回滚：{exc}") from exc


@router.get("/late/audit")
def late_audit(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM late_snapshot_audit WHERE store_id=? ORDER BY id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM late_snapshot_audit ORDER BY store_id, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/late/attest")
def late_attest(store_id: str | None = None):
    """迟到缝链核证只读接口：原镜 as_of、缝入 hist、现镜 as_of 三方互证。"""
    conn = connect()
    try:
        results = attest_late(conn, store_id)
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    run_id = body.get("detect_run_id")
    if not run_id:
        raise HTTPException(400, "需要 detect_run_id")
    try:
        return scd.apply_type2(run_id)
    except LateApplyError as exc:
        raise HTTPException(409, f"迟到快照禁止盖写当前镜：{exc}") from exc


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS}

# --- 迟到缝点（衔接边）只读接口：架构甲 = 以缝点记录导航为准 ---
from app.modules import late_seam_spine as seam_spine


@router.get("/late/seam-spine")
def late_seam_spine(store_id: str | None = None):
    """缝点记录列表：store_id / late_as_of / 前段 / 本段 / 后段 hist + 对齐判定。"""
    conn = connect()
    try:
        return seam_spine.spines_of(conn, store_id)
    finally:
        conn.close()


@router.get("/late/seam-attest")
def late_seam_attest(store_id: str | None = None):
    """缝点核证（只读）：缝成功必有点；点 as_of/前后 hist 与实链、审计三方对齐。"""
    conn = connect()
    try:
        return seam_spine.attest_seams(conn, store_id)
    finally:
        conn.close()


@router.get("/late/seam-lookup")
def late_seam_lookup(store_id: str, as_of: str):
    """跨缝点查（只读）：落在迟到缝入段时经缝点记录导航，缺记录即点查失败。"""
    conn = connect()
    try:
        return seam_spine.lookup_as_of(conn, store_id, as_of)
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "store_id" in out and "biz_key" not in out:
        out["biz_key"] = out.get("store_id")
    if "hist_id" in out and "segment_ref" not in out:
        out["segment_ref"] = out.get("hist_id")
    if "row_hash" in out and "content_digest" not in out:
        out["content_digest"] = out.get("row_hash")
    if "aligned" not in out:
        out["aligned"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True, "scope": "hourspan"}
    if extra:
        data.update(extra)
    return data

