"""Resolve guest model routes without importing the owner's subscription credentials."""

from urllib.parse import urlsplit

from .model import GuestsError

CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_PROVIDER = "openai-codex"
API_KEY_ROUTES = {
    "openai": ("https://api.openai.com/v1", "codex_responses", ("OPENAI_API_KEY",)),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "chat_completions",
        ("OPENROUTER_API_KEY",),
    ),
    "anthropic": ("https://api.anthropic.com", "anthropic_messages", ("ANTHROPIC_API_KEY",)),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta",
        "chat_completions",
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ),
    "deepseek": ("https://api.deepseek.com/v1", "chat_completions", ("DEEPSEEK_API_KEY",)),
    "xai": ("https://api.x.ai/v1", "codex_responses", ("XAI_API_KEY",)),
}
ALIASES = {
    "codex": CODEX_PROVIDER,
    "openai-api": "openai",
    "chatgpt": CODEX_PROVIDER,
    "claude": "anthropic",
    "or": "openrouter",
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "grok": "xai",
    "deepseek-chat": "deepseek",
}
LOCAL_URLS = {"ollama": "http://127.0.0.1:11434/v1", "lmstudio": "http://127.0.0.1:1234/v1"}
NATIVE_MODES = {"chat_completions", "anthropic_messages", "codex_responses"}
ENDPOINT_KEYS = ("base_url", "api", "url")
CREDENTIAL_KEYS = ("key_env", "api_key_env", "api_key", "key_cmd")
MODEL_KEYS = ("model", "default", "default_model")


def _origin(url):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise GuestsError("Use a model endpoint URL without credentials, a query, or a fragment.")
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or {"http": 80, "https": 443}.get(parsed.scheme),
    )


def _loopback(origin):
    return origin[1] in {"localhost", "127.0.0.1", "::1"}


def _route(entry, custom, get_secret):
    name = str(entry.get("provider") or "auto").strip().lower()
    name = ALIASES.get(name, name)
    model = entry.get("model") or entry.get("default") or entry.get("default_model")
    if not isinstance(model, str) or not model.strip():
        raise GuestsError("Choose a Hermes model before sharing.")
    model = model.strip()
    if entry.get("enabled") is False:
        raise GuestsError("Enable the model provider in Hermes before sharing.")
    if name == CODEX_PROVIDER:
        if entry.get("openai_runtime") == "codex_app_server":
            raise GuestsError("Choose the native Codex API runtime in Hermes for sharing.")
        base_url = str(entry.get("base_url") or "").rstrip("/")
        if base_url and base_url != CODEX_URL.removesuffix("/responses"):
            raise GuestsError("Choose the standard Codex endpoint in Hermes for sharing.")
        return {"model": model, "provider": CODEX_PROVIDER, "api_mode": "codex_responses"}
    if name in {"nous", "claude-code"} or name.endswith("-oauth"):
        raise GuestsError(
            "This sign-in has no supported guest model route. Choose a supported fallback in Hermes."
        )

    default_url, default_mode, secret_names = API_KEY_ROUTES.get(
        name, (LOCAL_URLS.get(name, ""), "chat_completions", ())
    )
    base_url = str(
        entry.get("base_url") or entry.get("api") or entry.get("url") or default_url
    ).rstrip("/")
    mode = entry.get("api_mode") or entry.get("transport") or default_mode
    if mode not in NATIVE_MODES or (
        name == "openai" and entry.get("openai_runtime") == "codex_app_server"
    ):
        raise GuestsError("Choose a native API model in Hermes for sharing.")
    if not base_url:
        raise GuestsError(
            "Choose a supported provider in Hermes for sharing: Codex, an API-key provider, "
            "or a configured local model."
        )
    try:
        origin = _origin(base_url)
    except ValueError as exc:
        raise GuestsError("Choose a valid model endpoint in Hermes for sharing.") from exc
    local = _loopback(origin)
    key_env = entry.get("key_env") or entry.get("api_key_env")
    if origin[0] != "https" and not (
        origin[0] == "http" and (local or (custom and not key_env and not secret_names))
    ):
        raise GuestsError("Use HTTPS for a remote model endpoint that needs a key.")
    if name in API_KEY_ROUTES and origin != _origin(default_url) and not key_env:
        raise GuestsError("Set a profile-scoped key_env for the custom model endpoint.")
    if name not in API_KEY_ROUTES and not custom and not (name == "auto" and local):
        raise GuestsError(
            "This sign-in has no supported guest model route. Choose a model in Hermes."
        )
    if entry.get("key_cmd") or (entry.get("api_key") and not key_env):
        raise GuestsError("Configure the model key with key_env in the Hermes profile.")
    key = next(
        (value for env in ((key_env,) if key_env else secret_names) if (value := get_secret(env))),
        "",
    )
    if (key_env or secret_names) and not key:
        raise GuestsError("Connect the model's API key in the Hermes profile before sharing.")
    if name == "anthropic" and not key.startswith("sk-ant-api"):
        raise GuestsError(
            "Use an Anthropic API key for sharing. Choose it through Hermes model setup."
        )
    return {
        "model": model,
        "provider": name if name in API_KEY_ROUTES else "custom",
        "base_url": base_url,
        "api_key": key or "guests-no-key",
        "api_mode": mode,
    }


