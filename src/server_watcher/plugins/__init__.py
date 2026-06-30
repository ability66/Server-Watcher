from __future__ import annotations

from .base import CommandPlugin
from .df import DfPlugin
from .du import DuPlugin
from .gpustat import GpustatPlugin
from .top import TopPlugin


def build_plugins(*, plugin_configs: dict[str, object], max_message_chars: int) -> list[CommandPlugin]:
    plugins: list[CommandPlugin] = []
    gpustat_config = plugin_configs.get("gpustat")
    if gpustat_config is None or getattr(gpustat_config, "enabled", True):
        raw = {} if gpustat_config is None else dict(getattr(gpustat_config, "raw", {}) or {})
        plugins.append(GpustatPlugin(raw_config=raw, max_message_chars=max_message_chars))
    top_config = plugin_configs.get("top")
    if top_config is None or getattr(top_config, "enabled", True):
        raw = {} if top_config is None else dict(getattr(top_config, "raw", {}) or {})
        plugins.append(TopPlugin(raw_config=raw, max_message_chars=max_message_chars))
    df_config = plugin_configs.get("df")
    if df_config is None or getattr(df_config, "enabled", True):
        raw = {} if df_config is None else dict(getattr(df_config, "raw", {}) or {})
        plugins.append(DfPlugin(raw_config=raw, max_message_chars=max_message_chars))
    du_config = plugin_configs.get("du")
    if du_config is None or getattr(du_config, "enabled", True):
        raw = {} if du_config is None else dict(getattr(du_config, "raw", {}) or {})
        plugins.append(DuPlugin(raw_config=raw, max_message_chars=max_message_chars))
    return plugins
