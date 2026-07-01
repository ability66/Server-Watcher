from __future__ import annotations

from dataclasses import dataclass
import threading

from .command_router import normalize_message_text
from .config import ServerSwitchConfig, ServerTargetConfig
from .plugins.base import TextReply


@dataclass(frozen=True)
class ServerSwitchDecision:
    handled: bool
    reply: TextReply | None = None


class ServerSelector:
    def __init__(self, config: ServerSwitchConfig) -> None:
        self.config = config
        self._targets_by_name = {target.name: target for target in config.targets}
        self._targets_by_alias = {
            alias: target
            for target in config.targets
            for alias in target.aliases
        }
        self._active_by_reply_target: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def handle_switch_command(self, *, reply_target: str, raw_text: str) -> ServerSwitchDecision:
        if not self.config.enabled:
            return ServerSwitchDecision(handled=False)

        text = normalize_message_text(raw_text)
        tokens = text.split()
        if len(tokens) != 1:
            return ServerSwitchDecision(handled=False)

        target = self._targets_by_alias.get(tokens[0].lower())
        if target is None:
            return ServerSwitchDecision(handled=False)

        with self._lock:
            self._active_by_reply_target[reply_target] = target.name

        if target.name != self.config.current:
            return ServerSwitchDecision(handled=True)

        return ServerSwitchDecision(
            handled=True,
            reply=TextReply(f"已切换到 {target.label}。后续命令会由这台服务器处理。"),
        )

    def should_process(self, *, reply_target: str) -> bool:
        if not self.config.enabled:
            return True
        return self.active_server_name(reply_target=reply_target) == self.config.current

    def active_server_name(self, *, reply_target: str) -> str:
        with self._lock:
            active = self._active_by_reply_target.get(reply_target)
        return active or self.config.default or self.config.current

    def help_lines(self) -> list[str]:
        if not self.config.enabled or not self.config.targets:
            return []

        shortcuts = " / ".join(_primary_alias(target) for target in self.config.targets)
        current = self._targets_by_name.get(self.config.current)
        current_label = current.label if current is not None else self.config.current
        return [
            "",
            "服务器切换：",
            f"- {shortcuts}: 切换当前聊天要查看的服务器。当前进程：{current_label}。",
        ]


def _primary_alias(target: ServerTargetConfig) -> str:
    return target.aliases[0] if target.aliases else target.name
