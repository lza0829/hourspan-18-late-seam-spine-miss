"""late_snapshot_apply 验收测试（隔离临时库，不触碰 dev 数据）。

验收剧本：
  1. 先把当前镜推到较新 as_of（11-15 变更批 → 检测 → Type2）；
  2. 再灌更早变更：历史链可查缝段、审计有痕、镜仍最新；
  3. 区间重叠夹具整笔失败：无快照、无审计、无历史行、无批号落库；
  4. 只读核证接口三方互证（原镜 as_of / 缝入 hist / 现镜 as_of）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="hourspan-accept-")
os.environ["DATA_DIR"] = _TMP
os.environ["FIXTURES_DIR"] = str(BACKEND.parent / "fixtures")

from app.db import connect  # noqa: E402
from app.modules.late_snapshot_apply import LateApplyError  # noqa: E402
from app.seed import init_db  # noqa: E402
from app.services import detect, ingest, scd  # noqa: E402
from app.modules.late_snapshot_apply import attest_late  # noqa: E402


class LateSnapshotAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.conn_factory = connect

    def _conn(self):
        return connect()

    def test_01_push_mirror_to_newer_as_of(self):
        """前置：变更批把 EAST-01 / SOUTH-01 的镜推到 11-15。"""
        rep = ingest.run_batch("ingest_change_batch.json")
        self.assertIn("B-", rep["batch_id"])
        det = detect.run_detect(rep["batch_id"])
        applied = scd.apply_type2(det["detect_run_id"])
        self.assertTrue(applied["ok"])
        with self._conn() as c:
            m = c.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
            ).fetchone()
            self.assertEqual(m["as_of_ts"], "2026-11-15T00:00:00Z")
            n = c.execute(
                "SELECT COUNT(*) AS c FROM dim_store_hours_hist "
                "WHERE store_id='ST-EAST-01' AND is_current=1"
            ).fetchone()["c"]
            self.assertEqual(n, 1)

    def test_02_late_batch_stitches_and_keeps_mirror_fresh(self):
        """迟到快照：切开旧段/前置缝入，审计落库，镜 as_of 不被拖回。"""
        before = {}
        with self._conn() as c:
            for sid in ("ST-EAST-01", "ST-WEST-01"):
                before[sid] = c.execute(
                    "SELECT as_of_ts, hist_id FROM dim_store_hours_current WHERE store_id=?",
                    (sid,),
                ).fetchone()
            self.assertEqual(before["ST-EAST-01"]["as_of_ts"], "2026-11-15T00:00:00Z")

        rep = ingest.run_batch("late_snapshot_batch.json")
        self.assertEqual(len(rep["routed_late"]), 2, rep)
        self.assertEqual(len(rep["routed_normal"]), 1, rep)
        self.assertEqual(len(rep["late_audit"]), 2)

        with self._conn() as c:
            # 镜仍指时间轴最新段，as_of 未被拖回
            east = c.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
            ).fetchone()
            self.assertEqual(east["as_of_ts"], "2026-11-15T00:00:00Z")
            self.assertEqual(east["as_of_ts"], before["ST-EAST-01"]["as_of_ts"])
            self.assertEqual(east["hist_id"], before["ST-EAST-01"]["hist_id"])

            west = c.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id='ST-WEST-01'"
            ).fetchone()
            self.assertEqual(west["as_of_ts"], before["ST-WEST-01"]["as_of_ts"])

            # EAST-01 链：11-01(切至11-10) → 11-10 迟到段(至11-15) → 11-15 当前段
            chain = c.execute(
                """SELECT valid_from, valid_to, is_current, source_snapshot_id
                   FROM dim_store_hours_hist
                   WHERE store_id='ST-EAST-01' ORDER BY valid_from, id"""
            ).fetchall()
            self.assertEqual([r["valid_from"] for r in chain], [
                "2026-11-01T00:00:00Z",
                "2026-11-10T00:00:00Z",
                "2026-11-15T00:00:00Z",
            ])
            self.assertEqual(chain[0]["valid_to"], "2026-11-10T00:00:00Z")
            self.assertEqual(chain[1]["valid_to"], "2026-11-15T00:00:00Z")
            self.assertEqual(chain[1]["is_current"], 0)
            self.assertEqual(chain[1]["source_snapshot_id"], "SNAP-LATE-EAST-01")
            self.assertIsNone(chain[2]["valid_to"])
            self.assertEqual(chain[2]["is_current"], 1)

            # WEST-01：早于链首前置，valid_to 重接原链首 11-01
            wchain = c.execute(
                """SELECT valid_from, valid_to, is_current
                   FROM dim_store_hours_hist
                   WHERE store_id='ST-WEST-01' ORDER BY valid_from, id"""
            ).fetchall()
            self.assertEqual(wchain[0]["valid_from"], "2026-10-25T00:00:00Z")
            self.assertEqual(wchain[0]["valid_to"], "2026-11-01T00:00:00Z")
            self.assertEqual(wchain[0]["is_current"], 0)
            self.assertEqual(wchain[1]["valid_from"], "2026-11-01T00:00:00Z")
            self.assertEqual(wchain[-1]["is_current"], 1)

            # 审计痕迹：快照 id / 原镜 as_of / 缝入 hist_id 齐备
            audit = c.execute(
                "SELECT * FROM late_snapshot_audit WHERE snapshot_id=?",
                ("SNAP-LATE-EAST-01",),
            ).fetchone()
            self.assertEqual(audit["mirror_as_of_before"], "2026-11-15T00:00:00Z")
            self.assertTrue(audit["stitched_hist_id"].startswith("HIST-LATE-"))
            self.assertIsNotNone(audit["split_hist_id"])
            stitched = c.execute(
                "SELECT id FROM dim_store_hours_hist WHERE source_snapshot_id=?",
                ("SNAP-LATE-EAST-01",),
            ).fetchone()
            self.assertEqual(audit["stitched_hist_id"], stitched["id"])

            # 正常分流快照仅贴源落库，未被缝进历史
            self.assertIsNone(c.execute(
                "SELECT 1 FROM dim_store_hours_hist WHERE source_snapshot_id='SNAP-NORMAL-NORTH-01'"
            ).fetchone())

    def test_03_attest_readonly_triangulation(self):
        """只读核证：原镜 as_of、缝入 hist、现镜 as_of 全通过。"""
        with self._conn() as c:
            res = attest_late(c, "ST-EAST-01")
            self.assertEqual(len(res), 1)
            r = res[0]
            self.assertEqual(r["valid"], 1, r)
            self.assertEqual(r["mirror_as_of_before"], "2026-11-15T00:00:00Z")
            self.assertEqual(r["current_mirror_as_of"], "2026-11-15T00:00:00Z")
            self.assertEqual(r["current_hist_id"], r["current_hist_id"])
            self.assertTrue(r["stitched_hist_id"].startswith("HIST-LATE-"))
            self.assertEqual(r["errors"], [])

            all_res = attest_late(c)
            self.assertEqual(len(all_res), 2)
            self.assertTrue(all(x["valid"] for x in all_res))

    def test_04_overlap_fixture_fails_whole_batch(self):
        """区间重叠夹具：整笔失败，无任何痕迹落库。"""
        with self._conn() as c:
            snap_before = c.execute("SELECT COUNT(*) AS c FROM source_snapshots").fetchone()["c"]
            hist_before = c.execute("SELECT COUNT(*) AS c FROM dim_store_hours_hist").fetchone()["c"]
            audit_before = c.execute("SELECT COUNT(*) AS c FROM late_snapshot_audit").fetchone()["c"]
            batch_before = c.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"]

        with self.assertRaises(LateApplyError):
            ingest.run_batch("late_snapshot_overlap.json")

        with self._conn() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS c FROM source_snapshots").fetchone()["c"],
                snap_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS c FROM dim_store_hours_hist").fetchone()["c"],
                hist_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS c FROM late_snapshot_audit").fetchone()["c"],
                audit_before,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"],
                batch_before,
            )
            # 重叠夹具的快照 id 完全不存在
            self.assertIsNone(c.execute(
                "SELECT 1 FROM source_snapshots WHERE id='SNAP-OVERLAP-EAST-01-A'"
            ).fetchone())

    def test_05_chain_still_intact_and_mirror_fresh_after_failure(self):
        """失败闭后既有链与镜保持验收 02 的状态，核证仍全通过。"""
        with self._conn() as c:
            chain = c.execute(
                "SELECT valid_from, valid_to, is_current FROM dim_store_hours_hist "
                "WHERE store_id='ST-EAST-01' ORDER BY valid_from, id"
            ).fetchall()
            self.assertEqual(len(chain), 3)
            m = c.execute(
                "SELECT as_of_ts FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
            ).fetchone()
            self.assertEqual(m["as_of_ts"], "2026-11-15T00:00:00Z")
            self.assertTrue(all(r["valid"] for r in attest_late(c)))

    def test_06_type2_path_refuses_to_drag_mirror_back(self):
        """镜写入层守卫：对迟到批跑检测再应用 Type2 必须被拒，镜不动。"""
        with self._conn() as c:
            batch_id = c.execute(
                "SELECT batch_id FROM source_snapshots WHERE id='SNAP-LATE-EAST-01'"
            ).fetchone()["batch_id"]
            before = c.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
            ).fetchone()
            hist_before = c.execute(
                "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id='ST-EAST-01'"
            ).fetchone()["c"]

        det = detect.run_detect(batch_id)
        with self.assertRaises(LateApplyError):
            scd.apply_type2(det["detect_run_id"])

        with self._conn() as c:
            after = c.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
            ).fetchone()
            self.assertEqual(after["as_of_ts"], before["as_of_ts"])
            self.assertEqual(after["hist_id"], before["hist_id"])
            hist_after = c.execute(
                "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id='ST-EAST-01'"
            ).fetchone()["c"]
            self.assertEqual(hist_after, hist_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
