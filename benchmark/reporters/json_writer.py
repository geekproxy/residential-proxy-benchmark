"""
Writes raw benchmark data to JSONL for later analysis.

JSONL keeps each probe atomic and append-friendly so we can stream-write
during long runs and survive interruption.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import aiofiles


class JsonlWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def write(self, record: Any) -> None:
        if is_dataclass(record):
            payload = asdict(record)
        elif isinstance(record, dict):
            payload = record
        else:
            payload = {"value": record}
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        async with self._lock:
            async with aiofiles.open(self.path, "a", encoding="utf-8") as f:
                await f.write(line)

    async def write_many(self, records: list[Any]) -> None:
        for r in records:
            await self.write(r)
