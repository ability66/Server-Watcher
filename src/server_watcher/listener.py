from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

from websocket import WebSocketException, WebSocketTimeoutException, create_connection

from .codex_bridge import CodexBridgeManager
from .command_router import CommandRouter
from .config import AppConfig, ConfigError, load_config, resolve_secret, validate_runtime_config
from .plugins import build_plugins
from .plugins.base import ImageReply, TextReply
from .qqbot_client import QQBotClient, QQBotClientError


GROUP_AND_C2C_EVENT_INTENT = 1 << 25
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "server_watcher.yaml"


@dataclass(frozen=True)
class IncomingQQMessage:
    target: str
    target_type: str
    reply_target: str
    reply_target_type: str
    source_msg_id: str | None
    content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Listen for QQ messages and dispatch server watcher plugins.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Config YAML path. Default: configs/server_watcher.yaml",
    )
    parser.add_argument(
        "--retry-seconds",
        type=int,
        default=5,
        help="Seconds to wait before reconnecting after websocket errors. Default: 5",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = resolve_project_path(args.config)

    try:
        config = load_config(config_path)
        validate_runtime_config(config)
    except ConfigError as exc:
        print(f"Invalid config: {exc}", file=sys.stderr)
        return 2

    router = CommandRouter(
        build_plugins(plugin_configs=config.plugins, max_message_chars=config.max_message_chars),
    )
    codex_bridge: CodexBridgeManager | None = None
    retry_seconds = max(1, int(args.retry_seconds))

    first_start = True
    while True:
        try:
            app_id, client_secret, client, access_token, gateway_url = prepare_gateway_connection(config)
        except (ConfigError, QQBotClientError) as exc:
            print(f"Failed to prepare QQBot connection: {exc}", file=sys.stderr, flush=True)
            print(f"Retrying in {retry_seconds}s.", flush=True)
            time.sleep(retry_seconds)
            continue

        if codex_bridge is None:
            raw_codex_plugin = dict(getattr(config.plugins.get("codex"), "raw", {}) or {})
            codex_bridge = CodexBridgeManager.from_plugin_config(
                raw_config=raw_codex_plugin,
                qq_client=client,
                app_id=app_id,
                client_secret=client_secret,
            )

        if first_start:
            print("QQ server watcher is ready.", flush=True)
            print(f"config: {config_path}", flush=True)
            print(f"authorized {config.qq.target_type} openid: {config.qq.target}", flush=True)
            first_start = False
        else:
            print("QQ server watcher reconnected.", flush=True)

        try:
            ws = create_connection(gateway_url, timeout=10, enable_multithread=True)
            run_gateway_session(
                ws=ws,
                client=client,
                access_token=access_token,
                app_id=app_id,
                client_secret=client_secret,
                config=config,
                router=router,
                codex_bridge=codex_bridge,
            )
        except KeyboardInterrupt:
            if codex_bridge is not None:
                codex_bridge.stop_all()
            print("QQ server watcher stopped by keyboard interrupt.", flush=True)
            return 130
        except WebSocketException as exc:
            print(f"WebSocket session failed: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"Unexpected listener error: {exc}", file=sys.stderr, flush=True)

        print(f"QQ server watcher will retry in {retry_seconds}s.", flush=True)
        time.sleep(retry_seconds)


def resolve_project_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def prepare_gateway_connection(config: AppConfig) -> tuple[str, str, QQBotClient, str, str]:
    qq = config.qq
    app_id = resolve_secret(env_name=qq.app_id_env, config_value=qq.app_id, label="app_id")
    client_secret = resolve_secret(
        env_name=qq.client_secret_env,
        config_value=qq.client_secret,
        label="client_secret",
    )
    client = QQBotClient(
        api_base=qq.api_base,
        token_url=qq.token_url,
        timeout_seconds=qq.timeout_seconds,
    )
    access_token = client.get_access_token(app_id=app_id, client_secret=client_secret)
    gateway_url = client.get_gateway_url(access_token=access_token)
    return app_id, client_secret, client, access_token, gateway_url


def run_gateway_session(
    *,
    ws,
    client: QQBotClient,
    access_token: str,
    app_id: str,
    client_secret: str,
    config: AppConfig,
    router: CommandRouter,
    codex_bridge: CodexBridgeManager,
) -> None:
    latest_seq: dict[str, int | None] = {"value": None}
    send_lock = threading.Lock()
    stop_heartbeat = threading.Event()

    def heartbeat_loop(interval_seconds: float) -> None:
        while not stop_heartbeat.wait(interval_seconds):
            try:
                send_json(ws, {"op": 1, "d": latest_seq["value"]}, send_lock)
            except WebSocketException:
                return

    try:
        hello = recv_json(ws)
        heartbeat_interval_ms = int((hello.get("d") or {}).get("heartbeat_interval") or 45000)
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            args=(heartbeat_interval_ms / 1000,),
            daemon=True,
        )
        heartbeat_thread.start()

        identify = {
            "op": 2,
            "d": {
                "token": f"QQBot {access_token}",
                "intents": GROUP_AND_C2C_EVENT_INTENT,
                "shard": [0, 1],
                "properties": {
                    "$os": sys.platform,
                    "$browser": "server-watcher",
                    "$device": "server-watcher",
                },
            },
        }
        send_json(ws, identify, send_lock)

        while True:
            try:
                payload = recv_json(ws)
            except WebSocketTimeoutException:
                continue

            if payload.get("s") is not None:
                latest_seq["value"] = int(payload["s"])

            if payload.get("t") == "READY":
                continue

            message = extract_incoming_message(payload)
            if message is None or not is_authorized_message(message, config):
                continue

            if router.is_help_command(message.content):
                reply = router.dispatch(message.content)
            else:
                bridge_reply = codex_bridge.handle_message(
                    reply_target=message.reply_target,
                    reply_target_type=message.reply_target_type,
                    raw_text=message.content,
                )
                if bridge_reply is not None:
                    reply = bridge_reply
                elif codex_bridge.will_consume_message(reply_target=message.reply_target, raw_text=message.content):
                    print(
                        f"forwarded {message.target_type} message to codex bridge: {message.content!r}",
                        flush=True,
                    )
                    continue
                else:
                    reply = router.dispatch(message.content)
            if reply is None:
                continue

            if isinstance(reply, ImageReply):
                send_result = client.send_media_message(
                    app_id=app_id,
                    client_secret=client_secret,
                    target=message.reply_target,
                    target_type=message.reply_target_type,
                    file_bytes=reply.content,
                    msg_id=message.source_msg_id,
                    caption=reply.caption,
                )
            else:
                text_reply = reply.text if isinstance(reply, TextReply) else str(reply)
                send_result = client.send_message(
                    app_id=app_id,
                    client_secret=client_secret,
                    target=message.reply_target,
                    target_type=message.reply_target_type,
                    message=text_reply,
                    msg_id=message.source_msg_id,
                )
            if not send_result.success:
                print(f"Failed to send QQ reply: {send_result.error}", file=sys.stderr, flush=True)
                continue

            print(
                f"processed {message.target_type} command: {message.content!r}",
                flush=True,
            )
            time.sleep(0.3)
    finally:
        stop_heartbeat.set()
        try:
            ws.close()
        except WebSocketException:
            pass


def send_json(ws, payload: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        ws.send(json.dumps(payload, ensure_ascii=False))


def recv_json(ws) -> dict[str, Any]:
    raw = ws.recv()
    if not raw:
        return {}
    return json.loads(raw)


def extract_incoming_message(payload: dict[str, Any]) -> IncomingQQMessage | None:
    event_type = str(payload.get("t") or "").strip()
    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    content = str(data.get("content") or "").strip()

    if event_type == "C2C_MESSAGE_CREATE":
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        user_openid = str(author.get("user_openid") or "").strip()
        source_msg_id = str(data.get("id") or "").strip() or None
        if not user_openid:
            return None
        return IncomingQQMessage(
            target=user_openid,
            target_type="c2c",
            reply_target=user_openid,
            reply_target_type="c2c",
            source_msg_id=source_msg_id,
            content=content,
        )

    if event_type == "GROUP_AT_MESSAGE_CREATE":
        group_openid = str(data.get("group_openid") or "").strip()
        source_msg_id = str(data.get("id") or "").strip() or None
        if not group_openid:
            return None
        return IncomingQQMessage(
            target=group_openid,
            target_type="group",
            reply_target=group_openid,
            reply_target_type="group",
            source_msg_id=source_msg_id,
            content=content,
        )

    return None


def is_authorized_message(message: IncomingQQMessage, config: AppConfig) -> bool:
    return (
        message.target_type == config.qq.target_type
        and message.target == str(config.qq.target or "").strip()
    )
