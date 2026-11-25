"""事件同步：从联调入口或本地 fixture 按游标顺序摄入，断线后从上次游标续传。"""

from __future__ import annotations

import json
from urllib import request as urlrequest
from urllib.error import URLError

from .service import RehearsalService


class EventSync:
    def __init__(self, service: RehearsalService):
        self.service = service

    def ingest_bundle(self, events: list[dict]) -> list[dict]:
        """按游标顺序摄入一捆事件；已处理游标自动跳过，保证可重复运行。"""
        applied = []
        for event in sorted(events, key=lambda e: e["cursor"]):
            try:
                result = self.service.ingest_event(
                    cursor=event["cursor"], source=event["source"],
                    minute=event["minute"], payload=event["payload"])
            except Exception as exc:  # noqa: BLE001 - 同步器要把坏事件隔离出来
                if getattr(exc, "code", "") == "stale_cursor":
                    continue
                applied.append({"cursor": event["cursor"], "error": str(exc)})
                continue
            applied.append(result)
        return applied

    def pull_http(self, base_url: str, limit: int = 50,
                  max_pages: int = 100) -> list[dict]:
        """从 fixture_gateway 的 /v1/events 拉取并摄入，支持分页与断点续传。"""
        base_url = base_url.rstrip("/")
        cursor = self.service.ledger.event_cursor
        applied: list[dict] = []
        for _ in range(max_pages):
            url = f"{base_url}/v1/events?after={cursor}&limit={limit}"
            try:
                with urlrequest.urlopen(url, timeout=5) as response:
                    page = json.loads(response.read())
            except URLError as exc:
                raise ConnectionError(f"事件流中断于游标 {cursor}：{exc}") from exc
            events = page["events"]
            if not events:
                break
            applied.extend(self.ingest_bundle(events))
            cursor = page["next_cursor"]
        return applied
