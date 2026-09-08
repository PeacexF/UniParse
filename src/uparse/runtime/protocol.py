from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# stdout carries only these frames. Diagnostics go to stderr, always.
METHODS = frozenset(
    {
        "hello",
        "navigate",
        "content",
        "evaluate",
        "scroll",
        "click",
        "screenshot",
        "close",
        "shutdown",
    }
)


@dataclass(slots=True)
class Request:
    id: int
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> str:
        return json.dumps(
            {"id": self.id, "method": self.method, "params": self.params}, ensure_ascii=False
        )


@dataclass(slots=True)
class Response:
    id: int
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    code: str | None = None

    @classmethod
    def decode(cls, line: str) -> Response:
        payload = json.loads(line)
        return cls(
            id=int(payload.get("id", 0)),
            ok=bool(payload.get("ok")),
            data=payload.get("data") or {},
            error=payload.get("error"),
            code=payload.get("code"),
        )
