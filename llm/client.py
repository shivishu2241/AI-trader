"""
Provider-agnostic LLM client
────────────────────────────
Talks to any OpenAI-compatible endpoint (OpenAI, Gemini, OpenRouter,
OmniRoute, Ollama, LM Studio, vLLM, any custom gateway) plus Anthropic's
native Messages API. Uses plain `requests` — no extra dependencies.

Auth per provider is either an API key or, for OpenRouter, OAuth (PKCE).
Credentials live in the GUI-managed store (utils.user_config, section "llm").
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Optional

import requests

from utils import user_config
from utils.logger import get_logger

logger = get_logger("llm")

TIMEOUT = 60

PROVIDERS: dict[str, dict] = {
    "omniroute": {
        "label": "OmniRoute (self-hosted gateway)",
        "kind": "openai",
        "base_url": "http://localhost:20128/v1",
        "default_model": "auto",
        "needs_key": True,
        "help": "Run OmniRoute locally, add providers there, and create a gateway key under Endpoints.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter/auto",
        "needs_key": True,
        "oauth": True,
        "help": "Paste an API key, or use 'Sign in with OpenRouter' (OAuth).",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-5-5",
        "needs_key": True,
        "help": "API key from console.anthropic.com.",
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "needs_key": True,
        "help": "API key from platform.openai.com.",
    },
    "gemini": {
        "label": "Google Gemini",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "needs_key": True,
        "help": "API key from aistudio.google.com (OpenAI-compatible endpoint).",
    },
    "ollama": {
        "label": "Ollama (local)",
        "kind": "openai",
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3.1",
        "needs_key": False,
        "help": "No key needed. Run `ollama serve` on this machine.",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "kind": "openai",
        "base_url": "",
        "default_model": "",
        "needs_key": False,
        "help": "Any server exposing /chat/completions (LM Studio, vLLM, LiteLLM, ...).",
    },
}


class LLMError(Exception):
    pass


# ── Config ────────────────────────────────────────────────────────────

def get_config() -> dict:
    return user_config.get("llm")


def public_config() -> dict:
    """Config safe to send to the browser, merged with provider defaults."""
    cfg = user_config.masked("llm")
    saved = cfg.get("providers", {})
    out = {}
    for pid, meta in PROVIDERS.items():
        s = saved.get(pid, {})
        out[pid] = {
            "label": meta["label"],
            "help": meta["help"],
            "oauth": bool(meta.get("oauth")),
            "needs_key": meta["needs_key"],
            "base_url": s.get("base_url") or meta["base_url"],
            "model": s.get("model") or meta["default_model"],
            "api_key": s.get("api_key", ""),          # masked or ""
            "configured": bool(user_config.get("llm").get("providers", {}).get(pid, {}).get("api_key"))
                          or not meta["needs_key"],
        }
    return {"active": cfg.get("active", ""), "providers": out}


def _resolve(provider: Optional[str] = None) -> tuple[str, dict, dict]:
    cfg = get_config()
    pid = provider or cfg.get("active") or os.getenv("LLM_PROVIDER", "")
    if pid not in PROVIDERS:
        raise LLMError("No LLM provider selected. Configure one in Settings → AI / LLM.")
    meta = PROVIDERS[pid]
    saved = cfg.get("providers", {}).get(pid, {})
    env_key = os.getenv(f"{pid.upper()}_API_KEY", "")
    conn = {
        "base_url": (saved.get("base_url") or meta["base_url"]).rstrip("/"),
        "model": saved.get("model") or meta["default_model"],
        "api_key": saved.get("api_key") or env_key,
    }
    if not conn["base_url"]:
        raise LLMError(f"{meta['label']}: base URL is not set.")
    if meta["needs_key"] and not conn["api_key"]:
        raise LLMError(f"{meta['label']}: API key is not set.")
    return pid, meta, conn


def _headers(meta: dict, conn: dict) -> dict:
    if meta["kind"] == "anthropic":
        return {"x-api-key": conn["api_key"], "anthropic-version": "2023-06-01",
                "content-type": "application/json"}
    h = {"content-type": "application/json"}
    if conn["api_key"]:
        h["Authorization"] = f"Bearer {conn['api_key']}"
    return h


def _raise(r: requests.Response):
    try:
        msg = r.json().get("error", {})
        msg = msg.get("message", msg) if isinstance(msg, dict) else msg
    except Exception:
        msg = r.text[:300]
    raise LLMError(f"HTTP {r.status_code}: {msg}")


# ── Operations ────────────────────────────────────────────────────────

def chat(messages: list[dict], system: str = "", provider: Optional[str] = None,
         max_tokens: int = 1024, temperature: float = 0.3) -> dict:
    """messages: [{role: user|assistant, content: str}]. Returns {text, provider, model}."""
    pid, meta, conn = _resolve(provider)
    try:
        if meta["kind"] == "anthropic":
            body = {"model": conn["model"], "max_tokens": max_tokens, "messages": messages,
                    "temperature": temperature}
            if system:
                body["system"] = system
            r = requests.post(f"{conn['base_url']}/v1/messages", json=body,
                              headers=_headers(meta, conn), timeout=TIMEOUT)
            if not r.ok:
                _raise(r)
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
        else:
            msgs = ([{"role": "system", "content": system}] if system else []) + messages
            body = {"model": conn["model"], "messages": msgs,
                    "max_tokens": max_tokens, "temperature": temperature}
            r = requests.post(f"{conn['base_url']}/chat/completions", json=body,
                              headers=_headers(meta, conn), timeout=TIMEOUT)
            if not r.ok:
                _raise(r)
            text = r.json()["choices"][0]["message"].get("content") or ""
    except requests.RequestException as e:
        raise LLMError(f"Cannot reach {meta['label']} at {conn['base_url']}: {e}") from e
    return {"text": text, "provider": pid, "model": conn["model"]}


def list_models(provider: Optional[str] = None) -> list[str]:
    pid, meta, conn = _resolve(provider)
    try:
        path = "/v1/models" if meta["kind"] == "anthropic" else "/models"
        r = requests.get(f"{conn['base_url']}{path}", headers=_headers(meta, conn), timeout=20)
    except requests.RequestException as e:
        raise LLMError(f"Cannot reach {conn['base_url']}: {e}") from e
    if not r.ok:
        _raise(r)
    return sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))


def test(provider: Optional[str] = None) -> dict:
    res = chat([{"role": "user", "content": "Reply with the single word: ok"}],
               provider=provider, max_tokens=16, temperature=0)
    return {"ok": True, "provider": res["provider"], "model": res["model"], "reply": res["text"][:80]}


# ── OpenRouter OAuth (PKCE) ───────────────────────────────────────────

_pkce: dict[str, str] = {}   # single pending flow (local single-user app)


def openrouter_auth_url(callback_url: str) -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _pkce["verifier"] = verifier
    from urllib.parse import urlencode
    return "https://openrouter.ai/auth?" + urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def openrouter_exchange(code: str) -> None:
    verifier = _pkce.pop("verifier", "")
    if not verifier:
        raise LLMError("No OpenRouter sign-in in progress. Start again from Settings.")
    r = requests.post("https://openrouter.ai/api/v1/auth/keys", json={
        "code": code, "code_verifier": verifier, "code_challenge_method": "S256",
    }, timeout=20)
    if not r.ok:
        _raise(r)
    key = r.json().get("key")
    if not key:
        raise LLMError("OpenRouter did not return a key.")
    user_config.update("llm", {"providers": {"openrouter": {"api_key": key}}})
