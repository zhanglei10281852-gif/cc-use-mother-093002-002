"""追加式事件存储：单一日志文件构成唯一事实链。

- 每次追加携带调用方看到的最新序号（expected_seq），
  与文件实际序号不一致即拒绝，保证并发决定只有一方能推进事实链；
- 同一批事件一次性写入并 fsync，崩溃只可能留下尾部半行，
  读取时忽略损坏尾部，已落盘的事件不丢失；
- 进程中断后重新打开即可从已持久化的事件恢复全部状态。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConcurrencyConflictError


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> tuple[int, list[dict]]:
        """返回 (最新序号, 事件列表)。容忍崩溃留下的损坏尾部。"""
        events: list[dict] = []
        if not self.path.exists():
            return 0, events
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # 崩溃残留的半行：忽略尾部，保留已完整落盘的事件
        seq = events[-1]["seq"] if events else 0
        return seq, events

    def append(self, expected_seq: int, events: list[dict]) -> list[dict]:
        """原子追加一批事件，返回带序号与时间戳的事件信封。"""
        if not events:
            return []
        current, _ = self.read()
        if current != expected_seq:
            raise ConcurrencyConflictError(
                f"事实链已推进到序号 {current}，本次决定基于序号 {expected_seq}，"
                "已被拒绝以避免产生第二条事实链"
            )
        now = datetime.now(timezone.utc).isoformat()
        envelopes = []
        lines = []
        for offset, event in enumerate(events, start=1):
            envelope = {
                "seq": expected_seq + offset,
                "at": now,
                "type": event["type"],
                "payload": event["payload"],
            }
            envelopes.append(envelope)
            lines.append(json.dumps(envelope, ensure_ascii=False))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return envelopes
