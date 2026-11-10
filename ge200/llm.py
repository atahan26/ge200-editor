"""Provider layer for the tone generator: one small interface, several LLM back ends.

Back ends (all optional, none needs more than the standard library except Anthropic's own SDK):

  anthropic   Claude via the official SDK (``pip install anthropic``), ``ANTHROPIC_API_KEY``
  openai      OpenAI Chat Completions, ``OPENAI_API_KEY``
  gemini      Google Gemini ``generateContent``, ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)
  openrouter  OpenRouter (many vendors behind one key), ``OPENROUTER_API_KEY``
  local       any OpenAI-compatible server you run yourself (Ollama, LM Studio, vLLM, llama.cpp ...);
              set ``GE200_LLM_BASE_URL`` (Ollama's default is ``http://localhost:11434/v1``)

Settings come from the environment (a git-ignored ``.env`` is loaded too, see ``ge200.env``):

  GE200_LLM_PROVIDER   default provider id (otherwise the first one that has credentials)
  GE200_LLM_MODEL      default model for the non-Anthropic providers (or GE200_<ID>_MODEL, e.g. GE200_OPENAI_MODEL)
  GE200_LLM_BASE_URL   base URL of the ``local`` provider
  GE200_LLM_API_KEY    optional key for the ``local`` provider
  GE200_LLM_MAX_TOKENS answer length limit for the OpenAI-style and Gemini providers (default 8000)

Safety: API keys and base URLs are read from the environment only. The web page can pick a provider *id* and a
model *name*, never a URL or a key, so a page (or a script reaching the local server) cannot make the editor send
a key somewhere else. Model names are not guessed here: there is no default for the non-Anthropic providers, name
one in the environment or in the editor.

Privacy: the prompt (your description, the base preset, a sample of your presets, optionally model-name tables and
your rig text) goes to whichever provider you choose. Use ``local`` to keep it on your machine.

Only the Anthropic provider has been exercised against the real service by the author. The others are tested
against a local fake HTTP server (request shapes follow the vendors' public documentation) and may need small
adjustments: please report what a real service rejects.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")


class LlmError(RuntimeError):
    """A provider call failed (the message is safe to show: it never contains keys)."""


@dataclass
class Completion:
    text: str
    refused: bool = False
    truncated: bool = False


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def _max_tokens() -> int:
    try:
        return max(1000, min(64000, int(os.environ.get("GE200_LLM_MAX_TOKENS", "8000"))))
    except ValueError:
        return 8000


def check_model(model: str) -> str:
    if not isinstance(model, str) or not MODEL_RE.match(model):
        raise LlmError("invalid model name")
    return model


def schema_instruction(schema: dict) -> str:
    return ("Reply with ONE JSON object only (no markdown, no commentary) that follows this JSON schema:\n"
            + json.dumps(schema, separators=(",", ":")))


# ------------------------------------------------------------------ providers

class Provider:
    id = ""
    label = ""
    key_env: List[str] = []
    needs_key = True

    def default_model(self) -> str:
        return _env(f"GE200_{self.id.upper()}_MODEL", "GE200_LLM_MODEL")

    def info(self) -> dict:
        key = _env(*self.key_env) if self.key_env else ""
        reason = ""
        if self.needs_key and not key:
            reason = "set " + " or ".join(self.key_env)
        elif not self.default_model():
            reason = "pick a model name (the editor remembers it, or set GE200_LLM_MODEL)"
        return {"id": self.id, "label": self.label, "available": self.needs_key is False or bool(key),
                "reason": reason, "default_model": self.default_model(), "needs_key": self.needs_key}

    def complete(self, system: str, user: str, schema: dict, model: str, effort: str = "high") -> Completion:
        raise NotImplementedError


class AnthropicProvider(Provider):
    id, label, key_env = "anthropic", "Claude (Anthropic)", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]

    def __init__(self, client=None, fallbacks: bool = True) -> None:
        self._client = client
        self.fallbacks = fallbacks

    def default_model(self) -> str:
        return ANTHROPIC_DEFAULT_MODEL

    def info(self) -> dict:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return {"id": self.id, "label": self.label, "available": False, "default_model": ANTHROPIC_DEFAULT_MODEL,
                    "needs_key": True, "reason": "the 'anthropic' package is not installed (pip install anthropic)"}
        if self._client is not None:
            return {"id": self.id, "label": self.label, "available": True, "reason": "",
                    "default_model": ANTHROPIC_DEFAULT_MODEL, "needs_key": True}
        has = bool(_env(*self.key_env))
        return {"id": self.id, "label": self.label, "available": True, "default_model": ANTHROPIC_DEFAULT_MODEL,
                "needs_key": True, "reason": "" if has else "no ANTHROPIC_API_KEY (an 'ant auth login' profile also works)"}

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LlmError("the 'anthropic' package is not installed (pip install anthropic)") from e
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, system, user, schema, model, effort="high") -> Completion:
        client = self.client()
        kwargs = dict(
            model=model or ANTHROPIC_DEFAULT_MODEL,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self.fallbacks:    # server-side fallback if a safety classifier declines (unlikely for this task)
                response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                       **kwargs)
            else:
                response = client.messages.create(**kwargs)
        except LlmError:
            raise
        except Exception as e:      # SDK / network errors: translate, never leak keys
            raise LlmError(self._explain(e)) from e
        text = next((b.text for b in response.content if b.type == "text"), "")
        return Completion(text or "", refused=response.stop_reason == "refusal",
                          truncated=response.stop_reason == "max_tokens")

    @staticmethod
    def _explain(e: Exception) -> str:
        try:
            import anthropic
        except ImportError:
            return type(e).__name__
        if isinstance(e, anthropic.AuthenticationError):
            return "authentication failed: set ANTHROPIC_API_KEY (or run 'ant auth login')"
        if isinstance(e, anthropic.PermissionDeniedError):
            return "the API key is not allowed to use this model"
        if isinstance(e, anthropic.RateLimitError):
            return "rate limited by the API, try again in a minute"
        if isinstance(e, anthropic.APIConnectionError):
            return "could not reach the Anthropic API (network problem)"
        if isinstance(e, anthropic.APIStatusError):
            return f"API error {e.status_code}: {getattr(e, 'message', type(e).__name__)}"
        return f"{type(e).__name__}: {e}"


def post_json(url: str, headers: Dict[str, str], body: dict, timeout: float = 180.0) -> dict:
    """POST JSON, return the decoded JSON answer. Errors become LlmError without echoing headers."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise LlmError("the provider URL must start with http:// or https://")
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
            err = data.get("error", data) if isinstance(data, dict) else {}
            detail = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            pass
        detail = (detail or "").replace("\n", " ")[:240]
        if e.code in (401, 403):
            raise LlmError(f"authentication failed ({e.code}); check the API key in the environment") from None
        if e.code == 429:
            raise LlmError("rate limited by the API, try again in a minute") from None
        err_obj = LlmError(f"API error {e.code}" + (f": {detail}" if detail else ""))
        err_obj.status = e.code      # type: ignore[attr-defined]
        raise err_obj from None
    except urllib.error.URLError as e:
        raise LlmError(f"could not reach the provider ({getattr(e, 'reason', e)})") from None
    except (TimeoutError, OSError) as e:
        raise LlmError(f"network problem ({type(e).__name__})") from None
    except json.JSONDecodeError:
        raise LlmError("the provider did not answer with JSON") from None


