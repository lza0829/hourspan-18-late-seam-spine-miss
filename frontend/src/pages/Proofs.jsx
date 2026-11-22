import React, { useEffect, useState } from "react";
import { api, lateSeamAttest } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [companion, setCompanion] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));
    Promise.resolve(lateSeamAttest()).then(setCompanion).catch(() => setCompanion(null));
  };

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>当前镜 hist_id / row_hash 必须与 is_current 历史行一致；失败须可观测。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      
      <div className="panel">
        <h2>迟到缝点核证</h2>
        {companion?.results?.length ? (
          <table>
            <thead>
              <tr><th>门店</th><th>审计条</th><th>缝点条</th><th>对齐</th><th>核证</th><th>错误</th></tr>
            </thead>
            <tbody>
              {companion.results.map((r) => (
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
        ) : (
          <p className="muted">（暂无迟到缝点）</p>
        )}
      </div>
      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
