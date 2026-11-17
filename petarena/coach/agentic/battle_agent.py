"""战斗 Agent：引擎先把每个合法动作都推演一遍，LLM 看完结果决定怎么走，并写下本回合的计划。

    推演全部动作 → 用户消息（事件 + 局面 + 推演结果表）→ LLM（可查经验、看局面）→ submit_move

- 有胜率明显领先的动作（是否「明显」由 LLM 判断，不写死阈值）：推荐它，basis = "engine"。
- 没有：LLM 按 prompt 里的判断原则自己选（任何合法动作都可以），basis = "judgment"，并说明理由。

两种模式（COACH_BATTLE_AGENT，见 config.py）：
- step（每步单独）：每次建议一段新对话。上一次建议的动作、计划和玩家是否采纳写进这次的用户消息。
- session（整局一段对话）：每轮到我方就往同一段对话里追加一条用户消息（上次建议之后发生了什么 + 当前局面）。
  玩家在建议返回前就操作了、或者请求失败时，这一轮追加的消息全部丢弃，对话只保留已经完成的轮次；
  下一轮的用户消息照样告诉 LLM 实际发生了什么，所以对话和真实对局始终一致。早先轮次的推演结果和局面
  按块压缩（一次压一批，保住前缀缓存）。

不论哪种模式：guard 校验动作是否合法、文字里的数字是否有出处；basis = "engine" 时动作必须是推演胜率最高的。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...engine.pets import SIDE_NAMES, SPECIES_ORDER
from ..battle import Evaluation, MODEL_LABELS, OWN_LABEL, _effects, _label, _n, describe, evidence_for, skill_name
from ..draft import AdviceFailure, OpponentProfile, names
from ..memory import Memory
from ..rollout import fact_text
from . import runtime
from .guard import Facts

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "battle.md").read_text(encoding="utf-8").strip()

STEP = "step"
SESSION = "session"
MODES = (STEP, SESSION)
MODE_NOTES = {
    STEP: "每次行动都是一段新的对话：上一次的建议、你写的计划和玩家实际怎么做，会写在用户消息的开头。",
    SESSION: "整局是同一段对话：每轮到我方行动，你会收到上次建议之后发生的事件和新的局面。早先的推演结果会被省略，需要时重新推演。",
}

BUDGET = runtime.Budget(calls=3, seconds=14.0, max_tokens=420)
REASON_LIMIT = 100
RISK_LIMIT = 60
PLAN_LIMIT = 60
EVENT_LIMIT = 30  # 用户消息里最多带多少条事件
KEEP_FULL = 4  # 会话模式：压缩后保留几轮完整内容
COMPACT_AT = 8  # 完整的轮次超过这么多就压缩一批
INVALID = "invalid"
ENGINE = "engine"  # 依据：推演明显领先
JUDGMENT = "judgment"  # 依据：LLM 自己的判断
BASES = (ENGINE, JUDGMENT)


# ---------------------------------------------------------------- 文字

def state_text(snap: dict[str, Any], profile: OpponentProfile) -> str:
    view, static = snap["view"], snap["static"]
    queue = " → ".join(_label(snap, pet_id) for pet_id in view["upcoming"] if pet_id != snap["actor"]) or "（本回合没有其他宠物要行动了）"
    done = "、".join(_label(snap, pet_id) for pet_id in view["done"]) or "无"
    lines = [
        "【战况】",
        f"第 {view['round']} / {static['max_rounds']} 回合。"
        f"能量豆：我方 {view['teams']['me']['beans']}，对手方 {view['teams']['opp']['beans']}（上限 {static['energy_cap']}）。",
        f"本回合已行动：{done}。当前行动：{_label(snap, snap['actor'])}。之后依次：{queue}",
    ]
    lines += [f"场上效果：{line}" for line in _effects(snap)] or ["场上效果：无"]
    lines.append(f"对手方操作方式：{profile.brief}")
    lines += ["", "【宠物状态】（id｜名字｜站位｜生命/上限｜护盾｜攻击｜速度）"]
    for side in ("me", "opp"):
        for pet in static["pets"].values():
            if pet["side"] != side:
                continue
            state = view["pets"][pet["id"]]
            if not state["alive"]:
                lines.append(f"{pet['id']}｜{SIDE_NAMES[side]}{pet['name']}｜{pet['slot']} 号位｜已倒下")
                continue
            extra = "｜水链连接中" if state["linked"] else ""
            hit = f"｜命中 {pet['hit']}%" if pet.get("hit") else ""
            lines.append(
                f"{pet['id']}｜{SIDE_NAMES[side]}{pet['name']}｜{pet['slot']} 号位｜{_n(state['hp'])}/{_n(pet['max_hp'])}"
                f"｜护盾 {_n(state['shield'])}｜攻 {pet['atk']}｜速 {pet['speed']}{hit}{extra}")
    lines += ["", f"【当前行动】{snap['actor']} {_label(snap, snap['actor'])}，合法动作："]
    for option in snap["options"]:
        cost = f"，消耗 {option['cost']} 豆" if option["cost"] else ""
        if not option["enabled"]:
            lines.append(f"- {option['skill']}：{option['name']}（不可用：{option['reason']}）")
        elif option["targets"] is None:
            lines.append(f"- {option['skill']}：{option['name']}{cost}，无需选择目标（target 填 null）")
        else:
            lines.append(f"- {option['skill']}：{option['name']}{cost}，可选目标 {', '.join(option['targets'])}")
    return "\n".join(lines)


@dataclass
class LastAdvice:
    """上一次（已经显示给玩家的）建议：下一次请求要告诉 LLM 玩家有没有照做。"""

    index: int
    actor: str
    text: str
    plan: str


def events_text(snap: dict[str, Any], last: LastAdvice | None, seen: int) -> str:
    lines = []
    if last is not None:
        mark = (snap.get("marks") or {}).get(str(last.index))
        did = "玩家照做了" if mark and mark.get("adopted") else "玩家没有照做" if mark else "不清楚玩家有没有看到这条建议"
        lines.append(f"【上次建议】{last.actor}：{last.text}（{did}）。你当时的计划：{last.plan or '（没写）'}")
    events = [text for text in (snap.get("events") or [])[seen:] if text]
    if events:
        dropped = len(events) - EVENT_LIMIT
        events = events[-EVENT_LIMIT:]
        head = "【上次建议之后发生的事件】" if last is not None else "【战斗开始以来的事件】"
        lines.append(head + (f"（省略了更早的 {dropped} 条）" if dropped > 0 else ""))
        lines += [f"- {text}" for text in events]
    return "\n".join(lines)


def _pp(value: float) -> str:
    return f"{value * 100:.0f}"


def evaluation_text(snap: dict[str, Any], evaluation: Evaluation, profile: OpponentProfile) -> str:
    model = MODEL_LABELS.get(snap["opponent"], MODEL_LABELS["manual"])
    lines = [
        f"【推演结果】每个合法动作推演 {evaluation.games} 局：{OWN_LABEL}，{model}，打完为止。"
        "胜率只用来比较这一手的各个动作，绝对值偏低。从好到差：",
    ]
    for rank, value in enumerate(evaluation.ranked, 1):
        card = evaluation.cards[value.move]
        target = "null" if value.move[1] is None else value.move[1]
        gap, se = evaluation.gaps[value.move]
        compare = "推演胜率最高" if evaluation.top(value.move) else f"比最高少 {_pp(gap)} 个百分点（误差 ±{_pp(se)}）"
        lines.append(
            f"{rank}. skill={value.move[0]} target={target}｜{describe(snap, value.move)}｜胜率 {_pp(value.win_rate)}%，"
            f"平局 {_pp(value.draw_rate)}%｜{compare}｜直接后果：{fact_text(card)}，之后我方能量豆 {card['beans']}")
    if evaluation.flat and len(evaluation.ranked) > 1:
        lines.append("（所有动作的推演结果完全相同，分不出高下：请按你的判断原则选，basis 填 judgment。）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 会话

@dataclass
class Turn:
    index: int
    events: str
    state: str
    messages: list[dict[str, Any]]  # 这一轮 LLM 的回复和工具结果

    def user(self, full: bool) -> dict[str, Any]:
        content = f"{self.events}\n\n{self.state}".strip() if full else (self.events or "（局面推进了）") + "\n（当时的局面已省略）"
        return {"role": "user", "content": content}


_COMPACTED = "（早先的推演结果已省略）"


@dataclass
class BattleSession:
    """一局的整段对话（会话模式）。只追加已经完成、玩家看到了的轮次。"""

    match_id: str
    turns: list[Turn] = field(default_factory=list)
    full_from: int = 0  # 从这一轮起保留完整内容，之前的压缩
    evidence_next: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)
    rebuilt: bool = False  # 服务重启等原因丢了之前的对话、中途新开的

    def history(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for number, turn in enumerate(self.turns):
            full = number >= self.full_from
            messages.append(turn.user(full))
            for message in turn.messages:
                if not full and message["role"] == "tool" and len(message.get("content") or "") > 40:
                    message = {**message, "content": _COMPACTED}
                messages.append(message)
        return messages

    def commit(self, turn: Turn, evidence_used: int) -> None:
        self.turns.append(turn)
        self.evidence_next += evidence_used
        if len(self.turns) - self.full_from > COMPACT_AT:
            self.full_from = len(self.turns) - KEEP_FULL


# ---------------------------------------------------------------- Agent

@dataclass
class BattleResult:
    advice: dict[str, Any]
    evidence: dict[str, Any]
    trace: dict[str, Any]
    commit: Callable[[], None]  # 建议确实交给玩家之后再调用：会话模式把这一轮接到对话后面


class BattleAgent:
    def __init__(self, llm, mode: str = STEP, budget: runtime.Budget = BUDGET) -> None:
        if mode not in MODES:
            raise ValueError(f"未知的战斗 Agent 模式：{mode}")
        self.llm = llm
        self.mode = mode
        self.budget = budget

    def system_prompt(self) -> str:
        return f"{SYSTEM_PROMPT}\n\n【对话方式】{MODE_NOTES[self.mode]}"

    def tools(self, snap: dict[str, Any], state: str, memory: Memory | None) -> list[runtime.Tool]:
        def lessons(args: dict[str, Any]) -> str:
            if memory is None:
                return "经验库里还没有操作经验。"
            mine = {pet["species"] for pet in snap["static"]["pets"].values() if pet["side"] == "me"}
            theirs = {pet["species"] for pet in snap["static"]["pets"].values() if pet["side"] == "opp"}
            wanted = set(args.get("pets") or []) | mine
            found = [item for item in memory.lessons(snap["opponent"])
                     if item["scope"] == "play" and set(item["pets"]) & wanted
                     and (not item["against"] or set(item["against"]) & theirs)]
            found.sort(key=lambda item: -(item["support"] - item["contra"]))
            if not found:
                return "经验库里还没有相关的操作经验。"
            return "\n".join(
                f"- {'宜' if item['polarity'] == 'do' else '忌'}｜涉及 {names(item['pets'])}"
                + (f"，针对对手 {names(item['against'])}" if item["against"] else "")
                + f"｜{item['text']}（印证 {item['support']} 次）" for item in found[:4])

        return [
            runtime.Tool("search_lessons", "查之前复盘沉淀的操作经验（宜 / 忌），默认按我方上场宠物检索",
                         runtime.schema({"pets": {"type": "array", "items": {"type": "string", "enum": list(SPECIES_ORDER)}}}),
                         lessons),
            runtime.Tool("get_state", "重新查看当前完整局面和推演结果", runtime.schema(), lambda args: state),
            runtime.Tool("submit_move", "提交这次行动的建议（结束）", runtime.schema({
                "basis": {"type": "string", "enum": list(BASES),
                          "description": "engine：有推演胜率明显领先的动作，推荐的就是它；judgment：没有明显领先的，按你的判断原则选"},
                "skill": {"type": "string", "enum": ["basic", "special"]},
                "target": {"type": ["string", "null"], "description": "目标宠物编号；不需要目标的技能填 null"},
                "reason": {"type": "string", "description": "不超过 60 字"},
                "risk": {"type": "string", "description": "不超过 30 字，没有就写空字符串"},
                "plan": {"type": "string", "description": "不超过 40 字：本回合（和下回合开头）接下来怎么打"},
            }, ["basis", "skill", "target", "reason", "plan"])),
        ]

    def advise(self, snap: dict[str, Any], profile: OpponentProfile, evaluate: Callable[[], Evaluation], *,
               memory: Memory | None = None, last: LastAdvice | None = None, seen: int = 0,
               session: BattleSession | None = None, attempt: int = 1, feedback: str | None = None,
               on_commit: Callable[[LastAdvice, int], None] | None = None) -> BattleResult:
        """evaluate() 返回引擎对全部合法动作的评估（调用方负责缓存和预先计算；推演超时抛 AdviceFailure）。

        on_commit(这次的建议, 已经告诉 LLM 的事件数)：建议确实交给玩家之后调用，用来记住「上一次建议」。
        """
        evaluation = evaluate()  # 先把每个合法动作都推演一遍，结果直接写进用户消息
        events = events_text(snap, last, seen)
        state = f"{state_text(snap, profile)}\n\n{evaluation_text(snap, evaluation, profile)}"
        user = f"{events}\n\n{state}".strip()
        if feedback:
            user += f"\n\n【上次提交的问题】\n{feedback}"
        system = {"role": "system", "content": self.system_prompt()}
        if self.mode == SESSION and session is not None:
            with session.lock:
                history = session.history()
                first = session.evidence_next
                base_turns = len(session.turns)
            messages = [system, *history, {"role": "user", "content": user}]
        else:
            first, base_turns = 1, 0
            messages = [system, {"role": "user", "content": user}]

        hint = {"kind": "battle", "token": snap["token"],
                "moves": [(value.move[0], value.move[1], describe(snap, value.move)) for value in evaluation.ranked]}
        outcome = runtime.run(self.llm, messages, self.tools(snap, state, memory), self.budget, hint,
                              first_evidence=first)
        facts = Facts([SYSTEM_PROMPT, state, events, *outcome.evidence.values()])
        basis, move, reason, risk, plan = self.check(outcome.args, snap, facts, evaluation)

        advice = {
            "actor": snap["actor"], "skill": move[0], "target": move[1],
            "name": skill_name(snap, move[0]), "text": describe(snap, move),
            "basis": basis, "reason": reason, "risk": risk, "plan": plan,
            "facts": fact_text(evaluation.cards[move]),
        }
        ranked = [value.move for value in evaluation.ranked]
        evidence = evidence_for(snap, evaluation, move, basis)
        trace = {"attempt": attempt, "mode": self.mode, **outcome.trace(), "moves": len(ranked), "basis": basis,
                 "chosen_rank": ranked.index(move) + 1 if move in ranked else None,
                 "evaluated": evaluation.games,
                 "engine_ms": evaluation.ms, "rebuilt": bool(session and session.rebuilt and base_turns == 0)}
        current = LastAdvice(snap["index"], _label(snap, snap["actor"]), advice["text"], plan)
        event_count = len(snap.get("events") or [])

        def commit() -> None:
            if self.mode == SESSION and session is not None:
                with session.lock:
                    if len(session.turns) == base_turns:  # 期间没有别的轮次接上来（否则这一轮已经过时）
                        session.commit(Turn(snap["index"], events, state, outcome.messages), len(outcome.evidence))
            if on_commit is not None:
                on_commit(current, event_count)

        return BattleResult(advice, evidence, trace, commit)

    @staticmethod
    def check(args: dict[str, Any], snap: dict[str, Any], facts: Facts, evaluation: Evaluation | None = None,
              ) -> tuple[str, tuple[str, str | None], str, str, str]:
        """guard：动作合法、文字里的数字有出处；依据是「跟推演」时，动作必须是推演胜率最高的。
        不合格抛 AdviceFailure，原因写进下一次请求。返回（依据，动作，理由，风险，计划）。"""
        skill, target = args.get("skill"), args.get("target")
        if target in ("", "null", "None"):
            target = None
        option = next((o for o in snap["options"] if o["skill"] == skill), None)

        def fail(message: str, problem: str) -> AdviceFailure:
            return AdviceFailure(INVALID, message, feedback=f"- {problem}")

        if option is None:
            raise fail("AI 建议了不存在的动作", f'skill 必须是 "basic" 或 "special"，你写的是 {skill!r}')
        if not option["enabled"]:
            raise fail("AI 建议了不可用的技能", f"【{option['name']}】现在不可用：{option['reason']}")
        if option["targets"] is None:
            target = None
        elif target not in option["targets"]:
            raise fail("AI 建议的目标不合法",
                       f"【{option['name']}】的目标 {target!r} 不合法（已倒下或不可选），可选：{', '.join(option['targets'])}")
        basis = args.get("basis")
        if basis not in BASES:
            raise fail("AI 没有写明建议的依据", f'basis 必须是 "engine" 或 "judgment"，你写的是 {basis!r}')
        if basis == ENGINE and evaluation is not None and not evaluation.top((skill, target)):
            raise fail("AI 建议与推演不符",
                       f"你写的依据是 engine（跟推演），但【{option['name']}】不是推演胜率最高的动作："
                       f"要么改成推演胜率最高的动作，要么把 basis 改成 judgment 并说明理由")
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise fail("AI 没有给出理由", "缺少 reason")
        risk = args.get("risk") if isinstance(args.get("risk"), str) else ""
        plan = args.get("plan") if isinstance(args.get("plan"), str) else ""
        problems = facts.problems(reason, "reason") + facts.problems(risk, "risk") + facts.problems(plan, "plan")
        if problems:
            raise AdviceFailure(INVALID, "AI 建议没有通过校验", feedback="\n".join(f"- {p}" for p in problems))
        return basis, (skill, target), reason.strip()[:REASON_LIMIT], risk.strip()[:RISK_LIMIT], plan.strip()[:PLAN_LIMIT]
