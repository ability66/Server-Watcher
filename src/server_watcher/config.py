from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_QQBOT_API_BASE = "https://api.sgroup.qq.com"
DEFAULT_QQBOT_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_MAX_MESSAGE_CHARS = 3000


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class QQBotConfig:
    enabled: bool = True
    target_type: str = "c2c"
    target: str | None = None
    app_id_env: str = "QQBOT_APP_ID"
    client_secret_env: str = "QQBOT_CLIENT_SECRET"
    app_id: str | None = None
    client_secret: str | None = None
    api_base: str = DEFAULT_QQBOT_API_BASE
    token_url: str = DEFAULT_QQBOT_TOKEN_URL
    timeout_seconds: int = 30
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginConfig:
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    qq: QQBotConfig
    plugins: dict[str, PluginConfig]
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    source_path: Path | None = None


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return default_config(source_path=path)

    with path.open("r", encoding="utf-8") as file_obj:
        data = yaml.safe_load(file_obj) or {}

    providers = data.get("providers") or {}
    raw_qq = dict(providers.get("qq") or {})
    qq = QQBotConfig(
        enabled=bool(raw_qq.get("enabled", True)),
        target_type=normalize_target_type(raw_qq.get("target_type") or "c2c"),
        target=_optional_string(raw_qq.get("target")),
        app_id_env=str(raw_qq.get("app_id_env") or "QQBOT_APP_ID"),
        client_secret_env=str(raw_qq.get("client_secret_env") or "QQBOT_CLIENT_SECRET"),
        app_id=_optional_string(raw_qq.get("app_id")),
        client_secret=_optional_string(raw_qq.get("client_secret")),
        api_base=str(raw_qq.get("api_base") or DEFAULT_QQBOT_API_BASE).rstrip("/"),
        token_url=str(raw_qq.get("token_url") or DEFAULT_QQBOT_TOKEN_URL),
        timeout_seconds=max(1, int(raw_qq.get("timeout_seconds") or 30)),
        raw=raw_qq,
    )

    raw_plugins = data.get("plugins") or {}
    plugins: dict[str, PluginConfig] = {}
    for name, raw_plugin in raw_plugins.items():
        raw_plugin = dict(raw_plugin or {})
        plugins[str(name)] = PluginConfig(
            enabled=bool(raw_plugin.get("enabled", True)),
            raw=raw_plugin,
        )

    if "gpustat" not in plugins:
        plugins["gpustat"] = PluginConfig(enabled=True, raw={})

    max_message_chars = max(
        200,
        int((data.get("message") or {}).get("max_chars") or data.get("max_chars") or DEFAULT_MAX_MESSAGE_CHARS),
    )
    return AppConfig(
        qq=qq,
        plugins=plugins,
        max_message_chars=max_message_chars,
        source_path=path,
    )


def default_config(*, source_path: Path | None = None) -> AppConfig:
    return AppConfig(
        qq=QQBotConfig(enabled=True),
        plugins={"gpustat": PluginConfig(enabled=True, raw={})},
        max_message_chars=DEFAULT_MAX_MESSAGE_CHARS,
        source_path=source_path,
    )


def resolve_secret(*, env_name: str, config_value: object, label: str) -> str:
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value

    value = str(config_value or "").strip()
    if value:
        return value

    raise ConfigError(f"Missing {label}: set env {env_name} or config value providers.qq.{label}")


def validate_runtime_config(config: AppConfig) -> None:
    if not config.qq.enabled:
        raise ConfigError("QQ provider is disabled.")
    if not config.qq.target:
        raise ConfigError("providers.qq.target is required and must be the authorized user/group openid.")


def normalize_target_type(value: object) -> str:
    normalized = str(value or "c2c").strip().lower()
    if normalized in {"c2c", "private", "user"}:
        return "c2c"
    if normalized == "group":
        return "group"
    raise ConfigError(f"Unsupported target_type: {value}. Use c2c or group.")


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None

