"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { API_BASE } from "@/lib/api";
import { Send } from "lucide-react";

interface Msg { role: "user" | "assistant"; content: string }

export default function AiChat() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [meta, setMeta] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs]);

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    const next = [...msgs, { role: "user" as const, content: text }];
    setMsgs(next); setInput(""); setBusy(true); setErr("");
    try {
      const res = await fetch(`${API_BASE}/api/ai/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next }),
      });
      const j = await res.json();
      if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
      setMsgs([...next, { role: "assistant", content: j.text }]);
      setMeta(`${j.provider} · ${j.model}`);
    } catch (e) { setErr(e instanceof Error ? e.message : "Chat failed"); }
    setBusy(false);
  };

  return (
    <div className="t-panel p-5 mb-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: "#00e87b" }}>AI Analyst Chat</h3>
          <p className="text-[10px] mt-0.5" style={{ color: "#3d4450" }}>
            SEES LIVE REGIME, SIGNALS & POSITIONS · ADVISORY ONLY — CANNOT PLACE ORDERS {meta && `· ${meta.toUpperCase()}`}
          </p>
        </div>
        <Link href="/settings" className="t-btn">LLM SETTINGS</Link>
      </div>

      <div className="h-72 overflow-y-auto p-3 mb-2 space-y-2 text-[11px]" style={{ background: "#0b0d11", border: "1px solid #1e222c" }}>
        {msgs.length === 0 && <p style={{ color: "#3d4450" }}>Ask e.g. “Why is the regime sideways?” or “Is my open position at risk?”</p>}
        {msgs.map((m, i) => (
          <div key={i}>
            <span className="font-bold" style={{ color: m.role === "user" ? "#4da6ff" : "#00e87b" }}>{m.role === "user" ? "YOU" : "AI"} › </span>
            <span className="whitespace-pre-wrap" style={{ color: "#c8cdd5" }}>{m.content}</span>
          </div>
        ))}
        {busy && <p style={{ color: "#5a6270" }}>thinking…</p>}
        <div ref={end} />
      </div>

      {err && <p className="text-[11px] mb-2" style={{ color: "#ff3e3e" }}>{err} — configure a provider in Settings → AI / LLM Provider.</p>}
      <div className="flex gap-2">
        <input value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === "Enter" && send()}
          placeholder="Ask about the market, signals or positions…" className="flex-1 px-2 py-1.5 text-[11px]"
          style={{ background: "#111318", border: "1px solid #252a33", color: "#c8cdd5" }} />
        <button onClick={send} disabled={busy} className="t-btn t-btn-green flex items-center gap-1.5"><Send className="w-3 h-3" /> SEND</button>
      </div>
    </div>
  );
}