class OpenAIStyleProvider(Provider):
    """OpenAI Chat Completions and everything that speaks the same dialect."""

    def __init__(self, id: str, label: str, base_url: str, key_env: List[str], needs_key: bool = True,
                 token_param: str = "max_tokens") -> None:
        self.id, self.label, self.key_env, self.needs_key = id, label, key_env, needs_key
        self._base, self.token_param = base_url, token_param
        self._schema_ok = True      # flips to False once a server rejects json_schema

    def base_url(self) -> str:
        if self.id == "local":
            return _env("GE200_LLM_BASE_URL")
        return self._base

    def info(self) -> dict:
        d = super().info()
        if self.id == "local":
            if not self.base_url():
                d.update(available=False, reason="set GE200_LLM_BASE_URL (Ollama: http://localhost:11434/v1)")
        return d

    def complete(self, system, user, schema, model, effort="high") -> Completion:
        base = self.base_url().rstrip("/")
        if not base:
            raise LlmError("no base URL configured for this provider")
        key = _env(*self.key_env) if self.key_env else ""
        if self.needs_key and not key:
            raise LlmError("no API key: set " + " or ".join(self.key_env))
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {
            "model": check_model(model),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            self.token_param: _max_tokens(),
        }
        if self._schema_ok:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "ge200_preset_edit", "strict": True, "schema": schema}}
        else:
            body["response_format"] = {"type": "json_object"}
            body["messages"][0]["content"] = system + "\n\n" + schema_instruction(schema)
        try:
            data = post_json(base + "/chat/completions", headers, body)
        except LlmError as e:
            if getattr(e, "status", None) in (400, 422) and self._schema_ok:
                self._schema_ok = False        # server does not take json_schema: retry in plain JSON mode
                return self.complete(system, user, schema, model, effort)
            raise
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):             # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return Completion(text, refused=bool(msg.get("refusal")) or choice.get("finish_reason") == "content_filter",
                          truncated=choice.get("finish_reason") == "length")


