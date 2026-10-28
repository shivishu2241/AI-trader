"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchJSON, postJSON } from "@/lib/api";

interface BrokerCfg {
  mode: string;
  status: { broker: string; connected: boolean; halted: boolean; daily_pnl: number; open_positions: unknown[] };
  zerodha: { api_key: string; api_secret: string; has_access_token: boolean; redirect_url: string };
  indmoney: {
    access_token: string; base_url: string; auth_scheme: string;
    endpoints: Record<string, string>; default_endpoints: Record<string, string>;
  };
}
type Msg = { type: "ok" | "err"; text: string } | null;

const BROKERS = [
  { id: "paper", label: "PAPER (simulated)", color: "#4da6ff" },
  { id: "zerodha", label: "ZERODHA (Kite Connect)", color: "#e8c300" },
  { id: "indmoney", label: "INDMONEY (INDstocks)", color: "#00e87b" },
];

export default function BrokerSettings() {
  const [cfg, setCfg] = useState<BrokerCfg | null>(null);
  const [tab, setTab] = useState("paper");
  const [z, setZ] = useState({ api_key: "", api_secret: "" });
  const [im, setIm] = useState({ access_token: "", base_url: "", auth_scheme: "auto", endpoints: {} as Record<string, string> });
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);
  const [adv, setAdv] = useState(false);

  const apply = (c: BrokerCfg) => {
    setCfg(c);
    setZ({ api_key: c.zerodha.api_key, api_secret: c.zerodha.api_secret });
    setIm({ access_token: c.indmoney.access_token, base_url: c.indmoney.base_url,
            auth_scheme: c.indmoney.auth_scheme, endpoints: c.indmoney.endpoints });
  };

  const load = useCallback(async () => {
    const c = await fetchJSON<BrokerCfg>("/api/broker/config").catch(() => null);
    if (!c) { setMsg({ type: "err", text: "BACKEND OFFLINE" }); return; }
    apply(c);
    setTab(t => (t === "paper" && c.mode !== "paper" ? c.mode : t));
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("broker") === "zerodha") {
      setTab("zerodha");
      setMsg(q.get("auth") === "ok" ? { type: "ok", text: "ZERODHA LOGIN COMPLETE" } : { type: "err", text: "ZERODHA LOGIN FAILED" });
    }
  }, []);

  const creds = () => (tab === "zerodha" ? { zerodha: z } : tab === "indmoney" ? { indmoney: im } : {});

  const save = async () => {
    setBusy(true); setMsg(null);
    try { apply(await postJSON<BrokerCfg>("/api/broker/config", creds())); setMsg({ type: "ok", text: "SAVED" }); }
    catch { setMsg({ type: "err", text: "SAVE FAILED" }); }
    setBusy(false);
  };

  const test = async () => {
    setBusy(true); setMsg(null);
    try {
      await postJSON("/api/broker/config", creds());
      const r = await postJSON<{ ok: boolean; message: string }>("/api/broker/test", { broker: tab });
      setMsg({ type: r.ok ? "ok" : "err", text: r.message.toUpperCase() });
      await load();
    } catch { setMsg({ type: "err", text: "TEST FAILED" }); }
    setBusy(false);
  };

  const activate = async () => {
    if (tab !== "paper" && !window.confirm(
      `Switch to LIVE trading via ${tab.toUpperCase()}?\n\nReal orders with real money will be placed from now on. ` +
      `Make sure you tested the connection and understand the risk limits.`)) return;
    setBusy(true); setMsg(null);
    try {
      const c = await postJSON<BrokerCfg>("/api/broker/config", { ...creds(), mode: tab, confirm_live: tab !== "paper" });
      apply(c);
      const r = await postJSON<{ connected: boolean }>("/api/broker/connect");
      setMsg(r.connected ? { type: "ok", text: `${tab.toUpperCase()} ACTIVE & CONNECTED` }
                         : { type: "err", text: `${tab.toUpperCase()} SELECTED BUT NOT CONNECTED — run TEST CONNECTION` });
    } catch { setMsg({ type: "err", text: "COULD NOT SWITCH (close open positions first?)" }); }
    setBusy(false);
  };

  const zerodhaLogin = async () => {
    await postJSON("/api/broker/config", creds());
    const r = await fetchJSON<{ login_url?: string; error?: string }>("/api/broker/zerodha/login_url").catch(() => ({ error: "Request failed" }) as { login_url?: string; error?: string });
    if (r.login_url) window.location.href = r.login_url;
    else setMsg({ type: "err", text: (r.error || "NO LOGIN URL").toUpperCase() });
  };

  const input = (label: string, value: string, onChange: (v: string) => void, type = "text") => (
    <label className="block">
      <span className="text-[10px]" style={{ color: "#5a6270" }}>{label}</span>
      <input type={type} value={value} autoComplete="off" onChange={e => onChange(e.target.value)}
        className="w-full mt-1 px-2 py-1.5 text-[11px]"
        style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }} />
    </label>
  );

  const live = cfg && cfg.mode !== "paper";

  return (
    <div className="t-panel p-5 mb-4">
      <div className="flex items-start justify-between mb-1">
        <h2 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: "#c8cdd5" }}>Broker / Live Execution</h2>
        {cfg && (
          <span className="text-[10px] font-bold px-2 py-0.5"
            style={{ background: live ? "#ff3e3e22" : "#4da6ff22", color: live ? "#ff3e3e" : "#4da6ff", border: `1px solid ${live ? "#ff3e3e" : "#4da6ff"}` }}>
            {live ? "LIVE" : "PAPER"} · {cfg.status.broker.toUpperCase()} · {cfg.status.connected ? "CONNECTED" : "DISCONNECTED"}
          </span>
        )}
      </div>
      <p className="text-[10px] mb-4" style={{ color: "#5a6270" }}>
        Choose where orders go. Credentials are stored on the backend and never shown again.
      </p>

      {!cfg ? <div className="text-[11px]" style={{ color: "#3d4450" }}>LOADING...</div> : (
        <>
          <div className="flex flex-wrap gap-[1px] mb-4">
            {BROKERS.map(b => (
              <button key={b.id} onClick={() => { setTab(b.id); setMsg(null); }}
                className={`t-btn ${tab === b.id ? "t-btn-active" : ""}`}>
                {cfg.mode === b.id ? "● " : ""}{b.label}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-3xl">
            {tab === "paper" && <p className="md:col-span-2 text-[11px]" style={{ color: "#5a6270" }}>Simulated fills only. No credentials needed.</p>}

            {tab === "zerodha" && (<>
              {input("API KEY", z.api_key, v => setZ(s => ({ ...s, api_key: v })))}
              {input("API SECRET", z.api_secret, v => setZ(s => ({ ...s, api_secret: v })), "password")}
              <p className="md:col-span-2 text-[10px]" style={{ color: "#5a6270" }}>
                Set this as the Redirect URL in your Kite developer app:{" "}
                <code style={{ color: "#4da6ff" }}>{cfg.zerodha.redirect_url}</code><br />
                Zerodha tokens expire daily (~6am): click LOGIN WITH ZERODHA each trading day.
                Token: {cfg.zerodha.has_access_token ? "stored" : "not yet issued"}.
              </p>
              <div className="md:col-span-2"><button disabled={busy} onClick={zerodhaLogin} className="t-btn" style={{ color: "#e8c300" }}>LOGIN WITH ZERODHA</button></div>
            </>)}

            {tab === "indmoney" && (<>
              {input("ACCESS TOKEN", im.access_token, v => setIm(s => ({ ...s, access_token: v })), "password")}
              {input("BASE URL", im.base_url, v => setIm(s => ({ ...s, base_url: v })))}
              <p className="md:col-span-2 text-[10px]" style={{ color: "#5a6270" }}>
                Generate the token at indstocks.com → FnO → Algos → Access Tokens (a static IP must be whitelisted there).
              </p>
              <p className="md:col-span-2 text-[10px] p-2" style={{ color: "#e8c300", border: "1px solid #e8c30055" }}>
                ⚠ The INDstocks endpoint paths and order payload mapping were written without the official docs.
                Use TEST CONNECTION (read-only), compare with api-docs.indstocks.com, adjust under Advanced, and place a
                1-lot test order before trusting it with real money.
              </p>
              <div className="md:col-span-2">
                <button onClick={() => setAdv(a => !a)} className="t-btn">{adv ? "HIDE" : "SHOW"} ADVANCED: ENDPOINTS</button>
              </div>
              {adv && (<>
                <label className="block">
                  <span className="text-[10px]" style={{ color: "#5a6270" }}>AUTH HEADER</span>
                  <select value={im.auth_scheme} onChange={e => setIm(s => ({ ...s, auth_scheme: e.target.value }))}
                    className="w-full mt-1 px-2 py-1.5 text-[11px]" style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }}>
                    <option value="auto">auto-detect on connect</option>
                    <option value="raw">Authorization: &lt;token&gt;</option>
                    <option value="bearer">Authorization: Bearer &lt;token&gt;</option>
                  </select>
                </label>
                <span />
                {Object.keys(cfg.indmoney.default_endpoints).map(k =>
                  <div key={k}>{input(`PATH: ${k}`, im.endpoints[k] ?? "", v => setIm(s => ({ ...s, endpoints: { ...s.endpoints, [k]: v } })))}</div>)}
              </>)}
            </>)}

            <div className="md:col-span-2 flex flex-wrap items-center gap-2 mt-1">
              {tab !== "paper" && <button disabled={busy} onClick={save} className="t-btn">SAVE</button>}
              {tab !== "paper" && <button disabled={busy} onClick={test} className="t-btn">TEST CONNECTION</button>}
              <button disabled={busy || cfg.mode === tab} onClick={activate} className={tab === "paper" ? "t-btn t-btn-green" : "t-btn t-btn-red"}>
                {cfg.mode === tab ? "ACTIVE" : tab === "paper" ? "USE PAPER" : `GO LIVE WITH ${tab.toUpperCase()}`}
              </button>
              {msg && <span className="text-[11px]" style={{ color: msg.type === "ok" ? "#00e87b" : "#ff3e3e" }}>{msg.text}</span>}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
