"use client";

import { createContext, useContext, useState, useCallback, type ReactNode } from "react";
import { fetchJSON } from "@/lib/api";

export type TradingMode = "test" | "live";

interface TradingModeContextValue {
  mode: TradingMode;
  brokerName: string;
  setMode: (mode: TradingMode) => void;
  dialogError: string | null;
  setDialogError: (msg: string | null) => void;
  showDialog: boolean;
  setShowDialog: (v: boolean) => void;
}

const TradingModeContext = createContext<TradingModeContextValue | null>(null);

export function TradingModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeInternal] = useState<TradingMode>("test");
  const [brokerName, setBrokerName] = useState("BROKER");
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [showDialog, setShowDialog] = useState(false);

  const setMode = useCallback((newMode: TradingMode) => {
    if (newMode !== "live") { setModeInternal(newMode); return; }
    // Live mode needs a connected live broker configured in Settings → Broker (backend-side).
    fetchJSON<{ mode: string; broker: string; connected: boolean; halted: boolean; halt_reason: string }>("/api/broker/status")
      .then(b => {
        let problem = "";
        if (b.mode === "paper") problem = "NO LIVE BROKER SELECTED\n\nOpen Settings → Broker / Live Execution, enter your Zerodha or INDmoney credentials, test the connection and click GO LIVE.";
        else if (!b.connected) problem = `${b.broker.toUpperCase()} NOT CONNECTED\n\nOpen Settings → Broker / Live Execution and run TEST CONNECTION (Zerodha tokens expire daily).`;
        else if (b.halted) problem = `TRADING HALTED\n\n${b.halt_reason}`;
        if (problem) { setDialogError(problem); setShowDialog(true); return; }
        setBrokerName(b.broker);
        setModeInternal("live");
      })
      .catch(() => {
        setDialogError("BACKEND NOT REACHABLE\n\nStart the backend (./start.sh) and try again.");
        setShowDialog(true);
      });
  }, []);

  return (
    <TradingModeContext.Provider value={{ mode, brokerName, setMode, dialogError, setDialogError, showDialog, setShowDialog }}>
      {children}
    </TradingModeContext.Provider>
  );
}

export function useTradingMode() {
  const ctx = useContext(TradingModeContext);
  if (!ctx) throw new Error("useTradingMode must be used within TradingModeProvider");
  return ctx;
}
