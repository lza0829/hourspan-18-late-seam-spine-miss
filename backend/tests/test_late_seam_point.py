"""迟到缝点（衔接边）验收：架构甲 = 以缝点记录导航为准。

验收剧本（全新隔离库，单独跑一遍种子→推镜→迟到缝入）：
  1. 缝入一笔后：缝点记录与实缝区间一致（前/本/后段 hist），核证通过；
  2. 跨缝 as-of 点查必须经缝点记录命中；
  3. 删去缝点行：点查失败（架构甲口径），缝点核证标缺失；
     同时对照「裸扫 hist 区间」仍能命中——证明两种架构结论可区分；
  4. 半截失败（写缝点前即抛）整笔回滚：无新 hist、无审计、无缝点。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="hourspan-seam-")
os.environ["DATA_DIR"] = _TMP
os.environ["FIXTURES_DIR"] = str(BACKEND.parent / "fixtures")

from app.db import connect  # noqa: E402
from app.modules import late_seam_spine as spine  # noqa: E402
from app.modules.late_snapshot_apply import (  # noqa: E402
    LateApplyError,
    apply_late,
    attest_late,
)
from app.seed import init_db  # noqa: E402
from app.services import detect, ingest, scd  # noqa: E402

LATE_AS_OF = "2026-11-10T00:00:00Z"


class LateSeamPointAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 独立临时库：多测试模块同进程发现时须各自绑定 DB_PATH，用完复原
        from app import db as _db

        cls._saved = (_db.DATA_DIR, _db.DB_PATH)
        tmp = tempfile.mkdtemp(prefix="hourspan-seam-")
        _db.DATA_DIR = Path(tmp)
        _db.DB_PATH = Path(tmp) / "hourspan.db"
        init_db()
        # 把 EAST-01 的镜推到 11-15，方可缝入 11-10 迟到段
        rep = ingest.run_batch("ingest_change_batch.json")
        det = detect.run_detect(rep["batch_id"])
        scd.apply_type2(det["detect_run_id"])

    @classmethod
    def tearDownClass(cls):
        from app import db as _db

        _db.DATA_DIR, _db.DB_PATH = cls._saved

    def _conn(self):
        return connect()

    def test_01_stitch_writes_seam_point_aligned_to_chain(self):
        """缝入一笔：审计、缝点、实链三方对齐，核证通过。"""
        rep = ingest.run_batch("late_snapshot_batch.json")
        self.assertEqual(len(rep["late_audit"]), 2)

        with self._conn() as c:
            seam = c.execute(
                "SELECT * FROM late_seam_points WHERE store_id='ST-EAST-01'"
            ).fetchone()
            self.assertIsNotNone(seam, "缝成功必须有缝点记录")
            audit = c.execute(
                "SELECT * FROM late_snapshot_audit WHERE stitched_hist_id=?",
                (seam["this_hist_id"],),
            ).fetchone()
            self.assertIsNotNone(audit)

            # 字段齐备且与实缝区间一致
            self.assertEqual(seam["store_id"], "ST-EAST-01")
            self.assertEqual(seam["late_as_of_ts"], LATE_AS_OF)
            this_row = c.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (seam["this_hist_id"],)
            ).fetchone()
            self.assertEqual(this_row["valid_from"], seam["late_as_of_ts"])
            self.assertEqual(this_row["is_current"], 0)

            # 前段 hist = 被切开旧段；后段 hist = 原后续段
            prev_row = c.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (seam["prev_hist_id"],)
            ).fetchone()
            self.assertEqual(prev_row["valid_to"], seam["late_as_of_ts"])
            next_row = c.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (seam["next_hist_id"],)
            ).fetchone()
            self.assertEqual(this_row["valid_to"], next_row["valid_from"])

            # 前置缝入（WEST-01）：prev 可空，next 接原链首
            wseam = c.execute(
                "SELECT * FROM late_seam_points WHERE store_id='ST-WEST-01'"
            ).fetchone()
            self.assertIsNone(wseam["prev_hist_id"])
            self.assertIsNotNone(wseam["next_hist_id"])

            # 核证全通过
            for r in attest_late(c):
                self.assertEqual(r["valid"], 1, r)
                self.assertIsNotNone(r["seam_id"], r)

            att = spine.attest_seams(c)
            self.assertEqual(att["failed"], 0, att)
            self.assertEqual(att["checked"], 2)

    def test_02_cross_seam_lookup_goes_through_seam_record(self):
        """跨缝 as-of 点查：经缝点记录命中（via=seam_point）。"""
        with self._conn() as c:
            hit = spine.lookup_as_of(c, "ST-EAST-01", LATE_AS_OF)
            self.assertTrue(hit["found"], hit)
            self.assertEqual(hit["via"], "seam_point")
            self.assertIsNotNone(hit["seam_id"])

            # 非缝段（链首普通段）走 hist 区间即可
            plain = spine.lookup_as_of(c, "ST-EAST-01", "2026-11-01T00:00:00Z")
            self.assertTrue(plain["found"], plain)
            self.assertEqual(plain["via"], "hist_scan")

    def test_03_delete_seam_point_distinguishes_architectures(self):
        """删缝点行：架构甲点查失败；裸扫 hist 仍命中——两架构可区分。"""
        with self._conn() as c:
            seam = c.execute(
                "SELECT * FROM late_seam_points WHERE store_id='ST-EAST-01'"
            ).fetchone()
            hist_id = seam["this_hist_id"]

            # 删去缝点行（模拟「缝成功而无点」或记录被抹）
            c.execute("DELETE FROM late_seam_points WHERE id=?", (seam["id"],))
            c.commit()

            # 架构甲：缺记录 → 点查失败
            miss = spine.lookup_as_of(c, "ST-EAST-01", LATE_AS_OF)
            self.assertFalse(miss["found"], miss)
            self.assertEqual(miss["reason"], "seam_point_missing")
            self.assertEqual(miss["hist_id"], hist_id)

            # 架构乙口径对照：裸扫 hist 区间仍能命中该段
            row = c.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id='ST-EAST-01' "
                "AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
                (LATE_AS_OF, LATE_AS_OF),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["id"], hist_id)

            # 缝点核证必须标缺失（不得静默）
            att = spine.attest_seams(c, "ST-EAST-01")
            self.assertEqual(att["failed"], 1, att)
            joined = ";".join(att["results"][0]["errors"])
            self.assertIn("seam_point_missing", joined)

            # 逐条三方核证也须标出缺失
            r = [x for x in attest_late(c, "ST-EAST-01") if x["stitched_hist_id"] == hist_id][0]
            self.assertEqual(r["valid"], 0)
            self.assertIn("seam_point_missing", r["errors"])

            # 复原缝点，供后续用例
            c.execute(
                """INSERT INTO late_seam_points(
                  id, store_id, late_as_of_ts, prev_hist_id, this_hist_id,
                  next_hist_id, batch_id, audit_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    seam["id"], seam["store_id"], seam["late_as_of_ts"],
                    seam["prev_hist_id"], seam["this_hist_id"], seam["next_hist_id"],
                    seam["batch_id"], seam["audit_id"], seam["created_at"],
                ),
            )
            c.commit()
            self.assertTrue(spine.lookup_as_of(c, "ST-EAST-01", LATE_AS_OF)["found"])

    def test_04_halfway_failure_rolls_back_whole_stitch(self):
        """写缝点阶段抛错（区间对撞）→ 整笔回滚：无新 hist/审计/缝点。"""
        with self._conn() as c:
            hist_before = c.execute("SELECT COUNT(*) AS n FROM dim_store_hours_hist").fetchone()["n"]
            audit_before = c.execute("SELECT COUNT(*) AS n FROM late_snapshot_audit").fetchone()["n"]
            seam_before = c.execute("SELECT COUNT(*) AS n FROM late_seam_points").fetchone()["n"]
            # 造一条迟到快照，as_of 与既有段起点重合，必触重叠失败
            c.execute(
                """INSERT INTO source_snapshots(
                  id, batch_id, store_id, as_of_ts, open_time, close_time,
                  weekday_mask, biz_status, payload_json, row_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "SNAP-SEAM-COLLIDE", "B-TEST", "ST-EAST-01", "2026-11-15T00:00:00Z",
                    "09:00", "22:00", "1111100", "open", "{}", "deadbeef",
                ),
            )
            c.commit()

        conn = self._conn()
        try:
            with self.assertRaises(LateApplyError):
                apply_late(conn, ["SNAP-SEAM-COLLIDE"], "B-TEST")
            conn.rollback()
        finally:
            conn.close()

        with self._conn() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS n FROM dim_store_hours_hist").fetchone()["n"],
                hist_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS n FROM late_snapshot_audit").fetchone()["n"],
                audit_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS n FROM late_seam_points").fetchone()["n"],
                seam_before,
            )
            # 清理测试插入的贴源快照
            c.execute("DELETE FROM source_snapshots WHERE id='SNAP-SEAM-COLLIDE'")
            c.commit()

    def test_05_midwrite_failure_rolls_back_half_done_stitch(self):
        """写后复验失败（已写 hist+审计+缝点）→ 同事务整笔回滚，无半截残留。"""
        from unittest import mock

        from app.modules import late_snapshot_apply as lsa

        with self._conn() as c:
            hist_before = c.execute("SELECT COUNT(*) AS n FROM dim_store_hours_hist").fetchone()["n"]
            audit_before = c.execute("SELECT COUNT(*) AS n FROM late_snapshot_audit").fetchone()["n"]
            seam_before = c.execute("SELECT COUNT(*) AS n FROM late_seam_points").fetchone()["n"]
            c.execute(
                """INSERT INTO source_snapshots(
                  id, batch_id, store_id, as_of_ts, open_time, close_time,
                  weekday_mask, biz_status, payload_json, row_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "SNAP-SEAM-MIDWRITE", "B-TEST", "ST-EAST-01", "2026-11-03T00:00:00Z",
                    "08:00", "20:00", "1111100", "open", "{}", "cafebabe",
                ),
            )
            c.commit()

        def _boom(_conn, _store_id):
            raise LateApplyError("模拟写后缝点核证失败")

        conn = self._conn()
        try:
            with mock.patch.object(lsa, "_seam_point_errors", side_effect=_boom):
                with self.assertRaises(LateApplyError):
                    apply_late(conn, ["SNAP-SEAM-MIDWRITE"], "B-TEST")
                # 抛错时事务内确有半截写入（证明写已发生、须靠回滚清除）
                self.assertGreater(
                    conn.execute("SELECT COUNT(*) AS n FROM dim_store_hours_hist").fetchone()["n"],
                    hist_before,
                )
                conn.rollback()
        finally:
            conn.close()

        with self._conn() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS n FROM dim_store_hours_hist").fetchone()["n"],
                hist_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS n FROM late_snapshot_audit").fetchone()["n"],
                audit_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS n FROM late_seam_points").fetchone()["n"],
                seam_before,
            )
            c.execute("DELETE FROM source_snapshots WHERE id='SNAP-SEAM-MIDWRITE'")
            c.commit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
