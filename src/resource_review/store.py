"""仅追加的事件日志存储。

所有状态变化先写日志（fsync 落盘），再更新内存索引。
进程中断后重放日志即可恢复全部事实，未完成的复核与裁决可继续推进。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional


class EventStore:
    def __init__(self, path: os.PathLike | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._listeners: List[Callable[[dict], None]] = []

    @property
    def path(self) -> Path:
        return self._path

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        self._listeners.append(listener)

    def append(self, event: dict) -> None:
        """原子追加一条事件：先写临时文件 fsync，再追加 fsync。"""
        line = json.dumps(event, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        # 先把载荷落到临时文件并 fsync，降低崩溃产生半行事件的概率
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".journal-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            with open(self._path, "ab") as journal:
                journal.write(data)
                journal.flush()
                os.fsync(journal.fileno())
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        for listener in self._listeners:
            listener(event)

    def replay(self, handler: Callable[[dict], None]) -> int:
        """按写入顺序重放全部事件，返回事件条数。"""
        if not self._path.exists():
            return 0
        count = 0
        with open(self._path, "r", encoding="utf-8") as journal:
            for line in journal:
                line = line.strip()
                if not line:
                    continue
                handler(json.loads(line))
                count += 1
        return count

    def read_all(self) -> List[dict]:
        events: List[dict] = []
        self.replay(events.append)
        return events
