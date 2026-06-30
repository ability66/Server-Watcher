from __future__ import annotations

import re
from typing import Iterable

from .plugins.base import CommandPlugin, Reply, TextReply


LEADING_BOT_MENTION_RE = re.compile(r"^(?:\s*<@!?[^>]+>\s*)+")


class CommandRouter:
    def __init__(
        self,
        plugins: Iterable[CommandPlugin],
        *,
        help_aliases: tuple[str, ...] = ("help", "/help", "帮助"),
    ) -> None:
        self.plugins = list(plugins)
        self.help_aliases = tuple(alias.lower() for alias in help_aliases)

    def is_help_command(self, raw_text: str) -> bool:
        text = normalize_message_text(raw_text)
        if not text:
            return False
        return text.split()[0].lower() in self.help_aliases

    def dispatch(self, raw_text: str) -> Reply | None:
        text = normalize_message_text(raw_text)
        if not text:
            return None

        first_token = text.split()[0].lower()
        if first_token in self.help_aliases:
            return TextReply(self.help_text())

        for plugin in self.plugins:
            reply = plugin.handle(text)
            if reply is not None:
                return reply

        return TextReply(f"未识别命令：{first_token}。发送 help 查看支持命令。")

    def help_text(self) -> str:
        lines = [
            "支持的命令：",
            "- help: 查看帮助。这个命令始终由 bot 本地处理，即使你正在 Codex 对话里。",
        ]
        for plugin in self.plugins:
            examples = " / ".join(plugin.spec.examples) if plugin.spec.examples else ", ".join(plugin.spec.aliases)
            lines.append(f"- {examples}: {plugin.spec.summary}")
        lines.extend(
            [
                "",
                "本地工作区命令：",
                "- pwd: 查看当前工作目录。",
                "- ls: 查看当前工作目录内容。",
                "- ls -la /path: 查看指定目录内容。",
                "- cd /path: 切换当前工作目录；如果 Codex 会话正在运行，会在新目录重启。",
                "",
                "Codex Bridge：",
                "- codex: 启动服务器上的真实 Codex 会话。",
                "- codex 你的需求: 启动会话并把这句需求直接发给 Codex。",
                "- /permissions: 在没有活动会话时会自动启动 Codex 并转发；有活动会话时原样转发给 Codex。",
                "- /resume: 同上，会原样交给 Codex 的 slash 命令处理。",
                "- qqcodex status: 查看当前 Codex bridge 会话状态。",
                "- qqcodex stop: 停止当前 Codex bridge 会话。",
                "- qqcodex restart: 重启当前 Codex bridge 会话。",
                "- qqcodex resume last: 直接恢复最近一次 Codex 会话。",
                "- qqcodex resume <session_id>: 恢复指定 Codex 会话。",
                "- qqcodex key down|up|enter|esc|tab: 给 Codex TUI 发送按键。",
                "- qqcodex pick 2: 用于 /permissions 这类编号菜单，向下选第 2 项并回车。",
            ]
        )
        return "\n".join(lines)


def normalize_message_text(raw_text: str) -> str:
    text = str(raw_text or "").replace("\u3000", " ").strip()
    text = LEADING_BOT_MENTION_RE.sub("", text).strip()
    return " ".join(text.split())
