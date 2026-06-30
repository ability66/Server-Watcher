from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
from typing import Callable

from .command_router import normalize_message_text
from .plugins.base import TextReply
from .qqbot_client import QQBotClient


CONTROL_PREFIXES = ("qqcodex", "/qqcodex")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SPECIAL_KEYS = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "Space",
    "backspace": "BSpace",
    "delete": "DC",
    "pgup": "PageUp",
    "pgdn": "PageDown",
    "home": "Home",
    "end": "End",
}


@dataclass(frozen=True)
class CodexBridgeConfig:
    enabled: bool
    binary: str
    workdir: str
    session_prefix: str
    poll_seconds: float
    capture_lines: int
    max_chunk_chars: int
    startup_timeout_seconds: int
    command_timeout_seconds: int
    start_aliases: tuple[str, ...]
    auto_start_slash_commands: tuple[str, ...]
    extra_args: tuple[str, ...]


@dataclass
class CodexSessionState:
    session_name: str
    reply_target: str
    reply_target_type: str
    workdir: str
    stop_event: threading.Event
    watcher_thread: threading.Thread
    last_snapshot: str = ""
    started_at: float = 0.0


class CodexBridgeManager:
    def __init__(
        self,
        *,
        config: CodexBridgeConfig,
        qq_client: QQBotClient,
        app_id: str,
        client_secret: str,
        send_delay_seconds: float = 0.3,
    ) -> None:
        self.config = config
        self.qq_client = qq_client
        self.app_id = app_id
        self.client_secret = client_secret
        self.send_delay_seconds = send_delay_seconds
        self._sessions: dict[str, CodexSessionState] = {}
        self._preferred_workdirs: dict[str, str] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_plugin_config(
        cls,
        *,
        raw_config: dict[str, object],
        qq_client: QQBotClient,
        app_id: str,
        client_secret: str,
    ) -> "CodexBridgeManager":
        config = CodexBridgeConfig(
            enabled=bool(raw_config.get("enabled", True)),
            binary=str(raw_config.get("binary") or "codex"),
            workdir=str(raw_config.get("workdir") or "/raid/lhk"),
            session_prefix=str(raw_config.get("session_prefix") or "qq-codex"),
            poll_seconds=max(0.5, float(raw_config.get("poll_seconds") or 1.5)),
            capture_lines=max(50, int(raw_config.get("capture_lines") or 300)),
            max_chunk_chars=max(300, int(raw_config.get("max_chunk_chars") or 1800)),
            startup_timeout_seconds=max(5, int(raw_config.get("startup_timeout_seconds") or 20)),
            command_timeout_seconds=max(5, int(raw_config.get("command_timeout_seconds") or 10)),
            start_aliases=_normalize_aliases(raw_config.get("start_aliases"), default=("codex", "/codex")),
            auto_start_slash_commands=_normalize_aliases(
                raw_config.get("auto_start_slash_commands"),
                default=("/permissions", "/resume"),
            ),
            extra_args=_normalize_string_tuple(raw_config.get("extra_args")),
        )
        return cls(
            config=config,
            qq_client=qq_client,
            app_id=app_id,
            client_secret=client_secret,
        )

    def handle_message(self, *, reply_target: str, reply_target_type: str, raw_text: str) -> TextReply | None:
        if not self.config.enabled:
            return None

        text = normalize_message_text(raw_text)
        if not text:
            return None

        session = self._get_or_attach_session(reply_target=reply_target, reply_target_type=reply_target_type)

        builtin_reply = self._handle_local_builtin(
            reply_target=reply_target,
            reply_target_type=reply_target_type,
            text=text,
        )
        if builtin_reply is not None:
            return builtin_reply

        control_reply = self._handle_control_command(
            reply_target=reply_target,
            reply_target_type=reply_target_type,
            text=text,
        )
        if control_reply is not None:
            return control_reply

        session = self._get_session(reply_target)
        if session is not None:
            if text.split()[0].lower() in self.config.start_aliases and len(text.split()) == 1:
                return TextReply("Codex 会话已经在运行。直接继续发消息即可，退出用 `qqcodex stop`。")
            try:
                self._send_input(session.session_name, text)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
                self._stop_session(reply_target, send_notice=False)
                return TextReply(f"向 Codex 会话发送输入失败：{detail}")
            return None

        start_match = self._parse_start_command(text)
        if start_match is None:
            first_token = text.split()[0].lower()
            if first_token in self.config.auto_start_slash_commands:
                start_match = text
            else:
                return None

        initial_input = start_match
        try:
            self._start_session(
                reply_target=reply_target,
                reply_target_type=reply_target_type,
                initial_input=initial_input,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            return TextReply(f"启动 Codex 会话失败：{detail}")
        if text.split()[0].lower() in self.config.auto_start_slash_commands:
            return TextReply(f"Codex 会话已启动，并已转发 `{text.split()[0]}`。")

        return TextReply(
            "Codex 会话已启动。后续消息会直接转发到服务器上的 Codex。"
            "Codex 自己的 `/` 指令会原样生效，退出桥接用 `qqcodex stop`。"
        )

    def will_consume_message(self, *, reply_target: str, raw_text: str) -> bool:
        if not self.config.enabled:
            return False
        text = normalize_message_text(raw_text)
        if not text:
            return False
        first = text.split()[0].lower()
        if first in CONTROL_PREFIXES:
            return True
        if self._get_session(reply_target) is not None:
            return True
        if self._session_exists(self._session_name_for(reply_target)):
            return True
        if first in self.config.auto_start_slash_commands:
            return True
        return first in self.config.start_aliases

    def stop_all(self) -> None:
        with self._lock:
            targets = list(self._sessions)
        for reply_target in targets:
            self._stop_session(reply_target, send_notice=False)

    def _handle_local_builtin(self, *, reply_target: str, reply_target_type: str, text: str) -> TextReply | None:
        try:
            tokens = shlex.split(text)
        except ValueError as exc:
            return TextReply(f"命令解析失败：{exc}")

        if not tokens:
            return None

        command = tokens[0].lower()
        if command == "pwd":
            return TextReply(self._current_workdir(reply_target))

        if command == "ls":
            return self._handle_ls_command(reply_target=reply_target, args=tokens[1:])

        if command == "cd":
            return self._handle_cd_command(
                reply_target=reply_target,
                reply_target_type=reply_target_type,
                args=tokens[1:],
            )

        return None

    def _handle_ls_command(self, *, reply_target: str, args: list[str]) -> TextReply:
        command = ["ls", "--color=never"]
        current_workdir = self._current_workdir(reply_target)
        explicit_paths = [arg for arg in args if not arg.startswith("-")]
        for arg in args:
            if arg.startswith("-"):
                command.append(arg)
        for path_text in explicit_paths:
            command.append(self._resolve_path_text(reply_target, path_text))

        if not explicit_paths:
            command.append(current_workdir)

        try:
            completed = subprocess.run(
                command,
                cwd=current_workdir,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.config.command_timeout_seconds,
            )
        except FileNotFoundError:
            return TextReply("未找到 ls。")
        except subprocess.TimeoutExpired:
            return TextReply(f"ls 执行超时，超过 {self.config.command_timeout_seconds} 秒。")

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            return TextReply(f"ls 执行失败：{detail}")
        if not stdout:
            return TextReply("(空目录)")
        return TextReply(_truncate_text(stdout, self.config.max_chunk_chars))

    def _handle_cd_command(self, *, reply_target: str, reply_target_type: str, args: list[str]) -> TextReply:
        if len(args) > 1:
            return TextReply("cd 只支持一个路径参数，例如 `cd /raid/lhk/TradePilot`。")

        target_text = args[0] if args else self.config.workdir
        try:
            resolved = self._resolve_directory(reply_target, target_text)
        except ValueError as exc:
            return TextReply(str(exc))

        had_session = self._get_session(reply_target) is not None or self._session_exists(self._session_name_for(reply_target))
        if had_session:
            self._stop_session(reply_target, send_notice=False)
            try:
                self._start_session(
                    reply_target=reply_target,
                    reply_target_type=reply_target_type,
                    initial_input=None,
                    workdir=str(resolved),
                )
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
                return TextReply(f"切换目录后重启 Codex 会话失败：{detail}")
            return TextReply(f"工作目录已切换到：{resolved}\nCodex 会话已在新目录重启。")

        self._set_workdir(reply_target, str(resolved))
        return TextReply(f"工作目录已切换到：{resolved}")

    def _handle_control_command(self, *, reply_target: str, reply_target_type: str, text: str) -> TextReply | None:
        parts = text.split(maxsplit=1)
        first = parts[0].lower()
        if first not in CONTROL_PREFIXES:
            return None

        command = parts[1].strip() if len(parts) > 1 else "status"
        lowered_command = command.lower()
        session = self._get_session(reply_target)

        if lowered_command.startswith("key "):
            if session is None:
                return TextReply("当前没有活动中的 Codex 会话。")
            key_name = command.split(maxsplit=1)[1].strip().lower()
            key_token = SPECIAL_KEYS.get(key_name)
            if key_token is None:
                supported = ", ".join(sorted(SPECIAL_KEYS))
                return TextReply(f"不支持的按键：{key_name}。支持：{supported}。")
            try:
                self._send_special_key(session.session_name, key_token)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
                return TextReply(f"发送按键失败：{detail}")
            return TextReply(f"已发送按键：{key_name}")

        if lowered_command.startswith("pick "):
            if session is None:
                return TextReply("当前没有活动中的 Codex 会话。")
            choice_text = command.split(maxsplit=1)[1].strip()
            if not choice_text.isdigit() or int(choice_text) <= 0:
                return TextReply("`qqcodex pick` 后面必须是正整数，例如 `qqcodex pick 2`。")
            try:
                self._pick_index(session.session_name, int(choice_text))
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
                return TextReply(f"执行选择失败：{detail}")
            return TextReply(f"已尝试选择第 {choice_text} 项。")

        if lowered_command.startswith("resume"):
            remainder = command.split(maxsplit=1)[1].strip() if len(command.split(maxsplit=1)) > 1 else "last"
            resume_args = self._parse_resume_command(remainder)
            if resume_args is None:
                return TextReply("用法：`qqcodex resume last` 或 `qqcodex resume <session_id>`。")
            effective_reply_target_type = reply_target_type if session is None else session.reply_target_type
            if session is not None:
                self._stop_session(reply_target, send_notice=False)
            try:
                self._start_session(
                    reply_target=reply_target,
                    reply_target_type=effective_reply_target_type,
                    initial_input=None,
                    launch_args=["resume", *resume_args],
                )
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or str(exc)).strip()
                return TextReply(f"恢复 Codex 会话失败：{detail}")
            return TextReply("Codex resume 会话已启动。后续消息会直接转发到该会话。")

        if lowered_command == "status":
            if session is None:
                return TextReply("当前没有活动中的 Codex 会话。发送 `codex` 可启动。")
            return TextReply(f"Codex 会话运行中：{session.session_name}，工作目录：{session.workdir}")

        if lowered_command in {"stop", "exit"}:
            stopped = self._stop_session(reply_target, send_notice=False)
            if not stopped:
                return TextReply("当前没有活动中的 Codex 会话。")
            return TextReply("Codex 会话已停止。")

        if lowered_command in {"restart", "reset"}:
            existing = session
            effective_reply_target_type = reply_target_type if existing is None else existing.reply_target_type
            self._stop_session(reply_target, send_notice=False)
            self._start_session(
                reply_target=reply_target,
                reply_target_type=effective_reply_target_type,
                initial_input=None,
            )
            return TextReply("Codex 会话已重启。")

        return TextReply(
            "支持的桥接控制命令：`qqcodex status`、`qqcodex stop`、`qqcodex restart`、"
            "`qqcodex key down|up|enter|esc|tab`、`qqcodex pick 2`、`qqcodex resume last`。"
        )

    def _parse_start_command(self, text: str) -> str | None:
        parts = text.split(maxsplit=1)
        first = parts[0].lower()
        if first not in self.config.start_aliases:
            return None
        if len(parts) == 1:
            return ""
        return parts[1]

    def _get_or_attach_session(self, *, reply_target: str, reply_target_type: str) -> CodexSessionState | None:
        existing = self._get_session(reply_target)
        if existing is not None:
            return existing

        session_name = self._session_name_for(reply_target)
        if not self._session_exists(session_name):
            return None

        stop_event = threading.Event()
        attached_workdir = self._tmux_current_path(session_name) or self._preferred_workdirs.get(reply_target) or self.config.workdir
        state = CodexSessionState(
            session_name=session_name,
            reply_target=reply_target,
            reply_target_type=reply_target_type,
            workdir=attached_workdir,
            stop_event=stop_event,
            watcher_thread=threading.Thread(target=self._watch_session, args=(reply_target,), daemon=True),
            started_at=time.time(),
        )
        state.last_snapshot = self._capture_snapshot(session_name)
        with self._lock:
            self._sessions[reply_target] = state
            self._preferred_workdirs[reply_target] = attached_workdir
        state.watcher_thread.start()
        return state

    def _start_session(
        self,
        *,
        reply_target: str,
        reply_target_type: str,
        initial_input: str | None,
        launch_args: list[str] | None = None,
        workdir: str | None = None,
    ) -> None:
        session_name = self._session_name_for(reply_target)
        self._kill_tmux_session(session_name)
        effective_workdir = workdir or self._current_workdir(reply_target)
        command = self._build_start_command(launch_args=launch_args, workdir=effective_workdir)
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                effective_workdir,
                command,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        stop_event = threading.Event()
        state = CodexSessionState(
            session_name=session_name,
            reply_target=reply_target,
            reply_target_type=reply_target_type,
            workdir=effective_workdir,
            stop_event=stop_event,
            watcher_thread=threading.Thread(target=self._watch_session, args=(reply_target,), daemon=True),
            started_at=time.time(),
        )
        with self._lock:
            self._sessions[reply_target] = state
        self._set_workdir(reply_target, effective_workdir)
        state.watcher_thread.start()

        if initial_input:
            time.sleep(1.0)
            self._send_input(session_name, initial_input)

    def _stop_session(self, reply_target: str, *, send_notice: bool) -> bool:
        with self._lock:
            state = self._sessions.pop(reply_target, None)
        if state is None:
            return False

        state.stop_event.set()
        self._kill_tmux_session(state.session_name)
        if send_notice:
            self._send_text(state.reply_target, state.reply_target_type, "Codex 会话已结束。")
        return True

    def _watch_session(self, reply_target: str) -> None:
        while True:
            session = self._get_session(reply_target)
            if session is None:
                return
            if session.stop_event.wait(self.config.poll_seconds):
                return
            if not self._session_exists(session.session_name):
                self._stop_session(reply_target, send_notice=True)
                return

            snapshot = self._capture_snapshot(session.session_name)
            if not snapshot or snapshot == session.last_snapshot:
                continue

            delta = compute_terminal_delta(session.last_snapshot, snapshot)
            session.last_snapshot = snapshot
            if not delta:
                continue

            for chunk in split_for_qq(delta, max_chars=self.config.max_chunk_chars):
                self._send_text(session.reply_target, session.reply_target_type, chunk)
                time.sleep(self.send_delay_seconds)

    def _capture_snapshot(self, session_name: str) -> str:
        completed = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                session_name,
                "-p",
                "-S",
                f"-{self.config.capture_lines}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return ""
        return normalize_terminal_output(completed.stdout)

    def _send_input(self, session_name: str, text: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", text],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            check=True,
            capture_output=True,
            text=True,
        )

    def _send_special_key(self, session_name: str, key_token: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, key_token],
            check=True,
            capture_output=True,
            text=True,
        )

    def _pick_index(self, session_name: str, index: int) -> None:
        for _ in range(max(0, index - 1)):
            self._send_special_key(session_name, "Down")
            time.sleep(0.05)
        self._send_special_key(session_name, "Enter")

    def _send_text(self, target: str, target_type: str, text: str) -> None:
        self.qq_client.send_message(
            app_id=self.app_id,
            client_secret=self.client_secret,
            target=target,
            target_type=target_type,
            message=text,
        )

    def _build_start_command(self, *, launch_args: list[str] | None = None, workdir: str) -> str:
        command = [
            self.config.binary,
            *(launch_args or []),
            "--no-alt-screen",
            "-a",
            "never",
            "-s",
            "danger-full-access",
            "-C",
            workdir,
            *self.config.extra_args,
        ]
        return shlex.join(command)

    def _parse_resume_command(self, remainder: str) -> list[str] | None:
        normalized = remainder.strip()
        if not normalized or normalized == "last":
            return ["--last"]
        if normalized == "picker":
            return []
        return [normalized]

    def _current_workdir(self, reply_target: str) -> str:
        session = self._get_session(reply_target)
        if session is not None:
            return session.workdir
        session_name = self._session_name_for(reply_target)
        if self._session_exists(session_name):
            current_path = self._tmux_current_path(session_name)
            if current_path:
                return current_path
        preferred = self._preferred_workdirs.get(reply_target)
        if preferred:
            return preferred
        return self.config.workdir

    def _set_workdir(self, reply_target: str, workdir: str) -> None:
        self._preferred_workdirs[reply_target] = workdir
        session = self._get_session(reply_target)
        if session is not None:
            session.workdir = workdir

    def _resolve_path_text(self, reply_target: str, path_text: str) -> str:
        base = Path(self._current_workdir(reply_target))
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return str(candidate.resolve())

    def _resolve_directory(self, reply_target: str, path_text: str) -> Path:
        candidate = Path(self._resolve_path_text(reply_target, path_text))
        if not candidate.exists():
            raise ValueError(f"目录不存在：{candidate}")
        if not candidate.is_dir():
            raise ValueError(f"不是目录：{candidate}")
        return candidate

    def _session_name_for(self, reply_target: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]", "", reply_target)[:24] or "user"
        return f"{self.config.session_prefix}-{normalized}"

    def _get_session(self, reply_target: str) -> CodexSessionState | None:
        with self._lock:
            return self._sessions.get(reply_target)

    def _session_exists(self, session_name: str) -> bool:
        completed = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0

    def _tmux_current_path(self, session_name: str) -> str | None:
        completed = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_name, "#{pane_current_path}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        value = (completed.stdout or "").strip()
        return value or None

    def _kill_tmux_session(self, session_name: str) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            check=False,
            capture_output=True,
            text=True,
        )


