"use client";

import { API_BASE } from "@/lib/api";

export default function OfflineNotice({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="text-[11px] p-3" style={{ border: "1px solid #ff3e3e55", color: "#c8cdd5" }}>
      <p className="font-bold mb-1" style={{ color: "#ff3e3e" }}>BACKEND NOT REACHABLE</p>
      <p style={{ color: "#5a6270" }}>
        The dashboard could not reach <code style={{ color: "#4da6ff" }}>{API_BASE}</code>. Start everything with{" "}
        <code style={{ color: "#4da6ff" }}>./start.sh</code> and check <code style={{ color: "#4da6ff" }}>logs/backend.log</code> for errors.
      </p>
      <button onClick={onRetry} className="t-btn mt-2">RETRY</button>
    </div>
  );
}
