from __future__ import annotations

import os
import subprocess
from typing import Callable

from ..terminal_renderer import render_terminal_png
from .base import CommandPlugin, CommandSpec, ImageReply, Reply, TextReply


DEFAULT_ALIASES = ("top", "/top")
RENDER_MODES = {"image", "text"}
NAMED_PROCESS_LIMITS = {
    "default": 20,
    "full": 40,
    "brief": 10,
}


class TopPlugin(CommandPlugin):
    name = "top"

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
            summary="查看当前 top 快照",
            examples=("top", "top 30", "top text", "top full"),
        )
        self.binary = str(raw_config.get("binary") or "top")
        self.timeout_seconds = max(1, int(raw_config.get("timeout_seconds") or 15))
        self.width = max(80, int(raw_config.get("width") or 160))
        self.default_process_limit = max(5, int(raw_config.get("default_process_limit") or 20))
        self.max_process_limit = max(self.default_process_limit, int(raw_config.get("max_process_limit") or 60))
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
            process_limit, reply_mode = _parse_options(
                tokens[1:],
                default_reply_mode=self.default_reply_mode,
                default_process_limit=self.default_process_limit,
                max_process_limit=self.max_process_limit,
            )
        except ValueError as exc:
            return TextReply(str(exc))

        command = [self.binary, "-b", "-n", "1", "-w", str(self.width)]
        env = os.environ.copy()
        env["COLUMNS"] = str(self.width)
        env["LINES"] = str(process_limit + 12)

        try:
            completed = self.runner(command, self.timeout_seconds, env)
        except FileNotFoundError:
            return TextReply("未找到 top。请确认系统已安装并允许批处理模式调用。")
        except subprocess.TimeoutExpired:
            return TextReply(f"top 执行超时，超过 {self.timeout_seconds} 秒。")
        except Exception as exc:
            return TextReply(f"top 执行异常：{exc}")

        stdout = str(completed.stdout or "")
        stderr = str(completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout.strip() or f"exit code {completed.returncode}"
            return TextReply(_truncate_text(f"top 执行失败：{detail}", self.max_message_chars))

        formatted = _format_top_snapshot(stdout, process_limit=process_limit)
        if not formatted:
            return TextReply("top 没有返回内容。")

        if reply_mode == "image":
            image_bytes = render_terminal_png(_colorize_top_snapshot(formatted))
            caption = _caption_for(process_limit)
            return ImageReply(content=image_bytes, filename=f"top-{process_limit}.png", caption=caption)

        return TextReply(_truncate_text(formatted, self.max_message_chars))


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


def _parse_options(
    tokens: list[str],
    *,
    default_reply_mode: str,
    default_process_limit: int,
    max_process_limit: int,
) -> tuple[int, str]:
    process_limit = default_process_limit
    reply_mode = default_reply_mode

    for token in tokens:
        lowered = token.lower()
        if lowered in RENDER_MODES:
            reply_mode = lowered
            continue
        if lowered in NAMED_PROCESS_LIMITS:
            process_limit = NAMED_PROCESS_LIMITS[lowered]
            continue
        if lowered.isdigit():
            process_limit = int(lowered)
            continue
        supported_render_modes = ", ".join(sorted(RENDER_MODES))
        raise ValueError(
            "不支持的 top 参数："
            f"{token}。可用视图：default, brief, full, 或数字行数；可用回复模式：{supported_render_modes}。"
        )

    process_limit = max(1, min(process_limit, max_process_limit))
    return process_limit, reply_mode


def _format_top_snapshot(stdout: str, *, process_limit: int) -> str:
    raw_lines = [line.rstrip() for line in stdout.splitlines()]
    non_empty_lines = [line for line in raw_lines if line.strip()]
    if not non_empty_lines:
        return ""

    header_index = -1
    for index, line in enumerate(non_empty_lines):
        if line.lstrip().startswith("PID "):
            header_index = index
            break

    if header_index == -1:
        return "\n".join(non_empty_lines[: process_limit + 6]).strip()

    prefix = non_empty_lines[: header_index + 1]
    process_lines = non_empty_lines[header_index + 1 : header_index + 1 + process_limit]
    return "\n".join(prefix + process_lines).strip()


def _colorize_top_snapshot(text: str) -> str:
    colored_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("top - "):
            colored_lines.append(f"\x1b[96m{line}\x1b[0m")
        elif stripped.startswith("Tasks:"):
            colored_lines.append(f"\x1b[92m{line}\x1b[0m")
        elif stripped.startswith("%Cpu"):
            colored_lines.append(f"\x1b[93m{line}\x1b[0m")
        elif stripped.startswith("MiB Mem") or stripped.startswith("KiB Mem") or stripped.startswith("GiB Mem"):
            colored_lines.append(f"\x1b[94m{line}\x1b[0m")
        elif stripped.startswith("MiB Swap") or stripped.startswith("KiB Swap") or stripped.startswith("GiB Swap"):
            colored_lines.append(f"\x1b[95m{line}\x1b[0m")
        elif stripped.startswith("PID "):
            colored_lines.append(f"\x1b[97m{line}\x1b[0m")
        elif _is_hot_process_line(stripped):
            colored_lines.append(f"\x1b[91m{line}\x1b[0m")
        else:
            colored_lines.append(line)
    return "\n".join(colored_lines)


def _is_hot_process_line(line: str) -> bool:
    parts = line.split()
    if len(parts) < 9:
        return False
    try:
        cpu_value = float(parts[8])
    except ValueError:
        return False
    return cpu_value >= 100.0


def _normalize_aliases(raw_aliases: object) -> tuple[str, ...]:
    if not isinstance(raw_aliases, list) or not raw_aliases:
        return tuple(alias.lower() for alias in DEFAULT_ALIASES)
    aliases = [str(alias).strip().lower() for alias in raw_aliases if str(alias).strip()]
    return tuple(aliases) or tuple(alias.lower() for alias in DEFAULT_ALIASES)


def _normalize_reply_mode(raw_value: object) -> str:
    value = str(raw_value or "image").strip().lower()
    return value if value in RENDER_MODES else "image"


def _caption_for(process_limit: int) -> str:
    return f"top {process_limit}"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    ellipsis = "\n..."
    return text[: max_chars - len(ellipsis)].rstrip() + ellipsis
