"""
排行榜快照持久化

按榜单类型（H24/D7/D30）把每次发送的榜单保存到
``{data_dir}/rank_history/{tt}.json``，下次发送时对比上一次的快照，
找出「新上榜」的作品（本次在榜、上次不在榜）。

只保存 ``_id`` / ``title`` / 名次，文件很小；写入采用「先写临时文件再替换」的
原子方式，避免中途中断留下半个文件。
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

# 快照目录名（位于插件数据目录下，不受 /picaclean 影响）
HISTORY_DIR = "rank_history"


@dataclass
class RankDiff:
    """本次榜单与上次快照的差异"""

    #: 新上榜条目 [(名次, comic), ...]，名次为榜单内序号（从 1 开始）
    new_entries: list[tuple[int, dict]] = field(default_factory=list)
    #: 上次快照的保存时间戳，没有上一次快照时为 None
    previous_updated_at: float | None = None


class RankHistoryStore:
    """排行榜快照存取"""

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / HISTORY_DIR

    def path(self, tt: str) -> Path:
        return self.dir / f"{tt}.json"

    def load(self, tt: str) -> dict:
        """读取上次快照，无记录或文件损坏时返回空 dict"""
        path = self.path(tt)
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"读取排行榜快照失败 {path.name}: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, tt: str, comics: list[dict]) -> None:
        """保存本次榜单（原子写入）"""
        items = [
            {
                "_id": str(comic.get("_id")),
                "title": str(comic.get("title") or ""),
                "rank": rank,
            }
            for rank, comic in enumerate(comics, 1)
            if comic.get("_id")
        ]
        payload = {"tt": tt, "updated_at": time.time(), "items": items}

        path = self.path(tt)
        tmp = path.with_name(path.name + ".tmp")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"保存排行榜快照失败 {path.name}: {e}")

    def update(
        self, tt: str, comics: list[dict], limit: int | None = None
    ) -> RankDiff:
        """对比上次快照 → 保存本次快照 → 返回「新上榜」条目。

        Args:
            tt: 榜单类型
            comics: **全量**榜单（调用方自行决定展示多少条）
            limit: 只报告名次 ≤ limit 的新上榜作品（即用户实际能看到的部分）
        """
        previous = self.load(tt)
        prev_items = previous.get("items")
        prev_ids = {
            str(item.get("_id"))
            for item in (prev_items if isinstance(prev_items, list) else [])
            if isinstance(item, dict) and item.get("_id")
        }
        previous_at = previous.get("updated_at") if prev_ids else None

        new_entries: list[tuple[int, dict]] = []
        if prev_ids:
            for rank, comic in enumerate(comics, 1):
                if limit is not None and rank > limit:
                    break
                comic_id = str(comic.get("_id") or "")
                if comic_id and comic_id not in prev_ids:
                    new_entries.append((rank, comic))

        self.save(tt, comics)
        return RankDiff(new_entries=new_entries, previous_updated_at=previous_at)