def normalize_terminal_output(raw_text: str) -> str:
    text = raw_text.replace("\r", "\n")
    text = ANSI_ESCAPE_RE.sub("", text)
    text = CONTROL_CHAR_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    compacted: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        compacted.append(line)
    return "\n".join(compacted).strip()


def compute_terminal_delta(previous: str, current: str) -> str:
    if not previous:
        return tail_lines(current, 40)
    if current.startswith(previous):
        return current[len(previous) :].strip()

    prev_lines = previous.splitlines()
    curr_lines = current.splitlines()
    common = 0
    for left, right in zip(prev_lines, curr_lines):
        if left != right:
            break
        common += 1

    delta_lines = curr_lines[common:]
    if delta_lines and common >= max(1, len(prev_lines) - 5):
        return "\n".join(delta_lines).strip()
    return "[Codex 屏幕更新]\n" + tail_lines(current, 40)


def split_for_qq(text: str, *, max_chars: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in normalized.splitlines():
        line = line.rstrip()
        extra = len(line) + (1 if current_lines else 0)
        if current_lines and current_len + extra > max_chars:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = len(line)
            continue
        current_lines.append(line)
        current_len += extra

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def tail_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-limit:]).strip()


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    ellipsis = "\n..."
    return text[: max_chars - len(ellipsis)].rstrip() + ellipsis


def _normalize_aliases(raw_aliases: object, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw_aliases, list) or not raw_aliases:
        return tuple(alias.lower() for alias in default)
    aliases = [str(alias).strip().lower() for alias in raw_aliases if str(alias).strip()]
    return tuple(aliases) or tuple(alias.lower() for alias in default)


def _normalize_string_tuple(raw_values: object) -> tuple[str, ...]:
    if not isinstance(raw_values, list):
        return ()
    return tuple(str(value).strip() for value in raw_values if str(value).strip())
