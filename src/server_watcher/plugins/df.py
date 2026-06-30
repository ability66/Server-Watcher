from __future__ import annotations

import os
import re
import subprocess
from typing import Callable

from ..terminal_renderer import render_terminal_png
from .base import CommandPlugin, CommandSpec, ImageReply, Reply, TextReply


DEFAULT_ALIASES = ("df", "/df")
RENDER_MODES = {"image", "text"}
MODE_ARGS = {
    "default": ("-h",),
    "full": ("-hT",),
    "inode": ("-hi",),
}
TOKEN_TO_MODE = {
    "-h": "default",
    "-ht": "full",
    "-th": "full",
    "-i": "inode",
    "-hi": "inode",
    "-ih": "inode",
}
PERCENT_RE = re.compile(r"(\d+)%")


class DfPlugin(CommandPlugin):
    name = "df"

    def __init__(
        self,
        *,
        raw_config: dict[str, object],
        max_message_chars: int,
        runner: Callable[[list[str], int, dict[str, str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        aliases = _normalize_aliases(raw_config.get("command_aliases"))
        self.spec = CommandSpec(
            aliases=aliases,
            summary="查看当前磁盘挂载使用情况",
            examples=("df", "df -h", "df inode", "df text"),
        )
        self.binary = str(raw_config.get("binary") or "df")
        self.timeout_seconds = max(1, int(raw_config.get("timeout_seconds") or 15))
        self.width = max(80, int(raw_config.get("width") or 160))
        self.default_reply_mode = _normalize_reply_mode(raw_config.get("reply_mode"))
        self.max_message_chars = max(200, int(raw_config.get("max_chars") or max_message_chars))
        self.runner = runner or _run_subprocess

    def handle(self, command_text: str) -> Reply | None:
        tokens = command_text.split()
        if not tokens:
            return None

        first_token = tokens[0].lower()
        if first_token not in self.spec.aliases:
            return None

        try:
            mode, reply_mode = _parse_options(tokens[1:], default_reply_mode=self.default_reply_mode)
        except ValueError as exc:
            return TextReply(str(exc))

        command = [self.binary, *MODE_ARGS[mode]]
        env = os.environ.copy()
        env["COLUMNS"] = str(self.width)

        try:
            completed = self.runner(command, self.timeout_seconds, env)
        except FileNotFoundError:
            return TextReply("未找到 df。请确认系统已安装并允许命令行调用。")
        except subprocess.TimeoutExpired:
            return TextReply(f"df 执行超时，超过 {self.timeout_seconds} 秒。")
        except Exception as exc:
            return TextReply(f"df 执行异常：{exc}")

        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            return TextReply(_truncate_text(f"df 执行失败：{detail}", self.max_message_chars))
        if not stdout:
            return TextReply("df 没有返回内容。")

        if reply_mode == "image":
            return ImageReply(
                content=render_terminal_png(_colorize_df_output(stdout)),
                filename=f"df-{mode}.png",
                caption=_caption_for(mode),
            )
        return TextReply(_truncate_text(stdout, self.max_message_chars))


def _run_subprocess(
    command: list[str],
    timeout_seconds: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        env=env,
    )


def _parse_options(tokens: list[str], *, default_reply_mode: str) -> tuple[str, str]:
    mode = "default"
    reply_mode = default_reply_mode
    for token in tokens:
        lowered = token.lower()
        if lowered in MODE_ARGS:
            mode = lowered
            continue
        if lowered in TOKEN_TO_MODE:
            mode = TOKEN_TO_MODE[lowered]
            continue
        if lowered in RENDER_MODES:
            reply_mode = lowered
            continue
        supported_modes = ", ".join(MODE_ARGS)
        supported_render_modes = ", ".join(sorted(RENDER_MODES))
        raise ValueError(
            f"不支持的 df 参数：{token}。可用视图：{supported_modes}；可用回复模式：{supported_render_modes}。"
        )
    return mode, reply_mode


def _colorize_df_output(text: str) -> str:
    lines = text.splitlines()
    colored_lines: list[str] = []
    for index, line in enumerate(lines):
        if index == 0:
            colored_lines.append(f"\x1b[97m{line}\x1b[0m")
            continue
        match = PERCENT_RE.search(line)
        if not match:
            colored_lines.append(line)
            continue
        usage = int(match.group(1))
        if usage >= 90:
            color = 91
        elif usage >= 80:
            color = 93
        else:
            color = 92
        colored_lines.append(f"\x1b[{color}m{line}\x1b[0m")
    return "\n".join(colored_lines)


def _caption_for(mode: str) -> str:
    if mode == "default":
        return "df"
    return f"df {mode}"


def _normalize_aliases(raw_aliases: object) -> tuple[str, ...]:
    if not isinstance(raw_aliases, list) or not raw_aliases:
        return tuple(alias.lower() for alias in DEFAULT_ALIASES)
    aliases = [str(alias).strip().lower() for alias in raw_aliases if str(alias).strip()]
    return tuple(aliases) or tuple(alias.lower() for alias in DEFAULT_ALIASES)


def _normalize_reply_mode(raw_value: object) -> str:
    value = str(raw_value or "image").strip().lower()
    return value if value in RENDER_MODES else "image"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    ellipsis = "\n..."
    return text[: max_chars - len(ellipsis)].rstrip() + ellipsis