class GeminiProvider(Provider):
    id, label, key_env = "gemini", "Gemini (Google)", ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def base_url(self) -> str:
        return self.BASE

    def complete(self, system, user, schema, model, effort="high") -> Completion:
        key = _env(*self.key_env)
        if not key:
            raise LlmError("no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)")
        model = check_model(model)
        if "/" in model or ":" in model:
            raise LlmError("invalid Gemini model name")
        body = {
            "systemInstruction": {"parts": [{"text": system + "\n\n" + schema_instruction(schema)}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": _max_tokens()},
        }
        data = post_json(f"{self.base_url()}/models/{model}:generateContent", {"x-goog-api-key": key}, body)
        if (data.get("promptFeedback") or {}).get("blockReason"):
            return Completion("", refused=True)
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        reason = cand.get("finishReason", "")
        return Completion(text, refused=reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"),
                          truncated=reason == "MAX_TOKENS")


# ------------------------------------------------------------------ registry

def providers() -> Dict[str, Provider]:
    """Fresh provider objects (cheap); reads the environment at call time."""
    from .env import load_env
    load_env()
    items: List[Provider] = [
        AnthropicProvider(),
        OpenAIStyleProvider("openai", "OpenAI", "https://api.openai.com/v1", ["OPENAI_API_KEY"],
                            token_param="max_completion_tokens"),
        GeminiProvider(),
        OpenAIStyleProvider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", ["OPENROUTER_API_KEY"]),
        OpenAIStyleProvider("local", "Local / OpenAI-compatible server", "", ["GE200_LLM_API_KEY"], needs_key=False),
    ]
    return {p.id: p for p in items}


_cache: Dict[str, Provider] = {}


def get_provider(provider_id: Optional[str] = None) -> Provider:
    """Provider object for an id (None = the configured default). Objects are reused so per-server quirks stick."""
    reg = providers()
    pid = (provider_id or default_id(reg)).strip().lower()
    if pid not in reg:
        raise LlmError(f"unknown provider '{pid}' (choose one of: {', '.join(reg)})")
    if pid not in _cache:
        _cache[pid] = reg[pid]
    return _cache[pid]


def default_id(reg: Optional[Dict[str, Provider]] = None) -> str:
    reg = reg or providers()
    want = _env("GE200_LLM_PROVIDER").lower()
    if want in reg:
        return want
    for pid, p in reg.items():
        if p.info()["available"] and not p.info()["reason"]:
            return pid
    return "anthropic"


def overview() -> dict:
    """What the web editor shows: every provider with availability and the default one."""
    reg = providers()
    infos = [_cache.get(pid, p).info() for pid, p in reg.items()]
    default = default_id(reg)
    usable = [i for i in infos if i["available"]]
    return {"available": bool(usable), "default": default, "providers": infos,
            "reason": "" if usable else "no provider is configured: set an API key in .env (see .env.example)"}
