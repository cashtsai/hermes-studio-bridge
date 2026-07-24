"""Profile-scoped Hermes media adapter for Pocket's bridge.

The bridge owns transport and authentication only. Provider selection,
endpoints, models, and secrets stay in each Hermes profile.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit


class HermesMediaError(RuntimeError):
    """A sanitized Hermes media integration error."""


_STT_PROVIDERS = {
    "local",
    "local_command",
    "groq",
    "openai",
    "mistral",
    "xai",
    "elevenlabs",
    "deepinfra",
    "siege",
}
_OCR_PROVIDERS = {"none", "siege"}
_LOCALE_LANGUAGE = {
    "zh-Hant": "zh",
    "zh-Hans": "zh",
    "zh": "zh",
    "en": "en",
    "th": "th",
    "ja": "ja",
    "ko": "ko",
    "vi": "vi",
    "id": "id",
    "ms": "ms",
    "fr": "fr",
    "es": "es",
    "de": "de",
    "pt": "pt",
    "ru": "ru",
    "ar": "ar",
}
_LOCALE_PROMPT = {
    "zh-Hant": "以下是繁體中文的對話內容。",
    "zh-Hans": "以下是简体中文的对话内容。",
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@contextmanager
def _profile_scope(home: str | Path) -> Iterator[None]:
    try:
        profile_home = Path(home).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HermesMediaError(f"Hermes profile is not accessible: {exc}") from exc
    if not profile_home.is_dir():
        raise HermesMediaError("Hermes profile home is not a directory")
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(profile_home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _plugin_api(module: Any) -> Any | None:
    if module is None:
        return None
    api = getattr(module, "hermes_siege", module)
    required = (
        "ensure_stt_registered",
        "request_options",
        "ocr_document",
        "get_media_capabilities",
    )
    if all(callable(getattr(api, name, None)) for name in required):
        return api
    return None


@lru_cache(maxsize=1)
def _load_plugin():
    try:
        import hermes_siege
    except ImportError:
        hermes_siege = None
    api = _plugin_api(hermes_siege)
    if api is not None:
        return api

    try:
        from hermes_cli.plugins import get_plugin_manager

        manager = get_plugin_manager()
        manager.discover_and_load()
        loaded = getattr(manager, "_plugins", {}).get("hermes-siege")
        if loaded is None:
            manager.discover_and_load(force=True)
            loaded = getattr(manager, "_plugins", {}).get("hermes-siege")
        api = _plugin_api(getattr(loaded, "module", None))
    except Exception as exc:
        raise HermesMediaError(
            "Hermes could not load the hermes-siege plugin"
        ) from exc
    if loaded is None:
        raise HermesMediaError("hermes-siege is not installed for this profile")
    if not getattr(loaded, "enabled", False):
        raise HermesMediaError("hermes-siege is not enabled for this profile")
    if api is None:
        raise HermesMediaError("hermes-siege does not expose its media API")
    return api


def transcribe_audio(
    home: str | Path,
    file_path: str | Path,
    *,
    locale: str = "",
) -> dict[str, Any]:
    """Call Hermes' configured STT provider under the persona profile."""
    with _profile_scope(home):
        plugin = _load_plugin()
        plugin.ensure_stt_registered()
        from tools.transcription_tools import transcribe_audio as hermes_transcribe

        language = _LOCALE_LANGUAGE.get(locale, "")
        prompt = _LOCALE_PROMPT.get(locale, "")
        with plugin.request_options(language=language, prompt=prompt):
            result = hermes_transcribe(str(file_path))
    if not isinstance(result, dict):
        raise HermesMediaError("Hermes STT returned an invalid response")
    return result


def ocr_document(
    home: str | Path,
    file_path: str | Path,
) -> dict[str, Any]:
    """Call the Hermes OCR capability under the persona profile."""
    with _profile_scope(home):
        plugin = _load_plugin()
        result = plugin.ocr_document(file_path, trusted_path=True)
    if not isinstance(result, dict):
        raise HermesMediaError("Hermes OCR returned an invalid response")
    return result


def _provider_model(section: dict[str, Any], provider: str) -> str:
    provider_config = _mapping(section.get(provider))
    return str(
        provider_config.get("model")
        or provider_config.get("model_id")
        or ""
    ).strip()


