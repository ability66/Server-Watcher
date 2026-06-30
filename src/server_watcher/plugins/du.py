from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Callable

from ..terminal_renderer import TerminalTheme, render_terminal_png
from .base import CommandPlugin, CommandSpec, ImageReply, Reply, TextReply


DEFAULT_ALIASES = ("du", "/du")
RENDER_MODES = {"image", "text"}
SORT_ORDERS = {"asc", "desc"}


class DuPlugin(CommandPlugin):
    name = "du"

    def __init__(
        self,
        *,
        raw_config: dict[str, object],
        max_message_chars: int,
        runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        aliases = _normalize_aliases(raw_config.get("command_aliases"))
        self.spec = CommandSpec(
            aliases=aliases,
            summary="查看目录占用摘要",
            examples=("du -h /raid/lhk", "du -h /raid/lhk sort", "du -h /raid/lhk sort asc"),
        )
        self.binary = str(raw_config.get("binary") or "du")
        self.timeout_seconds = max(1, int(raw_config.get("timeout_seconds") or 20))
        self.default_path = str(raw_config.get("default_path") or ".")
        self.default_depth = max(0, int(raw_config.get("default_depth") or 1))
        self.max_depth = max(self.default_depth, int(raw_config.get("max_depth") or 3))
        self.default_entries = max(5, int(raw_config.get("default_entries") or 20))
        self.max_entries = max(self.default_entries, int(raw_config.get("max_entries") or 40))
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
            target_path, depth, sort_order, reply_mode = _parse_options(
                tokens[1:],
                default_path=self.default_path,
                default_depth=self.default_depth,
                max_depth=self.max_depth,
                default_reply_mode=self.default_reply_mode,
            )
        except ValueError as exc:
            return TextReply(str(exc))

        command = [self.binary, "-B1", f"-d{depth}", target_path]
        try:
            completed = self.runner(command, self.timeout_seconds)
        except FileNotFoundError:
            return TextReply("未找到 du。请确认系统已安装并允许命令行调用。")
        except subprocess.TimeoutExpired:
            return TextReply(f"du 执行超时，超过 {self.timeout_seconds} 秒。")
        except Exception as exc:
            return TextReply(f"du 执行异常：{exc}")

        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        if completed.returncode != 0 and not stdout:
            detail = stderr or f"exit code {completed.returncode}"
            return TextReply(_truncate_text(f"du 执行失败：{detail}", self.max_message_chars))
        if not stdout:
            return TextReply("du 没有返回内容。")

        formatted = _format_du_output(
            stdout,
            target_path=target_path,
            depth=depth,
            entries=self.default_entries,
            max_entries=self.max_entries,
            sort_order=sort_order,
            stderr=stderr,
        )
        if reply_mode == "image":
            return ImageReply(
                content=render_terminal_png(
                    _colorize_du_output(formatted),
                    theme=TerminalTheme(background="#f6f8fb", foreground="#101828"),
                ),
                filename="du-summary.png",
                caption=f"du -h {target_path}",
            )
        return TextReply(_truncate_text(formatted, self.max_message_chars))


def _run_subprocess(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _parse_options(
    tokens: list[str],
    *,
    default_path: str,
    default_depth: int,
    max_depth: int,
    default_reply_mode: str,
) -> tuple[str, int, str, str]:
    path_text = default_path
    path_explicitly_set = False
    depth = default_depth
    sort_order = "desc"
    reply_mode = default_reply_mode
    expect_sort_order = False
    expect_depth_value = False

    for token in tokens:
        lowered = token.lower()
        if expect_depth_value:
            if not lowered.isdigit():
                raise ValueError("du 的 `--max-depth` 或 `-d` 后面必须跟数字。")
            depth = int(lowered)
            expect_depth_value = False
            continue
        if lowered == "-h":
            continue
        if lowered in RENDER_MODES:
            reply_mode = lowered
            continue
        if lowered.startswith("--max-depth="):
            value = lowered.split("=", 1)[1]
            if not value.isdigit():
                raise ValueError("du 的 `--max-depth=...` 必须是数字。")
            depth = int(value)
            continue
        if lowered == "--max-depth":
            expect_depth_value = True
            continue
        if lowered.startswith("-d") and len(lowered) > 2:
            value = lowered[2:]
            if not value.isdigit():
                raise ValueError("du 的 `-d...` 必须是数字。")
            depth = int(value)
            continue
        if lowered == "-d":
            expect_depth_value = True
            continue
        if lowered == "sort":
            expect_sort_order = True
            continue
        if lowered in SORT_ORDERS:
            if not expect_sort_order:
                raise ValueError("du 的排序参数请写成 `sort asc` 或 `sort desc`。")
            sort_order = lowered
            expect_sort_order = False
            continue
        if path_explicitly_set:
            supported_render_modes = ", ".join(sorted(RENDER_MODES))
            raise ValueError(
                "不支持的 du 参数："
                f"{token}。支持 `du -h 路径`、`--max-depth=N`、`sort asc|desc`，回复模式：{supported_render_modes}。"
            )
        path_text = token
        path_explicitly_set = True

    if expect_depth_value:
        raise ValueError("du 的 `--max-depth` 或 `-d` 后面必须跟数字。")

    depth = max(0, min(depth, max_depth))
    return path_text, depth, sort_order, reply_mode


def _format_du_output(
    stdout: str,
    *,
    target_path: str,
    depth: int,
    entries: int,
    max_entries: int,
    sort_order: str,
    stderr: str,
) -> str:
    rows: list[tuple[int, str]] = []
    for line in stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            size_bytes = int(parts[0].strip())
        except ValueError:
            continue
        rows.append((size_bytes, parts[1].strip()))

    if not rows:
        return ""

    resolved_target = str(Path(target_path).expanduser())
    root_row = rows[-1]
    child_rows = [row for row in rows[:-1] if row[1] != root_row[1]]
    reverse = sort_order != "asc"
    child_rows.sort(key=lambda item: item[0], reverse=reverse)
    selected_rows = child_rows[: min(entries, max_entries)]

    lines = [f"$ du -h {resolved_target} --max-depth={depth} | sort -h{'r' if reverse else ''}", ""]
    lines.append(f"{_humanize_bytes(root_row[0]):>8}  {root_row[1]}")
    for size_bytes, row_path in selected_rows:
        lines.append(f"{_humanize_bytes(size_bytes):>8}  {row_path}")
    if stderr:
        lines.extend(["", f"warning: {stderr.splitlines()[0][:160]}"])
    return "\n".join(lines).strip()


def _colorize_du_output(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text

    colored_lines: list[str] = []
    for index, line in enumerate(lines):
        if index == 0:
            colored_lines.append(f"\x1b[34m{line}\x1b[0m")
        elif index == 2:
            colored_lines.append(f"\x1b[35m{line}\x1b[0m")
        elif line.startswith("warning:"):
            colored_lines.append(f"\x1b[31m{line}\x1b[0m")
        else:
            colored_lines.append(line)
    return "\n".join(colored_lines)


def _humanize_bytes(size_bytes: int) -> str:
    units = ["B", "K", "M", "G", "T", "P"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}P"


def _normalize_aliases(raw_aliases: object) -> tuple[str, ...]:
    if not isinstance(raw_aliases, list) or not raw_aliases:
        return tuple(alias.lower() for alias in DEFAULT_ALIASES)
    aliases = [str(alias).strip().lower() for alias in raw_aliases if str(alias).strip()]
    return tuple(aliases) or tuple(alias.lower() for alias in DEFAULT_ALIASES)


def _normalize_reply_mode(raw_value: object) -> str:
    value = str(raw_value or "text").strip().lower()
    return value if value in RENDER_MODES else "text"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    ellipsis = "\n..."
    return text[: max_chars - len(ellipsis)].rstrip() + ellipsis
