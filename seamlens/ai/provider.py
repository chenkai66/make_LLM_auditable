"""Config-driven LLM provider for the optional AI layer.

Design constraints that keep seamlens portable:

  * No SDK dependency -- talks to any OpenAI-compatible /chat/completions endpoint
    over stdlib urllib, so the core dependency set stays {PyYAML} and it runs on
    any Python 3.7+ with no install step.
  * No credentials in code OR in seamlens.yaml -- the config names *environment
    variables* that hold the base_url and key. A host wires those env vars however
    it likes (a key pool, a secrets manager, a plain export); seamlens never sees
    a literal key. This is the same "config is the only host surface, and even the
    config holds no secret" discipline the rest of the tool follows.
  * Graceful degradation -- if disabled, unconfigured, or the call fails, the
    provider reports `available = False` / returns None, and every AI command
    falls back to its deterministic behaviour instead of crashing.

`ai:` config block (all optional; shown with defaults)::

    ai:
      enabled: false
      base_url_env: "SEAMLENS_AI_BASE_URL"   # env var holding the endpoint base
      key_env: "SEAMLENS_AI_KEY"             # env var holding the API key
      base_url: null                         # optional literal fallback (no secret)
      model: "qwen-plus"
      max_tokens: 2048
      temperature: 0.2
      timeout: 60
"""
import json
import os
import urllib.error
import urllib.request

_DEFAULTS = {
    "enabled": False,
    "base_url_env": "SEAMLENS_AI_BASE_URL",
    "key_env": "SEAMLENS_AI_KEY",
    "base_url": None,
    "model": "qwen-plus",
    "max_tokens": 2048,
    "temperature": 0.2,
    "timeout": 60,
}


class Provider:
    def __init__(self, base_url, api_key, model, max_tokens, temperature, timeout):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.last_error = None

    @property
    def available(self):
        return bool(self.base_url and self.api_key)

    @classmethod
    def from_config(cls, cfg):
        raw = dict(_DEFAULTS)
        raw.update((cfg.get("ai") or {}))
        base_url = os.environ.get(raw["base_url_env"]) or raw.get("base_url")
        api_key = os.environ.get(raw["key_env"])
        prov = cls(base_url, api_key, raw["model"], raw["max_tokens"],
                   raw["temperature"], raw["timeout"])
        prov.enabled = bool(raw["enabled"])
        return prov

    def explain_unavailable(self):
        raw_env = (self.base_url, self.api_key)
        if not raw_env[0]:
            return "AI base_url not set (export the base_url_env var or set ai.base_url)"
        if not raw_env[1]:
            return "AI key not set (export the key_env var)"
        return "AI provider ready"

    # -- chat -------------------------------------------------------------
    def complete(self, system, user, max_tokens=None, temperature=None):
        """Return assistant text, or None on any failure (recorded in last_error)."""
        if not self.available:
            self.last_error = self.explain_unavailable()
            return None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            self.last_error = "HTTP %s: %s" % (e.code, detail)
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
        return None

    def complete_json(self, system, user, **kw):
        """complete() + tolerant JSON parse (handles ```json fences). Returns the
        parsed object, or None."""
        txt = self.complete(system, user, **kw)
        if txt is None:
            return None
        return _parse_json_loose(txt)


def _parse_json_loose(txt):
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("```", 2)
        s = s[1] if len(s) > 1 else txt
        if s.startswith("json"):
            s = s[4:]
        s = s.strip().rstrip("`").strip()
    try:
        return json.loads(s)
    except Exception:
        # last resort: grab the outermost {...} or [...]
        for op, cl in (("{", "}"), ("[", "]")):
            i, j = s.find(op), s.rfind(cl)
            if 0 <= i < j:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    pass
    return None
