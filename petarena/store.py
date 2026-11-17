"""JSON 文件存档。

    data/pve_records.jsonl     PVE 记录，只追加（永久成长由它累加得出）
    data/battles/<id>.json     每场战斗的完整记录（种子、阵容、行动、事件）
    data/battles/index.json    战斗记录摘要列表
    data/current_match.json    进行中的对局（种子 + 选择 + 行动），用于断点续玩

整文件写入都先写临时文件再改名，避免中途崩溃留下半截文件。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.battles_dir = self.root / "battles"
        self.battles_dir.mkdir(parents=True, exist_ok=True)
        self._pve_path = self.root / "pve_records.jsonl"
        self._index_path = self.battles_dir / "index.json"
        self._match_path = self.root / "current_match.json"

    # ------------------------------------------------------------------ PVE

    def load_pve(self) -> list[dict[str, Any]]:
        records = []
        try:
            with open(self._pve_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 写到一半的残行
        except FileNotFoundError:
            pass
        return records

    def append_pve(self, record: dict[str, Any]) -> None:
        with open(self._pve_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ------------------------------------------------------------- 战斗记录

    def list_battles(self) -> list[dict[str, Any]]:
        return _read(self._index_path, [])

    def save_battle(self, record: dict[str, Any], summary: dict[str, Any]) -> None:
        _write(self.battles_dir / f"{record['id']}.json", record)
        index = [s for s in self.list_battles() if s["id"] != record["id"]]
        index.append(summary)
        _write(self._index_path, index)

    def load_battle(self, battle_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID.match(battle_id):
            return None
        return _read(self.battles_dir / f"{battle_id}.json", None)

    # ----------------------------------------------------------- 进行中对局

    def load_match(self) -> dict[str, Any] | None:
        return _read(self._match_path, None)

    def save_match(self, data: dict[str, Any]) -> None:
        _write(self._match_path, data)

    def clear_match(self) -> None:
        try:
            self._match_path.unlink()
        except FileNotFoundError:
            pass
