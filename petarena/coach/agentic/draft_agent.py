"""选将 Agent：上下文里有局面、宠物和 Top10 阵容表；LLM 可以检索组合经验和经验条目，最后在 Top10 里选一套。

分工：
- 程序：算 Top10（经验库的数字，见 draft_context.py），一次性放进上下文；
- LLM：决定查什么、在 Top10 里选哪套、怎么讲；
- guard：编号必须存在，文字里的数字只能来自 Top10 表或工具结果。

不再有引擎决选：LLM 选了 T3，面板就推荐 T3，并照实显示它和 T1 的差距。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ...engine.draft import PICK_ORDER
from ...engine.pets import SIDE_NAMES, SPECIES_ORDER, TEAM_SIZE
from ..draft import AdviceFailure, OpponentProfile, Situation, names, pet_name
from ..experience import relevant_lessons
from ..knowledge import KnowledgeBase
from ..llm import LLM
from ..memory import Memory
from . import runtime
from .draft_context import Row, situation_text, table_text
from .guard import Facts

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "draft.md").read_text(encoding="utf-8").strip()

BUDGET = runtime.Budget(calls=4, seconds=25.0, max_tokens=900)
REASON_LIMIT = 120
STYLE_LIMIT = 16
INVALID = "invalid"
COMBO_FIELDS = ("members", "style", "synergy", "plan", "beats", "fears", "notes")


def strategy_text(style: str, lineup: Sequence[str], fixed: int) -> str:
    """整体思路 = 打法 + 之后几手的计划。我方第 i 只（从 0 数）在第 2(i+1) 手选。"""
    style = style or "综合推演最优"
    if fixed >= TEAM_SIZE - 1:
        return style
    later = "，".join(f"第 {2 * (index + 1)} 手计划补{pet_name(lineup[index])}" for index in range(fixed + 1, TEAM_SIZE))
    return f"{style}：本手{pet_name(lineup[fixed])}，{later}"


@dataclass
class DraftResult:
    advice: dict[str, Any]
    evidence: dict[str, Any]
    trace: dict[str, Any]


class DraftAgent:
    def __init__(self, llm: LLM, kb: KnowledgeBase, budget: runtime.Budget = BUDGET) -> None:
        self.llm = llm
        self.kb = kb
        self.budget = budget

    # ------------------------------------------------------------ 工具

    def tools(self, situation: Situation, memory: Memory | None) -> list[runtime.Tool]:
        kb = self.kb
        keys = {"type": "array", "items": {"type": "string", "enum": list(SPECIES_ORDER)}}

        def combos(args: dict[str, Any]) -> str:
            rows = kb.search_combo_experience(
                include=args.get("include") or (), against=args.get("against") or situation.opp_picked,
                exclude=args.get("exclude") or (), limit=min(int(args.get("limit") or 4), 6))
            # 只给文字经验：组合库里的平均胜率口径和 Top10 不同，放进来只会让 LLM 混用两种数字
            shown = [{field: row[field] for field in COMBO_FIELDS} for row in rows]
            return json.dumps(shown, ensure_ascii=False) if shown else "没有符合条件的组合经验。"

        def lessons(args: dict[str, Any]) -> str:
            pets = tuple(dict.fromkeys(list(situation.my_picked) + list(args.get("pets") or [])))
            against = tuple(dict.fromkeys(list(situation.opp_picked) + list(args.get("against") or [])))
            found = relevant_lessons(memory, situation.opponent, pets, against)
            if not found:
                return "经验库里还没有相关的经验条目。"
            return "\n".join(
                f"- {'宜' if item['polarity'] == 'do' else '忌'}｜涉及 {names(item['pets'])}"
                + (f"，针对对手 {names(item['against'])}" if item["against"] else "")
                + f"｜{item['text']}（印证 {item['support']} 次，反驳 {item['contra']} 次）" for item in found)

        return [
            runtime.Tool("search_combo_experience",
                         "查我方三宠组合的文字经验：打法、配合、对哪些对手宠物有利或不利。include 必须包含的宠物，"
                         "against 对手宠物（默认对手已选的），exclude 不能出现的宠物",
                         runtime.schema({"include": keys, "against": keys, "exclude": keys,
                                         "limit": {"type": "integer", "minimum": 1, "maximum": 6}}), combos),
            runtime.Tool("search_lessons", "查之前复盘沉淀的「宜 / 忌」经验条目（按涉及的我方宠物和针对的对手宠物检索）",
                         runtime.schema({"pets": keys, "against": keys}), lessons),
            runtime.Tool("submit_recommendation", "提交本手的选将建议（结束）",
                         runtime.schema({
                             "choice": {"type": "string", "description": "推荐的阵容编号，如 T1"},
                             "reason": {"type": "string", "description": "不超过 100 字"},
                             "alternatives": {"type": "array", "items": runtime.schema(
                                 {"choice": {"type": "string"}, "reason": {"type": "string"}}, ["choice", "reason"]),
                                 "description": "1～2 套备选，本手要选的宠物和推荐不同"},
                             "strategy": {"type": "string", "description": "不超过 16 字的整体打法"},
                         }, ["choice", "reason", "strategy"])),
        ]

    # ------------------------------------------------------------ 主流程

    def advise(self, situation: Situation, profile: OpponentProfile, rows: Sequence[Row], *,
               memory: Memory | None = None, record: Sequence[dict[str, Any]] = (),
               attempt: int = 1, feedback: str | None = None, exhaustive: bool = True) -> DraftResult:
        if PICK_ORDER[situation.step] != "me":
            raise ValueError(f"第 {situation.hand} 手不是{SIDE_NAMES['me']}的回合")
        if not rows:
            raise AdviceFailure(INVALID, "没有可推荐的阵容", retryable=False)
        context = situation_text(situation, profile, self.kb, record)
        table = table_text(rows, profile)
        user = f"{context}\n\n【Top{len(rows)} 阵容表】\n{table}"
        if feedback:
            user += f"\n\n【上次提交的问题】\n{feedback}"
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        hint = {"kind": "draft", "token": situation.token, "rows": [row.to_dict() for row in rows]}
        outcome = runtime.run(self.llm, messages, self.tools(situation, memory),
                              self.budget, hint)
        facts = Facts([SYSTEM_PROMPT, context, table, *outcome.evidence.values()])
        facts.add_pct(value for row in rows for value in (row.score.win_rate, row.score.worst_win_rate))
        chosen, alternatives, reason, strategy = self.check(outcome.args, rows, facts)

        fixed = len(situation.my_picked)
        score = chosen.score
        top = rows[0].score
        advice = {
            "pet": chosen.pick,
            "reason": reason,
            "alternatives": [
                {"pet": row.pick, "reason": why, "plan": list(row.lineup), "id": row.id,
                 "robust": round(row.score.robust, 3), "win_rate": round(row.score.win_rate, 3), "source": "agent"}
                for row, why in alternatives
            ],
            "strategy": strategy_text(strategy, score.lineup, fixed),
            "plan": list(score.lineup),
            "id": chosen.id,
            "source": "agent",
        }
        evidence = {
            "label": profile.label,
            "chosen": {
                "id": chosen.id, "rank": rows.index(chosen) + 1,
                "robust": round(score.robust, 3), "win_rate": round(score.win_rate, 3), "mean": round(score.mean, 3),
                "worst": round(score.worst, 3), "worst_win_rate": round(score.worst_win_rate, 3),
                "worst_reply": list(score.worst_reply), "replies": score.replies, "samples": score.samples,
                "verified": chosen.verified,
                "gap_to_top": round(top.win_rate - score.win_rate, 3),
            },
            "top": [row.to_dict() for row in rows],
            "ranking": [{"pet": row.pick, "robust": round(row.score.robust, 3), "win_rate": round(row.score.win_rate, 3)}
                        for row in rows],
            "exhaustive": exhaustive,
        }
        trace = {"attempt": attempt, **outcome.trace(), "chosen": chosen.id, "rows": len(rows)}
        return DraftResult(advice, evidence, trace)

    @staticmethod
    def check(args: dict[str, Any], rows: Sequence[Row], facts: Facts) -> tuple[Row, list[tuple[Row, str]], str, str]:
        """guard：编号存在、数字有出处。不合格抛 AdviceFailure，原因写进下一次请求。"""
        by_id = {row.id: row for row in rows}
        ids = "、".join(by_id)
        chosen = by_id.get(str(args.get("choice", "")).strip().upper())
        if chosen is None:
            raise AdviceFailure(INVALID, "AI 推荐了不在 Top10 里的阵容",
                                feedback=f"choice 必须是 Top 表里的编号（{ids}），你写的是 {args.get('choice')!r}。")
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise AdviceFailure(INVALID, "AI 没有给出理由", feedback="缺少 reason。")
        strategy = args.get("strategy") if isinstance(args.get("strategy"), str) else ""
        problems = facts.problems(reason, "reason") + facts.problems(strategy, "strategy")

        alternatives: list[tuple[Row, str]] = []
        picks = {chosen.pick}
        raw = args.get("alternatives") if isinstance(args.get("alternatives"), list) else []
        for item in raw:
            if not isinstance(item, dict):
                continue
            row = by_id.get(str(item.get("choice", "")).strip().upper())
            if row is None:
                problems.append(f"备选 {item.get('choice')!r} 不是 Top 表里的编号（{ids}）")
                continue
            if row.pick in picks or len(alternatives) >= 2:
                continue  # 本手要选的宠物和推荐（或前一个备选）相同：没有意义，忽略
            why = item.get("reason") if isinstance(item.get("reason"), str) else ""
            problems += facts.problems(why, f"备选 {row.id} 的 reason")
            picks.add(row.pick)
            alternatives.append((row, why.strip()[:REASON_LIMIT]))
        if problems:
            raise AdviceFailure(INVALID, "AI 建议没有通过校验", feedback="\n".join(f"- {p}" for p in problems))
        return chosen, alternatives, reason.strip()[:REASON_LIMIT], strategy.strip()[:STYLE_LIMIT]
