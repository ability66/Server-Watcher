from __future__ import annotations

import subprocess
from typing import Callable

from ..terminal_renderer import render_terminal_png
from .base import CommandPlugin, CommandSpec, ImageReply, Reply, TextReply


DEFAULT_ALIASES = ("gpustat", "gpu", "/gpustat", "/gpu")
MODE_ARGS = {
    "default": (),
    "full": ("--show-all", "--show-user", "--show-pid", "--show-cmd"),
    "json": ("--json",),
    "brief": ("--no-processes",),
}
RENDER_MODES = {"image", "text"}


class GpustatPlugin(CommandPlugin):
    name = "gpustat"

    def __init__(
        self,
        *,
        raw_config: dict[str, object],
        max_message_chars: int,
        runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        aliases = _normalize_aliases(raw_config.get("command_aliases"))
        default_args = _normalize_args(raw_config.get("default_args")) or ["--no-color"]
        self.spec = CommandSpec(
            aliases=aliases,
            summary="查看当前 GPU 状态",
            examples=("gpustat", "gpustat full", "gpustat json", "gpustat brief"),
        )
        self.binary = str(raw_config.get("binary") or "gpustat")
        self.timeout_seconds = max(1, int(raw_config.get("timeout_seconds") or 15))
        self.default_args = default_args
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

        command = [self.binary, *_command_base_args(reply_mode, self.default_args), *MODE_ARGS[mode]]
        try:
            completed = self.runner(command, self.timeout_seconds)
        except FileNotFoundError:
            return TextReply("未找到 gpustat。请先在当前环境安装并确认命令可执行。")
        except subprocess.TimeoutExpired:
            return TextReply(f"gpustat 执行超时，超过 {self.timeout_seconds} 秒。")
        except Exception as exc:
            return TextReply(f"gpustat 执行异常：{exc}")

        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            return TextReply(_truncate_text(f"gpustat 执行失败：{detail}", self.max_message_chars))

        if not stdout:
            return TextReply("gpustat 没有返回内容。")

        if reply_mode == "image":
            image_bytes = render_terminal_png(stdout)
            caption = _caption_for(mode)
            return ImageReply(content=image_bytes, filename=f"gpustat-{mode}.png", caption=caption)

        return TextReply(_truncate_text(stdout, self.max_message_chars))


def _run_subprocess(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _parse_options(tokens: list[str], *, default_reply_mode: str) -> tuple[str, str]:
    mode = "default"
    reply_mode = default_reply_mode
    for token in tokens:
        lowered = token.lower()
        if lowered in MODE_ARGS:
            mode = lowered
            continue
        if lowered in RENDER_MODES:
            reply_mode = lowered
            continue
        supported_modes = ", ".join(MODE_ARGS)
        supported_render_modes = ", ".join(sorted(RENDER_MODES))
        raise ValueError(
            f"不支持的 gpustat 参数：{token}。可用视图：{supported_modes}；可用回复模式：{supported_render_modes}。"
        )
    return mode, reply_mode


def _command_base_args(reply_mode: str, default_args: list[str]) -> list[str]:
    args = [arg for arg in default_args if arg not in {"--no-color", "--force-color"}]
    if reply_mode == "image":
        return [*args, "--force-color"]
    return [*args, "--no-color"]


def _normalize_reply_mode(raw_value: object) -> str:
    value = str(raw_value or "image").strip().lower()
    return value if value in RENDER_MODES else "image"


def _caption_for(mode: str) -> str:
    if mode == "default":
        return "gpustat"
    return f"gpustat {mode}"


def _normalize_aliases(raw_aliases: object) -> tuple[str, ...]:
    if not isinstance(raw_aliases, list) or not raw_aliases:
        return tuple(alias.lower() for alias in DEFAULT_ALIASES)
    aliases = [str(alias).strip().lower() for alias in raw_aliases if str(alias).strip()]
    return tuple(aliases) or tuple(alias.lower() for alias in DEFAULT_ALIASES)


def _normalize_args(raw_args: object) -> list[str]:
    if not isinstance(raw_args, list):
        return []
    return [str(arg).strip() for arg in raw_args if str(arg).strip()]


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    ellipsis = "\n..."
    return text[: max_chars - len(ellipsis)].rstrip() + ellipsis
