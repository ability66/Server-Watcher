from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class QQBotResult:
    success: bool
    endpoint: str
    status: int | None
    response: dict[str, Any] | list[Any] | str | None = None
    error: str = ""


@dataclass(frozen=True)
class QQBotMediaUploadResult:
    file_info: str
    file_uuid: str | None
    ttl: int | None
    response: dict[str, Any] | list[Any] | str | None = None


class QQBotClientError(RuntimeError):
    pass


class QQBotClient:
    def __init__(
        self,
        *,
        api_base: str,
        token_url: str,
        timeout_seconds: int = 30,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self.timeout_seconds = timeout_seconds

    def get_access_token(self, *, app_id: str, client_secret: str) -> str:
        payload = {"appId": app_id.strip(), "clientSecret": client_secret}
        response = self._post_json(
            self.token_url,
            headers={"Content-Type": "application/json"},
            payload=payload,
        )
        if not isinstance(response, dict) or not response.get("access_token"):
            raise QQBotClientError(f"QQBot token response did not include access_token: {response}")
        return str(response["access_token"])

    def get_gateway_url(self, *, access_token: str) -> str:
        response = self._get_json(
            f"{self.api_base}/gateway",
            headers={"Authorization": f"QQBot {access_token}"},
        )
        if not isinstance(response, dict) or not response.get("url"):
            raise QQBotClientError(f"QQBot gateway response did not include url: {response}")
        return str(response["url"])

    def send_message(
        self,
        *,
        app_id: str,
        client_secret: str,
        target: str,
        target_type: str,
        message: str,
        msg_id: str | None = None,
    ) -> QQBotResult:
        access_token = self.get_access_token(app_id=app_id, client_secret=client_secret)
        endpoint = self.message_endpoint(target=target, target_type=target_type)
        payload = self.message_payload(message=message, msg_id=msg_id)
        return self._send_payload(
            access_token=access_token,
            endpoint=endpoint,
            payload=payload,
        )

    def send_media_message(
        self,
        *,
        app_id: str,
        client_secret: str,
        target: str,
        target_type: str,
        file_bytes: bytes,
        msg_id: str | None = None,
        caption: str | None = None,
    ) -> QQBotResult:
        access_token = self.get_access_token(app_id=app_id, client_secret=client_secret)
        upload = self.upload_media(
            access_token=access_token,
            target=target,
            target_type=target_type,
            file_bytes=file_bytes,
        )
        endpoint = self.message_endpoint(target=target, target_type=target_type)
        payload = self.media_message_payload(
            file_info=upload.file_info,
            target_type=target_type,
            msg_id=msg_id,
            caption=caption,
        )
        return self._send_payload(
            access_token=access_token,
            endpoint=endpoint,
            payload=payload,
        )

    def upload_media(
        self,
        *,
        access_token: str,
        target: str,
        target_type: str,
        file_bytes: bytes,
        file_type: int = 1,
    ) -> QQBotMediaUploadResult:
        endpoint = self.media_endpoint(target=target, target_type=target_type)
        payload = {
            "file_type": file_type,
            "file_data": base64.b64encode(file_bytes).decode("ascii"),
        }
        url = f"{self.api_base}{endpoint}"

        try:
            response = self._post_json(
                url,
                headers={
                    "Authorization": f"QQBot {access_token}",
                    "Content-Type": "application/json",
                },
                payload=payload,
            )
        except QQBotClientError as exc:
            raise QQBotClientError(str(exc)) from exc

        if not isinstance(response, dict) or not response.get("file_info"):
            raise QQBotClientError(f"QQBot media upload response did not include file_info: {response}")
        return QQBotMediaUploadResult(
            file_info=str(response["file_info"]),
            file_uuid=str(response.get("file_uuid") or "").strip() or None,
            ttl=int(response["ttl"]) if response.get("ttl") is not None else None,
            response=response,
        )

    @staticmethod
    def message_endpoint(*, target: str, target_type: str) -> str:
        clean_target = target.strip()
        normalized_type = target_type.lower().strip()
        if normalized_type in {"c2c", "private", "user"}:
            return f"/v2/users/{clean_target}/messages"
        if normalized_type == "group":
            return f"/v2/groups/{clean_target}/messages"
        raise QQBotClientError(f"Unsupported QQBot target_type: {target_type}. Use c2c or group.")

    @staticmethod
    def media_endpoint(*, target: str, target_type: str) -> str:
        clean_target = target.strip()
        normalized_type = target_type.lower().strip()
        if normalized_type in {"c2c", "private", "user"}:
            return f"/v2/users/{clean_target}/files"
        if normalized_type == "group":
            return f"/v2/groups/{clean_target}/files"
        raise QQBotClientError(f"Unsupported QQBot target_type: {target_type}. Use c2c or group.")

    @staticmethod
    def message_payload(*, message: str, msg_id: str | None = None) -> dict[str, Any]:
        if not message.strip():
            raise QQBotClientError("QQBot message content is empty")

        payload: dict[str, Any] = {"content": message, "msg_type": 0}
        clean_msg_id = str(msg_id or "").strip()
        if clean_msg_id:
            payload["msg_id"] = clean_msg_id
        return payload

    @staticmethod
    def media_message_payload(
        *,
        file_info: str,
        target_type: str,
        msg_id: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
        }
        clean_msg_id = str(msg_id or "").strip()
        if clean_msg_id:
            payload["msg_id"] = clean_msg_id
        clean_caption = str(caption or "").strip()
        if clean_caption:
            payload["content"] = clean_caption
        elif target_type.lower().strip() == "group":
            payload["content"] = "image"
        return payload

    def _send_payload(
        self,
        *,
        access_token: str,
        endpoint: str,
        payload: dict[str, Any],
    ) -> QQBotResult:
        url = f"{self.api_base}{endpoint}"
        try:
            response = self._post_json(
                url,
                headers={
                    "Authorization": f"QQBot {access_token}",
                    "Content-Type": "application/json",
                },
                payload=payload,
            )
        except QQBotClientError as exc:
            return QQBotResult(success=False, endpoint=endpoint, status=None, error=str(exc))
        return QQBotResult(success=True, endpoint=endpoint, status=200, response=response)

    def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any] | list[Any] | str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise QQBotClientError(f"HTTP {exc.code} from {url}: {response_body}") from exc
        except error.URLError as exc:
            raise QQBotClientError(f"Network error calling {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQBotClientError(f"Timeout calling {url}") from exc

        if not response_body:
            return ""
        try:
            return json.loads(response_body)
        except json.JSONDecodeError:
            return response_body

    def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> dict[str, Any] | list[Any] | str:
        req = request.Request(url=url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise QQBotClientError(f"HTTP {exc.code} from {url}: {response_body}") from exc
        except error.URLError as exc:
            raise QQBotClientError(f"Network error calling {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQBotClientError(f"Timeout calling {url}") from exc

        if not response_body:
            return ""
        try:
            return json.loads(response_body)
        except json.JSONDecodeError:
            return response_body
