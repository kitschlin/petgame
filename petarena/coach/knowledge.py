"""知识层：宠物经验库与组合经验库。

两个库都是随代码提交的 JSON，启动时读进内存，按字段检索，不用向量检索：
条目少、局势本身是结构化的，字段匹配更准确，也更容易解释「为什么检索到这条」。

本版由编排层在调用 LLM 之前固定执行两个检索；接口按标准工具定义编写（见 TOOLS），
以后宠物变多、上下文放不下时，可以直接开放给 LLM 按需调用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from ..engine.pets import SIDES, SPECIES
from .policy import HEURISTIC, RANDOM_MODE, SKILL_FIRST

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

PET_FIELDS = ("key", "name", "role", "tags", "skill", "passive", "sides", "how_to_use",
              "strong_vs", "weak_vs", "counter_notes", "synergy")
COMBO_FIELDS = ("members", "style", "synergy", "plan", "beats", "fears", "notes")

# evidence 里按对手打法分别记录平均表现
EVIDENCE_AVG = {HEURISTIC: "avg_win_vs_field", RANDOM_MODE: "avg_win_vs_random", SKILL_FIRST: "avg_win_vs_smart"}
EVIDENCE_ORDER = {HEURISTIC: "best_order", RANDOM_MODE: "best_order_random", SKILL_FIRST: "best_order_smart"}


def tier(avg: float | None) -> str:
    """把对阵表里的平均表现换成给 LLM 看的档位（LLM 不接触具体胜率数字）。"""
    if avg is None:
        return "未验证"
    if avg >= 0.6:
        return "强"
    if avg >= 0.4:
        return "中上"
    if avg >= 0.25:
        return "中"
    return "弱"


class KnowledgeBase:
    def __init__(self, pets: list[dict[str, Any]], combos: list[dict[str, Any]]) -> None:
        self.pets = {pet["key"]: pet for pet in pets}
        self.combos = combos

    @classmethod
    def load(cls, directory: str | Path = KNOWLEDGE_DIR) -> "KnowledgeBase":
        directory = Path(directory)
        pets = json.loads((directory / "pets.json").read_text(encoding="utf-8"))
        combos = json.loads((directory / "combos.json").read_text(encoding="utf-8"))
        return cls(pets, combos)

    # ------------------------------------------------------------ 校验

    def problems(self) -> list[str]:
        """结构性问题：缺字段、未知宠物、重复成员等。空列表表示没有问题。"""
        found: list[str] = []
        for key in SPECIES:
            if key not in self.pets:
                found.append(f"宠物经验库缺少 {key}")
        for key, pet in self.pets.items():
            if key not in SPECIES:
                found.append(f"宠物经验库有未知宠物 {key}")
                continue
            for field in PET_FIELDS:
                if field not in pet:
                    found.append(f"{key} 缺少字段 {field}")
            if set(pet.get("sides", {})) != set(SIDES):
                found.append(f"{key} 的 sides 必须同时包含 me 和 opp")
            for field in ("strong_vs", "weak_vs", "synergy"):
                for other in pet.get(field, []):
                    if other not in SPECIES:
                        found.append(f"{key}.{field} 有未知宠物 {other}")
        seen: set[frozenset[str]] = set()
        for index, combo in enumerate(self.combos):
            label = f"组合 #{index + 1} {combo.get('members')}"
            for field in COMBO_FIELDS:
                if field not in combo:
                    found.append(f"{label} 缺少字段 {field}")
            members = combo.get("members", [])
            if len(members) != 3 or len(set(members)) != 3 or any(m not in SPECIES for m in members):
                found.append(f"{label} 的成员必须是 3 只不同的合法宠物")
                continue
            if frozenset(members) in seen:
                found.append(f"{label} 重复收录")
            seen.add(frozenset(members))
            for field in ("beats", "fears"):
                for other in combo.get(field, []):
                    if other not in SPECIES:
                        found.append(f"{label}.{field} 有未知宠物 {other}")
            if set(combo.get("beats", [])) & set(combo.get("fears", [])):
                found.append(f"{label} 的 beats 和 fears 有重叠")
        return found

    # ------------------------------------------------------------ 检索

    def search_pet_experience(self, keys: Sequence[str], side: str) -> list[dict[str, Any]]:
        """按输入顺序返回宠物条目；只带所问阵营的说明（side_note）。"""
        if side not in SIDES:
            raise ValueError(f"未知阵营：{side}")
        result = []
        for key in keys:
            pet = self.pets.get(key)
            if pet is None:
                continue
            entry = {field: pet[field] for field in PET_FIELDS if field != "sides" and field in pet}
            entry["side"] = side
            entry["side_note"] = pet["sides"][side]
            result.append(entry)
        return result

    def search_combo_experience(
        self,
        include: Sequence[str] = (),
        against: Sequence[str] = (),
        exclude: Sequence[str] = (),
        limit: int = 6,
        opp_policy: str = HEURISTIC,
    ) -> list[dict[str, Any]]:
        """检索我方组合。

        include：必须包含的宠物（我方已选）；exclude：不能出现的宠物；
        against：对手已选宠物，用来计算相关度（beats 命中 +1、fears 命中 −1）。
        排序：相关度从高到低 → 对阵表平均表现从高到低 → 收录顺序。
        """
        include_set, exclude_set, against_set = set(include), set(exclude), set(against)
        avg_key = EVIDENCE_AVG.get(opp_policy, EVIDENCE_AVG[HEURISTIC])
        order_key = EVIDENCE_ORDER.get(opp_policy, EVIDENCE_ORDER[HEURISTIC])
        rows = []
        for index, combo in enumerate(self.combos):
            members = set(combo["members"])
            if not include_set <= members or members & exclude_set:
                continue
            relation = len(against_set & set(combo["beats"])) - len(against_set & set(combo["fears"]))
            evidence = combo.get("evidence") or {}
            avg = evidence.get(avg_key)
            entry = {field: combo[field] for field in COMBO_FIELDS}
            entry.update(relation=relation, avg=avg, tier=tier(avg), order=evidence.get(order_key))
            rows.append((-relation, -(avg if avg is not None else -1.0), index, entry))
        rows.sort(key=lambda row: row[:3])
        return [row[3] for row in rows[:max(0, limit)]]


# ---------------------------------------------------------------- 工具定义

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_pet_experience",
            "description": "查询宠物经验：技能、被动、用法、克制关系，以及指定阵营的数值特点",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string", "enum": list(SPECIES)},
                             "description": "要查询的宠物 key"},
                    "side": {"type": "string", "enum": list(SIDES), "description": "me 我方 / opp 对手方"},
                },
                "required": ["keys", "side"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_combo_experience",
            "description": "查询我方三宠组合经验：打法、配合、有利和不利的对手宠物",
            "parameters": {
                "type": "object",
                "properties": {
                    "include": {"type": "array", "items": {"type": "string"}, "description": "必须包含的宠物（我方已选）"},
                    "against": {"type": "array", "items": {"type": "string"}, "description": "对手已选宠物"},
                    "exclude": {"type": "array", "items": {"type": "string"}, "description": "不能出现的宠物"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": [],
            },
        },
    },
]


def call_tool(kb: KnowledgeBase, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """按工具名分发，供编排层或（以后）LLM 的 tool call 使用。"""
    if name == "search_pet_experience":
        return kb.search_pet_experience(arguments["keys"], arguments["side"])
    if name == "search_combo_experience":
        return kb.search_combo_experience(
            include=arguments.get("include", ()), against=arguments.get("against", ()),
            exclude=arguments.get("exclude", ()), limit=arguments.get("limit", 6),
            opp_policy=arguments.get("opp_policy", HEURISTIC),
        )
    raise ValueError(f"未知工具：{name}")
