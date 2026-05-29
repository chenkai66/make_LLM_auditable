"""A Claude Code companion *chain* for the live view's narration + Q&A.

Why a chain and not a raw LLM call: the god-view's whole premise is that a system
graph is where cross-component bugs hide. Answering questions about it well means
reading the actual code, not just a capped summary. So instead of POSTing a prompt
to /chat/completions, this launches a real `claude -p` instance whose cwd is the
WATCHED project, with read-only tools -- literally "restart a Claude Code beside you
to read along". It can Read/Grep/Glob the real source to ground its answer.

This matches the rest of the ecosystem's CC integration (claude -p --model
--allowed-tools --output-format text; auth via the ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL env trio).

Portability mirrors the ai: provider -- we name ENV VARS, never literal keys:
  * If the host's `claude` is already authenticated (OAuth/keychain), no config is
    needed; we run WITHOUT --bare so that ambient auth is honored.
  * If a token env (default ANTHROPIC_AUTH_TOKEN) is present, we inject the
    BASE_URL/AUTH_TOKEN/MODEL trio and add --bare, exactly matching a key-pool host
    like the research-agent runner.

Graceful degradation: if disabled or `claude` isn't on PATH, `available` is False
and the caller falls back to deterministic template narration / an off message.
"""
import os
import shutil
import subprocess

_DEFAULTS = {
    "enabled": True,
    "bin": "claude",
    "model": None,
    "narrate_model": None,
    "base_url_env": "ANTHROPIC_BASE_URL",
    "token_env": "ANTHROPIC_AUTH_TOKEN",
    "allowed_tools": "Read,Glob,Grep",
    "narrate_timeout": 45,
    "qa_timeout": 180,
}


class CCChain:
    def __init__(self, project_root, bin, model, base_url, token,
                 allowed_tools, timeout, enabled=True):
        self.project_root = project_root
        self.bin = bin or "claude"
        self.model = model
        self.base_url = base_url
        self.token = token
        self.allowed_tools = allowed_tools
        self.timeout = timeout
        self.enabled = bool(enabled)
        self.last_error = None

    @property
    def available(self):
        return bool(self.enabled and shutil.which(self.bin))

    def explain_unavailable(self):
        if not self.enabled:
            return "cc.enabled is false"
        if not shutil.which(self.bin):
            return "`%s` not found on PATH (install Claude Code CLI)" % self.bin
        return "CC chain ready"

    @classmethod
    def from_config(cls, cfg, kind="qa"):
        raw = dict(_DEFAULTS)
        raw.update((cfg.get("cc") or {}))
        base_url = os.environ.get(raw["base_url_env"])
        token = os.environ.get(raw["token_env"])
        if kind == "narrate":
            model = raw.get("narrate_model") or raw.get("model")
            timeout = raw.get("narrate_timeout", 45)
        else:
            model = raw.get("model")
            timeout = raw.get("qa_timeout", 180)
        return cls(
            project_root=getattr(cfg, "project_root", None) or os.getcwd(),
            bin=raw.get("bin"),
            model=model,
            base_url=base_url,
            token=token,
            allowed_tools=raw.get("allowed_tools"),
            timeout=timeout,
            enabled=bool(raw.get("enabled", True)),
        )

    def _env_and_bare(self):
        """(env, bare). Inject the BASE_URL/AUTH_TOKEN/MODEL trio + --bare only when
        an explicit token env is configured (key-pool host). Otherwise inherit the
        host's ambient `claude` auth (OAuth/keychain) and DON'T use --bare, since
        --bare disables OAuth/keychain reads."""
        env = os.environ.copy()
        # Break the feedback loop: the companion runs `claude -p` INSIDE the watched
        # project, which has our hooks installed. Without --bare those hooks fire on
        # the companion's own prompt/stop and POST back to /event -- narrating our own
        # narration, forever. This marker (inherited by the companion's hook procs)
        # tells hook.py to drop anything the companion itself triggers.
        env["SEAMLENS_LIVE_INTERNAL"] = "1"
        if self.base_url and self.token:
            env.pop("ANTHROPIC_API_KEY", None)
            env["ANTHROPIC_BASE_URL"] = self.base_url
            env["ANTHROPIC_AUTH_TOKEN"] = self.token
            if self.model:
                env["ANTHROPIC_MODEL"] = self.model
            return env, True
        return env, False

    def run(self, prompt, system=None, timeout=None):
        """Spawn `claude -p` in the watched project and return its text output, or
        None on any failure (recorded in last_error). The companion may read the
        real source via its read-only tools before answering."""
        if not self.available:
            self.last_error = self.explain_unavailable()
            return None
        env, bare = self._env_and_bare()
        cmd = [self.bin, "-p", prompt, "--output-format", "text",
               "--allowed-tools", self.allowed_tools]
        if self.model:
            cmd += ["--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        if bare:
            cmd += ["--bare"]
        try:
            proc = subprocess.run(
                cmd, cwd=self.project_root, env=env,
                capture_output=True, text=True,
                timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            self.last_error = "claude -p timed out after %ss" % (timeout or self.timeout)
            return None
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return None
        if proc.returncode != 0:
            self.last_error = "claude exited %s: %s" % (
                proc.returncode, (proc.stderr or "").strip()[:300])
            return None
        out = (proc.stdout or "").strip()
        if not out:
            self.last_error = "empty output (stderr: %s)" % (proc.stderr or "").strip()[:200]
            return None
        return out
