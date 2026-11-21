STATUS = "partial"


def spines_of(conn, store_id: str | None = None) -> dict:
    if store_id:
        audits = conn.execute(
            "SELECT * FROM late_snapshot_audit WHERE store_id=? ORDER BY id",
            (store_id,),
        ).fetchall()
    else:
        audits = conn.execute("SELECT * FROM late_snapshot_audit ORDER BY id").fetchall()
    rows = []
    for a in audits:
        rows.append(
            {
                "audit_id": a["id"],
                "store_id": a["store_id"],
                "late_as_of": a["late_as_of_ts"],
                "prev_hist_id": None,
                "this_hist_id": a["stitched_hist_id"],
                "next_hist_id": None,
                "spine_ready": False,
            }
        )
    return {"spines": rows, "linked": 0}
