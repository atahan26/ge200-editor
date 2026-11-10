"""Provider layer for the tone generator: one small interface, several LLM back ends.

Back ends (all optional, none needs more than the standard library except Anthropic's own SDK):

  anthropic   Claude via the official SDK (``pip install anthropic``), ``ANTHROPIC_API_KEY``
  openai      OpenAI Chat Completions, ``OPENAI_API_KEY``
  gemini      Google Gemini ``generateContent``, ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)
  openrouter  OpenRouter (many vendors behind one key), ``OPENROUTER_API_KEY``
  local       any OpenAI-compatible server you run yourself (Ollama, LM Studio, vLLM, llama.cpp ...);
              set ``GE200_LLM_BASE_URL`` (Ollama's default is ``http://localhost:11434/v1``)

Settings come from the environment, a git-ignored ``.env`` (see ``ge200.env``) or the editor's own setup dialog
(saved to ``llm.json`` next to ``rig.json``, outside the repository; a real environment variable wins):

  GE200_LLM_PROVIDER   default provider id (otherwise the first one that has credentials)
  GE200_LLM_MODEL      default model for the non-Anthropic providers (or GE200_<ID>_MODEL, e.g. GE200_OPENAI_MODEL)
  GE200_LLM_BASE_URL   base URL of the ``local`` provider
  GE200_LLM_API_KEY    optional key for the ``local`` provider
  GE200_LLM_MAX_TOKENS answer length limit for the OpenAI-style and Gemini providers (default 8000)

Safety: hosted providers always talk to their fixed vendor URL, so a key can never be sent somewhere else. The only
user-chosen address is the ``local`` provider's server, set in the setup dialog (a per-run token protects it) or the
environment. Requests from the page can choose a provider *id* and a model *name*, never a URL. Model names are not guessed here: there is no default for the non-Anthropic providers, name
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
from pathlib import Path
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


CONFIG_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GE200_LLM_BASE_URL",
               "GE200_LLM_API_KEY", "GE200_LLM_MODEL", "GE200_LLM_PROVIDER", "GE200_ANTHROPIC_MODEL",
               "GE200_OPENAI_MODEL", "GE200_GEMINI_MODEL", "GE200_OPENROUTER_MODEL", "GE200_LOCAL_MODEL")


def config_path() -> Path:
    """Where the editor's provider setup is stored: outside the repository, next to rig.json (or GE200_LLM_CONFIG)."""
    override = os.environ.get("GE200_LLM_CONFIG")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "ge200" / "llm.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ge200" / "llm.json"


def _file_env() -> Dict[str, str]:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return {}
    return {k: v.strip() for k, v in env.items() if k in CONFIG_KEYS and isinstance(v, str) and v.strip()}


def save_config(updates: Dict[str, Optional[str]]) -> None:
    """Store provider settings entered in the editor. Empty value / None removes a setting. Plain-text file (like
    a .env): keep it out of backups you share. Only the whitelisted names are accepted; secrets are never returned."""
    if not isinstance(updates, dict):
        raise LlmError("settings must be an object")
    current = _file_env()
    for k, v in updates.items():
        if k not in CONFIG_KEYS:
            raise LlmError(f"unknown setting {k}")
        if v is None or v == "":
            current.pop(k, None)
            continue
        if not isinstance(v, str) or len(v) > 300 or any(ord(c) < 32 for c in v):
            raise LlmError(f"invalid value for {k}")
        v = v.strip()
        if k == "GE200_LLM_BASE_URL" and urllib.parse.urlparse(v).scheme not in ("http", "https"):
            raise LlmError("the server address must start with http:// or https://")
        if k.endswith("_MODEL") and not MODEL_RE.match(v):
            raise LlmError("invalid model name")
        current[k] = v
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"env": current}, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    _cache.clear()


def source_of(*names: str) -> str:
    """Where a setting comes from: 'environment' (real environment or .env), 'saved' (editor setup) or ''."""
    saved = _file_env()
    for n in names:
        if os.environ.get(n, "").strip():
            return "environment"
        if saved.get(n):
            return "saved"
    return ""


def _env(*names: str) -> str:
    """A setting from the real environment, else from the editor's saved setup."""
    saved = None
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
        if saved is None:
            saved = _file_env()
        if saved.get(n):
            return saved[n]
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


def _model_ids(ids) -> List[str]:
    """Unique, valid-looking model names in the order the provider gave them."""
    out: List[str] = []
    for i in ids:
        if isinstance(i, str) and MODEL_RE.match(i) and i not in out:
            out.append(i)
    return out