def get_capabilities(
    home: str | Path,
    *,
    probe: bool = True,
    attachment_max_bytes: int = 32 * 1024 * 1024,
    attachment_max_count: int = 12,
) -> dict[str, Any]:
    """Return the effective, secret-free Hermes media configuration."""
    with _profile_scope(home):
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        stt = _mapping(config.get("stt"))
        ocr = _mapping(config.get("ocr"))
        stt_provider = str(stt.get("provider") or "").strip().lower()
        ocr_provider = str(ocr.get("provider") or "none").strip().lower()
        result = {
            "stt": {
                "enabled": bool(stt.get("enabled", True)),
                "provider": stt_provider,
                "model": _provider_model(stt, stt_provider),
                "configured": bool(stt_provider),
                "available": bool(stt.get("enabled", True) and stt_provider),
            },
            "ocr": {
                "enabled": bool(ocr.get("enabled", False)),
                "provider": ocr_provider,
                "configured": bool(ocr_provider and ocr_provider != "none"),
                "available": bool(
                    ocr.get("enabled", False)
                    and ocr_provider
                    and ocr_provider != "none"
                ),
            },
            "limits": {
                "attachment_count": int(attachment_max_count),
                "attachment_bytes": int(attachment_max_bytes),
                "stt_input_bytes": 25 * 1024 * 1024,
            },
            "provider_options": {
                "stt": sorted(_STT_PROVIDERS),
                "ocr": sorted(_OCR_PROVIDERS),
            },
        }
        if stt_provider == "siege" or ocr_provider == "siege":
            try:
                plugin_caps = _load_plugin().get_media_capabilities(probe=probe)
            except Exception as exc:
                result["error"] = (
                    str(exc)
                    if isinstance(exc, HermesMediaError)
                    else f"Hermes media capability check failed: {type(exc).__name__}"
                )
                if stt_provider == "siege":
                    result["stt"]["available"] = False
                if ocr_provider == "siege":
                    result["ocr"]["available"] = False
            else:
                if stt_provider == "siege":
                    result["stt"].update(_mapping(plugin_caps.get("stt")))
                if ocr_provider == "siege":
                    result["ocr"].update(_mapping(plugin_caps.get("ocr")))
    return result


def _optional_bool(target: dict[str, Any], source: dict[str, Any], key: str) -> None:
    if key in source:
        target[key] = bool(source[key])


def _bounded_text(value: Any, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise HermesMediaError(f"{field} exceeds {limit} characters")
    return text


def _validated_url(value: Any, *, field: str) -> str:
    raw = _bounded_text(value, field=field, limit=500).rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise HermesMediaError(f"{field} must be an http(s) URL")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _update_stt(config: dict[str, Any], patch: dict[str, Any]) -> None:
    stt = config.setdefault("stt", {})
    _optional_bool(stt, patch, "enabled")
    if "provider" in patch:
        provider = _bounded_text(
            patch["provider"], field="stt.provider", limit=40
        ).lower()
        if provider not in _STT_PROVIDERS:
            raise HermesMediaError("unsupported stt.provider")
        stt["provider"] = provider
    siege_patch = _mapping(patch.get("siege"))
    if siege_patch:
        siege = stt.setdefault("siege", {})
        if "base_url" in siege_patch:
            siege["base_url"] = _validated_url(
                siege_patch["base_url"], field="stt.siege.base_url"
            )
        for key, limit in (("model", 128), ("language", 32), ("prompt", 500)):
            if key in siege_patch:
                siege[key] = _bounded_text(
                    siege_patch[key],
                    field=f"stt.siege.{key}",
                    limit=limit,
                )


def _update_ocr(config: dict[str, Any], patch: dict[str, Any]) -> None:
    ocr = config.setdefault("ocr", {})
    _optional_bool(ocr, patch, "enabled")
    if "provider" in patch:
        provider = _bounded_text(
            patch["provider"], field="ocr.provider", limit=40
        ).lower()
        if provider not in _OCR_PROVIDERS:
            raise HermesMediaError("unsupported ocr.provider")
        ocr["provider"] = provider
    siege_patch = _mapping(patch.get("siege"))
    if siege_patch:
        siege = ocr.setdefault("siege", {})
        if "base_url" in siege_patch:
            siege["base_url"] = _validated_url(
                siege_patch["base_url"], field="ocr.siege.base_url"
            )
        for key in (
            "use_doc_orientation_classify",
            "use_doc_unwarping",
            "use_textline_orientation",
            "return_word_box",
        ):
            _optional_bool(siege, siege_patch, key)


def update_settings(
    home: str | Path,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Atomically update allowlisted Hermes media settings."""
    if not isinstance(patch, dict):
        raise HermesMediaError("settings body must be an object")
    unknown = set(patch) - {"stt", "ocr"}
    if unknown:
        raise HermesMediaError(
            f"unsupported settings fields: {', '.join(sorted(unknown))}"
        )
    with _profile_scope(home):
        from hermes_cli.config import read_raw_config, save_config

        config = read_raw_config()
        _update_stt(config, _mapping(patch.get("stt")))
        _update_ocr(config, _mapping(patch.get("ocr")))

        stt_provider = str(_mapping(config.get("stt")).get("provider") or "")
        ocr_provider = str(_mapping(config.get("ocr")).get("provider") or "")
        if "siege" in {stt_provider, ocr_provider}:
            plugins = config.setdefault("plugins", {})
            enabled = plugins.get("enabled")
            if not isinstance(enabled, list):
                enabled = []
                plugins["enabled"] = enabled
            if "hermes-siege" not in enabled:
                enabled.append("hermes-siege")

        save_config(config, strip_defaults=False)
        try:
            from tools.registry import invalidate_check_fn_cache

            invalidate_check_fn_cache()
        except Exception:
            pass
    return get_capabilities(home, probe=False)
