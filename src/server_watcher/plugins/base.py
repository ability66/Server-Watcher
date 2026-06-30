from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    aliases: tuple[str, ...]
    summary: str
    examples: tuple[str, ...] = ()


class CommandPlugin(ABC):
    name: str
    spec: CommandSpec

    @abstractmethod
    def handle(self, command_text: str) -> "Reply | None":
        raise NotImplementedError


@dataclass(frozen=True)
class TextReply:
    text: str


@dataclass(frozen=True)
class ImageReply:
    content: bytes
    filename: str = "reply.png"
    caption: str | None = None


Reply = TextReply | ImageReply