# OpenAI's list mixes in embeddings, speech, images and moderation models that cannot write a preset
_NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|moderation|audio|realtime|image|transcribe|davinci|babbage|search|sora", re.I)


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
                "reason": reason, "default_model": self.default_model(), "needs_key": self.needs_key,
                "source": source_of(*self.key_env) if self.key_env else ""}

    def complete(self, system: str, user: str, schema: dict, model: str, effort: str = "high") -> Completion:
        raise NotImplementedError

    def list_models(self) -> List[str]:
        """Model names the provider offers to this account (so nobody has to look them up by hand)."""
        raise LlmError("this provider cannot list its models")


class AnthropicProvider(Provider):
    id, label, key_env = "anthropic", "Claude (Anthropic)", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]

    def __init__(self, client=None, fallbacks: bool = True) -> None:
        self._client = client
        self.fallbacks = fallbacks

    def default_model(self) -> str:
        return _env("GE200_ANTHROPIC_MODEL") or ANTHROPIC_DEFAULT_MODEL

    def _has_sdk(self) -> bool:
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    def info(self) -> dict:
        has_key = bool(_env("ANTHROPIC_API_KEY"))
        base = {"id": self.id, "label": self.label, "default_model": self.default_model(), "needs_key": True,
                "source": source_of("ANTHROPIC_API_KEY")}
        if self._client is not None:
            return {**base, "available": True, "reason": ""}
        if has_key:                                  # works with or without the SDK (REST fallback)
            return {**base, "available": True, "reason": ""}
        if not self._has_sdk():
            return {**base, "available": False, "reason": "set ANTHROPIC_API_KEY (no SDK needed)"}
        return {**base, "available": True, "reason": "no ANTHROPIC_API_KEY (an 'ant auth login' profile also works)"}

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LlmError("the 'anthropic' package is not installed (pip install anthropic)") from e
            self._client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY") or None)
        return self._client

    REST_URL = "https://api.anthropic.com/v1/messages"
    MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"

    def list_models(self) -> List[str]:
        key = _env("ANTHROPIC_API_KEY")
        if not key:
            raise LlmError("no API key: set ANTHROPIC_API_KEY")
        data = post_json(self.MODELS_URL, {"x-api-key": key, "anthropic-version": "2023-06-01"}, None, timeout=30)
        return _model_ids(x.get("id") for x in data.get("data", []) if isinstance(x, dict))

    def _complete_rest(self, system, user, schema, model) -> Completion:
        """Plain Messages API call for when the SDK is not installed (schema travels in the prompt)."""
        key = _env("ANTHROPIC_API_KEY")
        data = post_json(self.REST_URL, {"x-api-key": key, "anthropic-version": "2023-06-01"}, {
            "model": check_model(model or ANTHROPIC_DEFAULT_MODEL), "max_tokens": 16000,
            "system": [{"type": "text", "text": system + "\n\n" + schema_instruction(schema), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}]})
        text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        return Completion(text, refused=data.get("stop_reason") == "refusal", truncated=data.get("stop_reason") == "max_tokens")

    def complete(self, system, user, schema, model, effort="high") -> Completion:
        if self._client is None and not self._has_sdk():
            if not _env("ANTHROPIC_API_KEY"):
                raise LlmError("no API key: set ANTHROPIC_API_KEY")
            return self._complete_rest(system, user, schema, model)
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


def post_json(url: str, headers: Dict[str, str], body: Optional[dict], timeout: float = 180.0) -> dict:
    """POST JSON (GET when ``body`` is None) and return the decoded JSON answer. Errors become LlmError without
    echoing headers."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise LlmError("the provider URL must start with http:// or https://")
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode("utf-8"),
                                 method="GET" if body is None else "POST",
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

    def list_models(self) -> List[str]:
        base = self.base_url().rstrip("/")
        if not base:
            raise LlmError("no server address configured for this provider")
        key = _env(*self.key_env) if self.key_env else ""
        if self.needs_key and not key:
            raise LlmError("no API key: set " + " or ".join(self.key_env))
        data = post_json(base + "/models", {"Authorization": f"Bearer {key}"} if key else {}, None, timeout=30)
        ids = _model_ids(x.get("id") for x in data.get("data", []) if isinstance(x, dict))
        if self.id == "openai":
            ids = [i for i in ids if not _NOT_CHAT.search(i)]
        return sorted(ids)

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

    def list_models(self) -> List[str]:
        key = _env(*self.key_env)
        if not key:
            raise LlmError("no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)")
        data = post_json(f"{self.base_url()}/models?pageSize=1000", {"x-goog-api-key": key}, None, timeout=30)
        names = []
        for m in data.get("models", []):
            if isinstance(m, dict) and "generateContent" in (m.get("supportedGenerationMethods") or []):
                names.append(str(m.get("name", "")).replace("models/", "", 1))
        return sorted(_model_ids(names))

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
    return {"available": bool(usable), "default": default, "providers": infos, "config_path": str(config_path()),
            "reason": "" if usable else "no AI provider is connected yet"}
