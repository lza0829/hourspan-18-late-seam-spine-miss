import React, { useEffect, useState } from "react";
import { api, lateSeamAttest, lateSeamLookup, lateSeamSpine } from "../api.js";

export default function Late() {
  const [audits, setAudits] = useState([]);
  const [attest, setAttest] = useState(null);
  const [spine, setSpine] = useState(null);
  const [seamAttest, setSeamAttest] = useState(null);
  const [lookup, setLookup] = useState(null);
  const [lookupStore, setLookupStore] = useState("ST-EAST-01");
  const [lookupAsOf, setLookupAsOf] = useState("2026-11-10T00:00:00Z");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    Promise.all([
      api("/api/late/audit"),
      api("/api/late/attest"),
      lateSeamSpine(),
      lateSeamAttest(),
    ])
      .then(([a, t, s, sa]) => {
        setAudits(a);
        setAttest(t);
        setSpine(s);
        setSeamAttest(sa);
      })
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

  const doLookup = async () => {
    setErr("");
    try {
      setLookup(await lateSeamLookup(lookupStore.trim(), lookupAsOf.trim()));
    } catch (e) {
      setErr(String(e.message || e));
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
            valid 边界；镜仍只指时间轴最新段。落库形态=独立迟到审计表 + 缝点记录；
            失败闭=同事务草稿校验，缝链/审计/缝点同生共死。
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
        <h2>缝点记录（衔接边）</h2>
        <p className="muted">
          架构甲：以缝点记录导航为准。跨缝 as-of 点查必须经缝点记录定位，缺记录则点查失败。
        </p>
        {spine && (
          <p className="muted">共 {spine.total ?? 0} 条，对齐 {spine.linked ?? 0} 条</p>
        )}
        <table>
          <thead>
            <tr>
              <th>缝点</th><th>门店</th><th>late_as_of</th>
              <th>前段 hist</th><th>本段 hist</th><th>后段 hist</th>
              <th>对齐</th><th>错误</th>
            </tr>
          </thead>
          <tbody>
            {(spine?.spines || []).map((s) => (
              <tr key={s.seam_id}>
                <td className="mono">{s.seam_id}</td>
                <td>{s.store_id}</td>
                <td>{s.late_as_of}</td>
                <td className="mono">{s.prev_hist_id || "—（前置缝入）"}</td>
                <td className="mono">{s.this_hist_id}</td>
                <td className="mono">{s.next_hist_id || "—"}</td>
                <td>
                  <span className={`badge ${s.spine_ready ? "ok" : "bad"}`}>
                    {s.spine_ready ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{s.errors?.length ? s.errors.join(", ") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>缝点核证（只读：缝成功必有点 · 点与实链/审计三方对齐）</h2>
        {seamAttest && (
          <p className="muted">
            共 {seamAttest.checked} 店，失败 {seamAttest.failed} 店
          </p>
        )}
        <table>
          <thead>
            <tr><th>门店</th><th>审计条</th><th>缝点条</th><th>对齐</th><th>核证</th><th>错误</th></tr>
          </thead>
          <tbody>
            {(seamAttest?.results || []).map((r) => (
              <tr key={r.store_id}>
                <td>{r.store_id}</td>
                <td>{r.audit_count}</td>
                <td>{r.seam_count}</td>
                <td>{r.linked}</td>
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
        <h2>跨缝 as-of 点查（选架构甲：须经缝点记录导航）</h2>
        <div className="actions">
          <input
            value={lookupStore}
            onChange={(e) => setLookupStore(e.target.value)}
            placeholder="门店，如 ST-EAST-01"
            style={{ minWidth: 180, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <input
            value={lookupAsOf}
            onChange={(e) => setLookupAsOf(e.target.value)}
            placeholder="as_of，如 2026-11-10T00:00:00Z"
            style={{ minWidth: 240, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={doLookup}>点查</button>
        </div>
        {lookup && <pre className="msg">{JSON.stringify(lookup, null, 2)}</pre>}
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
              <th>缝点</th><th>前段</th><th>后段</th>
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
                <td className="mono">{r.seam_id || "—（缺缝点）"}</td>
                <td className="mono">{r.prev_hist_id || "—"}</td>
                <td className="mono">{r.next_hist_id || "—"}</td>
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
