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
class ServerTargetConfig:
    name: str
    label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ServerSwitchConfig:
    enabled: bool = False
    current: str = "local"
    default: str | None = None
    targets: tuple[ServerTargetConfig, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    qq: QQBotConfig
    plugins: dict[str, PluginConfig]
    server_switch: ServerSwitchConfig = field(default_factory=ServerSwitchConfig)
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
        server_switch=load_server_switch_config(data.get("servers") or data.get("server_switch") or {}),
        max_message_chars=max_message_chars,
        source_path=path,
    )


def default_config(*, source_path: Path | None = None) -> AppConfig:
    return AppConfig(
        qq=QQBotConfig(enabled=True),
        plugins={"gpustat": PluginConfig(enabled=True, raw={})},
        server_switch=ServerSwitchConfig(enabled=False),
        max_message_chars=DEFAULT_MAX_MESSAGE_CHARS,
        source_path=source_path,
    )


def load_server_switch_config(raw_value: object) -> ServerSwitchConfig:
    if not isinstance(raw_value, dict):
        return ServerSwitchConfig(enabled=False)

    raw = dict(raw_value)
    enabled = bool(raw.get("enabled", False))
    current = _normalize_server_key(raw.get("current") or raw.get("current_server") or "local")
    default = _optional_server_key(raw.get("default") or raw.get("default_active"))
    targets = _normalize_server_targets(raw.get("items") or raw.get("targets") or raw.get("choices") or raw.get("servers"))

    if current not in {target.name for target in targets}:
        targets = (
            *targets,
            ServerTargetConfig(
                name=current,
                label=str(raw.get("label") or current).strip() or current,
                aliases=(current,),
            ),
        )

    _validate_server_targets(targets)
    target_names = {target.name for target in targets}
    if default is not None and default not in target_names:
        raise ConfigError(f"servers.default 指向了未配置的服务器：{default}")
    return ServerSwitchConfig(
        enabled=enabled,
        current=current,
        default=default,
        targets=targets,
        raw=raw,
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


def _optional_server_key(value: object) -> str | None:
    text = _normalize_server_key(value)
    return text or None


def _normalize_server_key(value: object) -> str:
    return str(value or "").strip().lower()


def _normalize_server_targets(raw_targets: object) -> tuple[ServerTargetConfig, ...]:
    if isinstance(raw_targets, dict):
        return tuple(
            _normalize_server_target(name=key, raw_target=value)
            for key, value in raw_targets.items()
            if str(key or "").strip()
        )

    if isinstance(raw_targets, list):
        targets: list[ServerTargetConfig] = []
        for index, raw_target in enumerate(raw_targets):
            if isinstance(raw_target, dict):
                target = _normalize_server_target(name=raw_target.get("name") or f"server-{index + 1}", raw_target=raw_target)
            else:
                target = _normalize_server_target(name=raw_target, raw_target={})
            if target.name:
                targets.append(target)
        return tuple(targets)

    return ()


def _normalize_server_target(*, name: object, raw_target: object) -> ServerTargetConfig:
    raw = dict(raw_target or {}) if isinstance(raw_target, dict) else {}
    normalized_name = _normalize_server_key(raw.get("name") or name)
    label = str(raw.get("label") or raw.get("display_name") or normalized_name).strip() or normalized_name
    aliases = _normalize_server_aliases(raw.get("aliases"), default=normalized_name)
    return ServerTargetConfig(name=normalized_name, label=label, aliases=aliases)


def _normalize_server_aliases(raw_aliases: object, *, default: str) -> tuple[str, ...]:
    aliases: list[str] = []
    if isinstance(raw_aliases, list):
        aliases = [_normalize_server_key(alias) for alias in raw_aliases if _normalize_server_key(alias)]
    elif isinstance(raw_aliases, str) and raw_aliases.strip():
        aliases = [_normalize_server_key(raw_aliases)]

    if default and default not in aliases:
        aliases.insert(0, default)
    return tuple(dict.fromkeys(aliases))


def _validate_server_targets(targets: tuple[ServerTargetConfig, ...]) -> None:
    names: set[str] = set()
    aliases: dict[str, str] = {}
    for target in targets:
        if not target.name:
            raise ConfigError("servers.targets 里存在空的服务器名称。")
        if target.name in names:
            raise ConfigError(f"servers.targets 里服务器名称重复：{target.name}")
        names.add(target.name)
        for alias in target.aliases:
            owner = aliases.get(alias)
            if owner is not None and owner != target.name:
                raise ConfigError(f"服务器快捷指令重复：{alias} 同时属于 {owner} 和 {target.name}")
            aliases[alias] = target.name
