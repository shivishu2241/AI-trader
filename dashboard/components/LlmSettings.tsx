"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchJSON, postJSON } from "@/lib/api";
import OfflineNotice from "@/components/OfflineNotice";

interface Provider {
  label: string; help: string; oauth: boolean; needs_key: boolean;
  base_url: string; model: string; api_key: string; configured: boolean;
}
interface LlmConfig { active: string; providers: Record<string, Provider> }
type Msg = { type: "ok" | "err"; text: string } | null;

export default function LlmSettings() {
  const [cfg, setCfg] = useState<LlmConfig | null>(null);
  const [sel, setSel] = useState<string>("");
  const [form, setForm] = useState({ api_key: "", base_url: "", model: "" });
  const [models, setModels] = useState<string[]>([]);
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);

  const load = useCallback(async () => {
    const c = await fetchJSON<LlmConfig>("/api/llm/config").catch(() => null);
    if (!c) { setOffline(true); return; }
    setOffline(false);
    setCfg(c);
    setSel(s => s || c.active || "omniroute");
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const p = cfg?.providers[sel];
    if (p) { setForm({ api_key: p.api_key, base_url: p.base_url, model: p.model }); setModels([]); }
  }, [cfg, sel]);

  // Result of the OpenRouter OAuth redirect
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("llm") === "openrouter") {
      setMsg(q.get("auth") === "ok"
        ? { type: "ok", text: "SIGNED IN WITH OPENROUTER" }
        : { type: "err", text: "OPENROUTER SIGN-IN FAILED" });
      setSel("openrouter");
    }
  }, []);

  const p = cfg?.providers[sel];

  const save = async (activate: boolean) => {
    setBusy(true); setMsg(null);
    try {
      const body: Record<string, unknown> = { providers: { [sel]: form } };
      if (activate) body.active = sel;
      setCfg(await postJSON<LlmConfig>("/api/llm/config", body));
      setMsg({ type: "ok", text: activate ? `${sel.toUpperCase()} SAVED & ACTIVE` : "SAVED" });
    } catch { setMsg({ type: "err", text: "SAVE FAILED" }); }
    setBusy(false);
  };

  const test = async () => {
    setBusy(true); setMsg(null);
    try {
      await postJSON("/api/llm/config", { providers: { [sel]: form } });
      const r = await postJSON<{ ok?: boolean; error?: string; model?: string; reply?: string }>("/api/llm/test", { provider: sel });
      setMsg(r.ok ? { type: "ok", text: `OK — ${r.model} replied "${r.reply}"` } : { type: "err", text: r.error || "TEST FAILED" });
    } catch { setMsg({ type: "err", text: "TEST FAILED" }); }
    setBusy(false);
  };

  const loadModels = async () => {
    setBusy(true);
    try {
      await postJSON("/api/llm/config", { providers: { [sel]: form } });
      const r = await fetchJSON<{ models: string[]; error?: string }>(`/api/llm/models?provider=${sel}`);
      setModels(r.models);
      if (r.error) setMsg({ type: "err", text: r.error });
    } catch { setMsg({ type: "err", text: "COULD NOT LIST MODELS" }); }
    setBusy(false);
  };

  const oauth = async () => {
    const r = await fetchJSON<{ url: string }>("/api/llm/oauth/openrouter/start");
    window.location.href = r.url;
  };

  const field = (label: string, key: "api_key" | "base_url" | "model", type = "text", list?: string) => (
    <label className="block">
      <span className="text-[10px]" style={{ color: "#5a6270" }}>{label}</span>
      <input
        type={type} list={list} value={form[key]} autoComplete="off"
        onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
        className="w-full mt-1 px-2 py-1.5 text-[11px]"
        style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }}
      />
    </label>
  );

  return (
    <div className="t-panel p-5 mb-4">
      <h2 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: "#c8cdd5" }}>AI / LLM Provider</h2>
      <p className="text-[10px] mb-4" style={{ color: "#5a6270" }}>
        Powers the AI chat. Keys are stored on the backend only and never shown again.
        Active: <span style={{ color: "#00e87b" }}>{cfg?.active ? cfg.providers[cfg.active]?.label : "none"}</span>
      </p>

      {!cfg ? (offline ? <OfflineNotice onRetry={load} /> : <div className="text-[11px]" style={{ color: "#3d4450" }}>LOADING...</div>) : (
        <>
          <div className="flex flex-wrap gap-[1px] mb-4">
            {Object.entries(cfg.providers).map(([id, pr]) => (
              <button key={id} onClick={() => setSel(id)}
                className={`t-btn ${sel === id ? "t-btn-active" : ""}`}>
                {cfg.active === id ? "● " : ""}{pr.label}
              </button>
            ))}
          </div>

          {p && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-3xl">
              <p className="md:col-span-2 text-[10px]" style={{ color: "#5a6270" }}>{p.help}</p>
              {field("BASE URL", "base_url")}
              {field("MODEL", "model", "text", "llm-models")}
              <datalist id="llm-models">{models.map(m => <option key={m} value={m} />)}</datalist>
              {(p.needs_key || sel === "custom") && field(p.needs_key ? "API KEY" : "API KEY (optional)", "api_key", "password")}
              <div className="md:col-span-2 flex flex-wrap items-center gap-2">
                <button disabled={busy} onClick={() => save(true)} className="t-btn t-btn-green">SAVE & USE</button>
                <button disabled={busy} onClick={() => save(false)} className="t-btn">SAVE</button>
                <button disabled={busy} onClick={test} className="t-btn">TEST</button>
                <button disabled={busy} onClick={loadModels} className="t-btn">LOAD MODELS</button>
                {p.oauth && <button disabled={busy} onClick={oauth} className="t-btn" style={{ color: "#4da6ff" }}>SIGN IN WITH OPENROUTER (OAUTH)</button>}
                {msg && <span className="text-[11px]" style={{ color: msg.type === "ok" ? "#00e87b" : "#ff3e3e" }}>{msg.text}</span>}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