def _selected(entry, details):
    """The owner's entry over a named provider's entry, as Hermes reads the pair.

    A named provider's endpoint and credential stay together, so an endpoint left on
    the owner's entry never receives that provider's key. The model the owner chose
    wins over the provider's own default.
    """
    merged = {**details, **entry}
    if any(details.get(key) for key in (*ENDPOINT_KEYS, *CREDENTIAL_KEYS)):
        for key in (*ENDPOINT_KEYS, *CREDENTIAL_KEYS):
            merged.pop(key, None)
            if key in details:
                merged[key] = details[key]
    chosen = next((entry[key] for key in MODEL_KEYS if entry.get(key)), None)
    if chosen:
        for key in MODEL_KEYS:
            merged.pop(key, None)
        merged["model"] = chosen
    return merged


def resolve_model(config, get_secret, identify=None):
    """Resolve the guest model route.

    `identify` names the provider Hermes selects when the owner left the choice to
    Hermes. It returns a provider name and reads no credential.
    """
    model = config.get("model") or {}
    primary = {"default": model} if isinstance(model, str) else model
    if not isinstance(primary, dict):
        raise GuestsError("Choose a Hermes model before sharing.")
    providers = config.get("providers") or {}
    legacy = config.get("custom_providers") or []
    legacy = legacy if isinstance(legacy, list) else []
    fallback_chain = []
    for key in ("fallback_providers", "fallback_model"):
        entries = config.get(key)
        if isinstance(entries, dict):
            entries = [entries]
        if isinstance(entries, list):
            fallback_chain.extend(
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("provider") and entry.get("model")
            )
    first_error = None
    for entry in (primary, *fallback_chain):
        try:
            name = str(entry.get("provider") or "auto").strip().lower()
            if name == "auto" and identify and not any(entry.get(k) for k in ENDPOINT_KEYS):
                # Hermes makes the choice, and that provider's own settings then apply.
                name = str(identify() or "auto").strip().lower()
                entry = {**entry, "provider": name}
            slug = name.removeprefix("custom:")
            details = providers.get(slug) if isinstance(providers, dict) else None
            if details is None:
                details = next(
                    (
                        item
                        for item in legacy
                        if isinstance(item, dict)
                        and str(item.get("name") or "").strip().lower().replace(" ", "-") == slug
                    ),
                    {},
                )
            details = details if isinstance(details, dict) else {}
            return _route(
                _selected(entry, details),
                bool(details) or name in {"custom", *LOCAL_URLS} or name.startswith("custom:"),
                get_secret,
            )
        except GuestsError as exc:
            if first_error is None:
                first_error = exc
    raise first_error


def codex_input(items):
    """Message text as typed parts, the only text form the Codex endpoint accepts."""
    return [
        {
            **item,
            "content": [
                {
                    "type": "output_text" if item["role"] == "assistant" else "input_text",
                    "text": item["content"],
                }
            ],
        }
        if isinstance(item, dict) and item.get("role") and isinstance(item.get("content"), str)
        else item
        for item in items
    ]


def request_codex(body, *, timeout):
    from hermes_cli.plugin_provider_requests import credentialed_provider_request

    return credentialed_provider_request(
        CODEX_PROVIDER,
        "POST",
        CODEX_URL,
        json=body,
        headers={"originator": "hermes-agent"},
        timeout=timeout,
    )
