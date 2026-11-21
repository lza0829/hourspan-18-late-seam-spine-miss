import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Late() {
  const [audits, setAudits] = useState([]);
  const [attest, setAttest] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    Promise.all([api("/api/late/audit"), api("/api/late/attest")])
      .then(([a, t]) => { setAudits(a); setAttest(t); })
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const run = async (fixture) => {
    setErr("");
    setMsg("");
    try {
      const r = await api("/api/ingest/run", {
        method: "POST",
        body: JSON.stringify({ fixture }),
      });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      // 失败闭：409 整笔回滚，错误须可观测
      setErr(String(e.message || e));
      await reload();
    }
  };

  return (
    <>
      <div data-plant="18-late-seam-spine-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>迟到快照缝链</h1>
          <p>
            as_of 严格早于当前镜即判迟到：禁止盖写镜，按 as_of 切开旧段、缝入迟到段并重接
            valid 边界；镜仍只指时间轴最新段。落库形态=独立迟到审计表；失败闭=同事务草稿校验。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={() => run("late_snapshot_batch.json")}>灌迟到变更夹具</button>
          <button className="secondary" onClick={() => run("late_snapshot_overlap.json")}>
            灌区间重叠夹具（须整笔失败）
          </button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="panel">
        <h2>迟到缝链核证（只读：原镜 as_of · 缝入 hist · 现镜 as_of）</h2>
        {attest && (
          <p className="muted">
            共 {attest.checked} 条，失败 {attest.failed} 条
          </p>
        )}
        <table>
          <thead>
            <tr>
              <th>审计</th><th>门店</th><th>快照</th><th>原镜 as_of</th>
              <th>迟到 as_of</th><th>切开 hist</th><th>缝入 hist</th>
              <th>现镜 as_of</th><th>现镜 hist</th><th>核证</th><th>错误</th>
            </tr>
          </thead>
          <tbody>
            {(attest?.results || []).map((r) => (
              <tr key={r.audit_id}>
                <td className="mono">{r.audit_id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.snapshot_id}</td>
                <td>{r.mirror_as_of_before}</td>
                <td>{r.late_as_of_ts}</td>
                <td className="mono">{r.split_hist_id || "—（前置缝入）"}</td>
                <td className="mono">{r.stitched_hist_id}</td>
                <td>{r.current_mirror_as_of}</td>
                <td className="mono">{r.current_hist_id}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.errors?.length ? r.errors.join(", ") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>late_snapshot_audit 原始审计</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>批号</th><th>门店</th><th>快照</th>
              <th>迟到 as_of</th><th>原镜 as_of</th><th>切开 hist</th>
              <th>缝入 hist</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {audits.map((a) => (
              <tr key={a.id}>
                <td className="mono">{a.id}</td>
                <td className="mono">{a.batch_id}</td>
                <td>{a.store_id}</td>
                <td className="mono">{a.snapshot_id}</td>
                <td>{a.late_as_of_ts}</td>
                <td>{a.mirror_as_of_before}</td>
                <td className="mono">{a.split_hist_id || "—"}</td>
                <td className="mono">{a.stitched_hist_id}</td>
                <td className="mono">{a.detail_json}</td>
                <td>{a.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
