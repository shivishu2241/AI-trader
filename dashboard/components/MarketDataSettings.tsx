"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchJSON, postJSON } from "@/lib/api";
import OfflineNotice from "@/components/OfflineNotice";

interface DataCfg {
  source: string;
  sources: Record<string, string>;
  truedata: { user: string; password: string };
  has_option_prices: boolean;
  status: { candles: number; first: string | null; last: string | null; db_error: string | null; last_sync: string | null; feed_running: boolean };
  restart_needed?: boolean;
}
type Msg = { type: "ok" | "err"; text: string } | null;

export default function MarketDataSettings() {
  const [cfg, setCfg] = useState<DataCfg | null>(null);
  const [offline, setOffline] = useState(false);
  const [td, setTd] = useState({ user: "", password: "" });
  const [days, setDays] = useState(7);
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);

  const apply = (c: DataCfg) => { setCfg(c); setTd(c.truedata); };

  const load = useCallback(async () => {
    const c = await fetchJSON<DataCfg>("/api/data/config").catch(() => null);
    if (!c) { setOffline(true); return; }
    setOffline(false); apply(c);
  }, []);
  useEffect(() => { load(); }, [load]);

  const choose = async (source: string) => {
    setBusy(true); setMsg(null);
    try { apply(await postJSON<DataCfg>("/api/data/config", { source })); }
    catch { setMsg({ type: "err", text: "COULD NOT SWITCH SOURCE" }); }
    setBusy(false);
  };

  const saveTd = async () => {
    setBusy(true); setMsg(null);
    try {
      const c = await postJSON<DataCfg>("/api/data/config", { source: "truedata", truedata: td });
      apply(c);
      setMsg({ type: "ok", text: "SAVED — RESTART ./start.sh TO APPLY THE NEW CREDENTIALS" });
    } catch { setMsg({ type: "err", text: "SAVE FAILED" }); }
    setBusy(false);
  };

  const sync = async () => {
    setBusy(true); setMsg({ type: "ok", text: `DOWNLOADING ${days} DAY(S)…` });
    try {
      const r = await postJSON<{ ok: boolean; fetched: number; last: string | null; status: DataCfg["status"] }>("/api/data/sync", { days });
      setCfg(c => (c ? { ...c, status: r.status } : c));
      setMsg({ type: "ok", text: `LOADED ${r.fetched.toLocaleString()} CANDLES${r.last ? ` — LATEST ${r.last}` : ""}` });
    } catch (e) {
      setMsg({ type: "err", text: (e instanceof Error ? e.message : "SYNC FAILED").toUpperCase() });
    }
    setBusy(false);
  };

  const st = cfg?.status;
  const free = cfg?.source === "yahoo";

  return (
    <div className="t-panel p-5 mb-4">
      <h2 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: "#c8cdd5" }}>Market Data Source</h2>
      <p className="text-[10px] mb-4" style={{ color: "#5a6270" }}>
        Where charts, scanner and backtests get their prices. Use one source per database — do not mix them.
      </p>

      {!cfg ? (offline ? <OfflineNotice onRetry={load} /> : <div className="text-[11px]" style={{ color: "#3d4450" }}>LOADING...</div>) : (
        <>
          <div className="flex flex-wrap gap-[1px] mb-4">
            {Object.entries(cfg.sources).map(([id, label]) => (
              <button key={id} disabled={busy} onClick={() => choose(id)} className={`t-btn ${cfg.source === id ? "t-btn-active" : ""}`}>
                {cfg.source === id ? "● " : ""}{label}
              </button>
            ))}
          </div>

          <p className="text-[11px] mb-3" style={{ color: "#5a6270" }}>
            DATABASE: <span style={{ color: "#c8cdd5" }}>{st?.db_error ? "ERROR" : `${st?.candles.toLocaleString()} NIFTY candles`}</span>
            {st?.last && <> · LATEST <span style={{ color: "#c8cdd5" }}>{st.last}</span></>}
            {free && <> · LIVE FEED <span style={{ color: st?.feed_running ? "#00e87b" : "#5a6270" }}>{st?.feed_running ? "RUNNING" : "IDLE (MARKET HOURS ONLY)"}</span></>}
          </p>
          {st?.db_error && <p className="text-[11px] mb-3" style={{ color: "#ff3e3e" }}>{st.db_error}</p>}

          {free ? (
            <div className="max-w-3xl">
              <p className="text-[10px] p-2 mb-3" style={{ color: "#e8c300", border: "1px solid #e8c30055" }}>
                ⚠ Free source = NIFTY 50 <b>index</b> candles from Yahoo Finance (unofficial, may be delayed). No option prices, ticks or futures:
                charts, regime and index signals work; tick backtests and option-premium features do not, and <b>live broker orders are disabled</b>.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <select value={days} onChange={e => setDays(Number(e.target.value))} className="px-2 py-1.5 text-[11px]"
                  style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }}>
                  <option value={1}>last 1 day</option><option value={7}>last 7 days</option>
                  <option value={14}>last 14 days</option><option value={30}>last 30 days (max)</option>
                </select>
                <button disabled={busy} onClick={sync} className="t-btn t-btn-green">DOWNLOAD / SYNC CANDLES</button>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-3xl">
              <label className="block">
                <span className="text-[10px]" style={{ color: "#5a6270" }}>TRUEDATA API USER</span>
                <input value={td.user} autoComplete="off" onChange={e => setTd(s => ({ ...s, user: e.target.value }))}
                  className="w-full mt-1 px-2 py-1.5 text-[11px]" style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }} />
              </label>
              <label className="block">
                <span className="text-[10px]" style={{ color: "#5a6270" }}>TRUEDATA API PASSWORD</span>
                <input type="password" value={td.password} autoComplete="off" onChange={e => setTd(s => ({ ...s, password: e.target.value }))}
                  className="w-full mt-1 px-2 py-1.5 text-[11px]" style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }} />
              </label>
              <div className="md:col-span-2"><button disabled={busy} onClick={saveTd} className="t-btn t-btn-green">SAVE CREDENTIALS</button></div>
            </div>
          )}
          {msg && <p className="text-[11px] mt-3" style={{ color: msg.type === "ok" ? "#00e87b" : "#ff3e3e" }}>{msg.text}</p>}
        </>
      )}
    </div>
  );
}
