"""LLM 代理的游戏环境：把 GameService 和现成的 AI Coach 能力包成 function-calling 工具。

- 动作工具 pick / act：LLM 给出的参数原样交给 GameService（与网页同一条路径），对手方由
  GameService 按「智能模式」自动应对，返回新产生的事件和新的局面。
- 只读工具：局面、知识库检索（knowledge.TOOLS）、AI 军师选将建议（CoachService.draft）、
  引擎推演评估（coach.battle.evaluate）、AI 战斗指导（CoachService.battle）。
- 审计：每次 act 之前，引擎都对当前局面做一次推演评估（不给 LLM 看，除非它自己调用
  evaluate_moves），用来事后统计 LLM 的选择和推演最优相差多少。它不影响对局。
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..coach import battle as battle_advice
from ..coach.config import OFF, CoachConfig
from ..coach.draft import profile_for
from ..coach.knowledge import TOOLS as KB_TOOLS
from ..coach.knowledge import KnowledgeBase, call_tool
from ..coach.rollout import fact_card, fact_text
from ..engine.battle import BASIC, SPECIAL
from ..engine.draft import PICK_ORDER
from ..engine.pets import SIDE_NAMES, SPECIES, SPECIES_ORDER
from ..service import GameError, GameService
from ..store import Store

OPPONENT = "smart"
ACTIONS = ("pick", "act")


def _fn(name: str, description: str, properties: dict[str, Any] | None = None,
        required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
    }}


TOOL_DEFS: dict[str, dict[str, Any]] = {
    "get_state": _fn("get_state", "查看当前完整局面：阶段、双方宠物和属性、生命护盾、能量豆、结界水链、行动队列、当前合法动作"),
    "pick": _fn(
        "pick", "选将：为我方选一只宠物（只在轮到我方选将时可用）。执行后对手方会自动应对，返回新局面",
        {"species": {"type": "string", "enum": list(SPECIES_ORDER), "description": "宠物 key"}}, ["species"]),
    "act": _fn(
        "act", "战斗行动：当前行动的我方宠物执行一个动作。执行后对手方会自动行动直到再次轮到我方，返回期间发生的事件和新局面",
        {"skill": {"type": "string", "enum": [BASIC, SPECIAL], "description": "basic 普通攻击 / special 特色技能"},
         "target": {"type": ["string", "null"], "description": "目标宠物编号，如 opp2、me1；不需要目标的技能填 null"}},
        ["skill", "target"]),
    **{tool["function"]["name"]: tool for tool in KB_TOOLS},
    "ask_draft_coach": _fn(
        "ask_draft_coach",
        "AI 军师选将建议（游戏内置）：推荐这一手选哪只、整套阵容计划、理由，以及按「照指导出招、对上智能对手」修正过的推演胜率和其他候选的胜率。只在轮到我方选将时可用，约需几秒"),
    "evaluate_moves": _fn(
        "evaluate_moves",
        "引擎推演评估（游戏内置）：把当前宠物的每个合法动作各推演若干局（双方接下来一整轮都按推演选择，智能对手按它的真实打法），返回每个动作的胜率、平局率、排名，以及这个动作的直接后果。只在轮到我方行动时可用"),
    "ask_battle_coach": _fn(
        "ask_battle_coach",
        "AI 战斗指导（游戏内置）：在引擎推演分不出高下的候选动作里推荐一个，并给出理由和风险。只在轮到我方行动时可用"),
}

TOOLSETS: dict[str, tuple[str, ...]] = {
    "full": ("get_state", "search_pet_experience", "search_combo_experience", "ask_draft_coach",
             "evaluate_moves", "ask_battle_coach", "pick", "act"),
    "engine": ("get_state", "search_pet_experience", "search_combo_experience", "evaluate_moves", "pick", "act"),
    "bare": ("get_state", "pick", "act"),
}


def pet_name(key: str) -> str:
    return SPECIES[key].name


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _n(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


class AgentEnv:
    """一局「LLM 操作我方 vs 智能对手」。data_dir 是这一局独立的存档目录（战斗记录按游戏的标准格式保存）。"""

    def __init__(self, data_dir: str | Path, seed: int, toolset: str | tuple[str, ...], config: CoachConfig,
                 *, workers: int = 0, audit: bool = True) -> None:
        self.tool_names = TOOLSETS[toolset] if isinstance(toolset, str) else tuple(toolset)
        unknown = [name for name in self.tool_names if name not in TOOL_DEFS]
        if unknown:
            raise ValueError(f"未知工具：{unknown}")
        rng = random.Random(seed)
        self.seed = seed
        self.game = GameService(Store(data_dir), new_seed=lambda: rng.getrandbits(32))
        self.workers = workers
        self.audit = audit
        self.profile = profile_for(OPPONENT)
        self.kb = KnowledgeBase.load()
        self.coach = None
        if {"ask_draft_coach", "ask_battle_coach"} & set(self.tool_names):
            from ..coach.service import CoachService  # 只有用到军师 / 指导时才建（它要连 LLM）

            self.coach = CoachService(self.game, replace(config, reflect=OFF), workers=workers)
        self.game.start_match(OPPONENT)  # 智能对手先选第一手
        self._seen = 0  # 已经交给 LLM 的战斗事件数
        self._evals: dict[str, battle_advice.Evaluation] = {}  # token → 引擎评估（LLM 调用或审计）
        self.decisions: list[dict[str, Any]] = []
        self._pending_tools: list[str] = []  # 上一次决策之后 LLM 调用过的只读工具
        self._advice: dict[str, Any] = {}  # token → 这个决策点上军师 / 指导给出的建议（统计是否采纳）
        self.illegal = 0

    # ------------------------------------------------------------ 状态

    @property
    def match(self):
        return self.game.match

    @property
    def finished(self) -> bool:
        return self.match is not None and self.match.phase == "finished"

    def tools(self) -> list[dict[str, Any]]:
        return [TOOL_DEFS[name] for name in self.tool_names]

    def _label(self, pet_id: str) -> str:
        pet = self.match.battle.static()["pets"][pet_id]
        return f"{pet_id} {SIDE_NAMES[pet['side']]}{pet['name']}"

    def state_text(self) -> str:
        match = self.match
        if match.battle is None:
            return self._draft_text()
        return self._battle_text()

    def _draft_text(self) -> str:
        match = self.match
        draft = match.draft
        step = len(draft.picks)
        turn = draft.turn
        lines = [f"【选将】第 {step + 1}/6 手，轮到{SIDE_NAMES[turn]}。选将顺序：" +
                 "、".join(SIDE_NAMES[side] for side in PICK_ORDER)]
        for side in ("opp", "me"):
            picked = draft.lineup(side)
            shown = "，".join(f"{i}号位 {pet_name(k)}({k})" for i, k in enumerate(picked, 1)) or "（还没有）"
            lines.append(f"{SIDE_NAMES[side]}已选：{shown}")
        mine = draft.lineup("me")
        lines.append("我方还可以选：" + "、".join(f"{k} {pet_name(k)}" for k in SPECIES_ORDER if k not in mine))
        lines.append("双方属性（生命上限 / 攻击 / 基础速度 / 效果命中）：")
        for side in ("me", "opp"):
            rows = [f"{pet_name(k)}({k}) {s['max_hp']}/{s['atk']}/{s['speed']}/{s['hit']}%"
                    for k, s in ((k, match.roster[side][k]) for k in SPECIES_ORDER)]
            lines.append(f"  {SIDE_NAMES[side]}：" + "；".join(rows))
        lines.append("（小兔、小鱼开战后速度再 +100）")
        return "\n".join(lines)

    def _battle_text(self) -> str:
        battle = self.match.battle
        view, static = battle.view(), battle.static()
        lines = []
        if battle.finished:
            result = {"me": "我方胜", "opp": "对手方胜", "draw": "平局"}[battle.winner]
            lines.append(f"【战斗结束】{result}（{battle.events[-1].get('text') or ''}），共进行到第 {battle.round} 回合")
        else:
            lines.append(f"【战斗】第 {battle.round}/10 回合，轮到 {self._label(battle.actor)} 行动")
        lines.append(f"能量豆：我方 {view['teams']['me']['beans']}，对手方 {view['teams']['opp']['beans']}（上限 8）")
        effects = battle_advice._effects({"view": view, "static": static})
        if effects:
            lines.extend(effects)
        for side in ("me", "opp"):
            lines.append(f"{SIDE_NAMES[side]}：")
            for pet_id, info in static["pets"].items():
                if info["side"] != side:
                    continue
                pet = view["pets"][pet_id]
                if not pet["alive"]:
                    lines.append(f"  {pet_id} {info['name']}：已倒下")
                    continue
                tags = []
                if pet_id in view["done"]:
                    tags.append("本回合已行动")
                elif pet_id in view["upcoming"] or pet_id == battle.actor:
                    tags.append("本回合待行动")
                if pet["linked"]:
                    tags.append("在水链上")
                shield = f" 护盾 {_n(pet['shield'])}" if pet["shield"] else ""
                lines.append(
                    f"  {pet_id} {info['name']}：生命 {_n(pet['hp'])}/{_n(info['max_hp'])}{shield}，攻击 {info['atk']}，"
                    f"速度 {info['speed']}" + (f"，命中 {info['hit']}%" if info["hit"] else "") +
                    (f"（{'，'.join(tags)}）" if tags else ""))
        if not battle.finished:
            upcoming = [self._label(pid) for pid in view["upcoming"] if pid != battle.actor]
            lines.append("本回合之后的行动顺序：" + (" → ".join(upcoming) or "（无，本回合结束）"))
            lines.append("当前合法动作：")
            for option in battle.options():
                head = f"  {option['skill']}【{option['name']}】" + (f"（{option['cost']} 豆）" if option["cost"] else "")
                if not option["enabled"]:
                    lines.append(f"{head}：不可用，{option['reason']}")
                elif option["targets"] is None:
                    lines.append(f"{head}：无需目标（target 填 null）")
                else:
                    lines.append(f"{head}：目标 " + "、".join(self._label(t) for t in option["targets"]))
        return "\n".join(lines)

    def _new_events(self) -> list[str]:
        battle = self.match.battle
        if battle is None:
            return []
        texts = [event["text"] for event in battle.events[self._seen:] if event.get("text")]
        self._seen = len(battle.events)
        return texts

    def opening_text(self) -> str:
        opp = self.match.draft.lineup("opp")
        return (f"新的一局开始了，对手是智能模式。对手方已经先选了 {pet_name(opp[0])}({opp[0]})。\n\n"
                + self.state_text())

    # ------------------------------------------------------------ 工具分发

    def call(self, name: str, args: dict[str, Any], rationale: str = "") -> dict[str, Any]:
        """执行一个工具，返回 {ok, text, data?}。text 是交给 LLM 的内容。"""
        if name not in self.tool_names:
            return {"ok": False, "text": f"没有这个工具：{name}"}
        if self.finished and name in ACTIONS:
            return {"ok": False, "text": "对局已经结束，不需要再行动。"}
        try:
            if name == "get_state":
                return {"ok": True, "text": self.state_text()}
            if name == "pick":
                return self._pick(args, rationale)
            if name == "act":
                return self._act(args, rationale)
            self._pending_tools.append(name)
            if name in ("search_pet_experience", "search_combo_experience"):
                rows = call_tool(self.kb, name, args)
                return {"ok": True, "text": json.dumps(rows, ensure_ascii=False)}
            if name == "ask_draft_coach":
                return self._draft_coach()
            if name == "evaluate_moves":
                return self._evaluate_moves()
            if name == "ask_battle_coach":
                return self._battle_coach()
        except (KeyError, TypeError, ValueError) as error:
            return {"ok": False, "text": f"参数不对：{type(error).__name__}: {error}"}
        return {"ok": False, "text": f"没有这个工具：{name}"}

    # ------------------------------------------------------------ 动作

    def _decision(self, kind: str, choice: Any, rationale: str, **extra: Any) -> dict[str, Any]:
        record = {"index": len(self.decisions), "kind": kind, "choice": choice, "rationale": rationale.strip(),
                  "tools": self._pending_tools, **extra}
        self._pending_tools = []
        self.decisions.append(record)
        return record

    def _pick(self, args: dict[str, Any], rationale: str) -> dict[str, Any]:
        species = args.get("species")
        match = self.match
        if match.battle is not None:
            return self._illegal("选将已经结束，现在是战斗阶段，请用 act。")
        step = len(match.draft.picks)
        token = self.game.coach_token()
        try:
            self.game.pick("me", species)
        except GameError as error:
            return self._illegal(f"选将失败：{error}")
        opp_reply = match.draft.picks[step + 1][1] if len(match.draft.picks) > step + 1 else None
        self._decision("pick", species, rationale, hand=step + 1, opp_reply=opp_reply,
                       coach=self._advice.get(token))
        parts = [f"我方第 {len(match.draft.lineup('me'))} 只选了 {pet_name(species)}。"]
        if opp_reply:
            parts.append(f"对手方接着选了 {pet_name(opp_reply)}({opp_reply})。")
        if match.battle is not None:
            events = self._new_events()
            self.decisions[-1]["events"] = events
            parts.append("选将完成，战斗开始。\n战斗事件：\n" + "\n".join(events))
        return {"ok": True, "text": "\n".join(parts) + "\n\n" + self.state_text()}

    def _act(self, args: dict[str, Any], rationale: str) -> dict[str, Any]:
        match = self.match
        if match.battle is None:
            return self._illegal("现在还在选将阶段，请用 pick。")
        skill, target = args.get("skill"), args.get("target")
        if target in ("", "null", "None"):
            target = None
        battle = match.battle
        actor, index, round_ = battle.actor, len(battle.actions), battle.round
        token = self.game.coach_token()
        legal = (skill, target) in battle.legal_moves()
        audit = self._audit(token, (skill, target)) if legal else None
        try:
            self.game.action(skill, target)
        except GameError as error:
            return self._illegal(f"行动失败：{error}。局面没有变化。")
        self._decision("act", [skill, target], rationale, index=index, round=round_, actor=actor,
                       species=battle.pets[actor].species, audit=audit, coach=self._advice.get(token))
        events = self._new_events()
        self.decisions[-1]["events"] = events
        return {"ok": True, "text": "发生的事件：\n" + "\n".join(events) + "\n\n" + self.state_text()}

    def _illegal(self, text: str) -> dict[str, Any]:
        self.illegal += 1
        return {"ok": False, "text": text}

    # ------------------------------------------------------------ 只读工具

    def _evaluation(self, token: str) -> battle_advice.Evaluation:
        evaluation = self._evals.get(token)
        if evaluation is None:
            snap = self.game.coach_battle_snapshot()
            evaluation = battle_advice.evaluate(snap, self.profile, games=24, workers=self.workers)
            self._evals[token] = evaluation
        return evaluation

    def _audit(self, token: str, move: tuple) -> dict[str, Any] | None:
        if not self.audit and token not in self._evals:
            return None
        evaluation = self._evaluation(token)
        ranked = evaluation.ranked
        chosen = next(value for value in ranked if value.move == move)
        return {
            "rank": ranked.index(chosen) + 1, "moves": len(ranked), "tier": evaluation.tier(chosen),
            "value": round(chosen.value, 3), "best": list(ranked[0].move), "best_value": round(ranked[0].value, 3),
            "gap": round(ranked[0].value - chosen.value, 3),
            "candidates": [list(value.move) for value in evaluation.candidates],
        }

    def _require_my_battle_turn(self) -> str | None:
        snap = self.game.coach_battle_snapshot()
        if snap is None or snap["turn"] != "me":
            return None
        return snap["token"]

    def _evaluate_moves(self) -> dict[str, Any]:
        token = self._require_my_battle_turn()
        if token is None:
            return {"ok": False, "text": "现在不是我方的战斗行动回合。"}
        evaluation = self._evaluation(token)
        battle = self.match.battle
        snap = {"options": battle.options(), "static": battle.static(), "actor": battle.actor}
        lines = [f"推演评估（每个动作 {evaluation.games} 局；价值 = 胜率 + 0.5 × 平局率，从好到差）："]
        for rank, value in enumerate(evaluation.ranked, 1):
            card = evaluation.cards.get(value.move) or fact_card(battle, value.move)
            lines.append(
                f"{rank}. act({value.move[0]}, {value.move[1]}) {battle_advice.describe(snap, value.move)}："
                f"胜率 {_pct(value.win_rate)}，平局 {_pct(value.draw_rate)}，{evaluation.tier(value)}。直接后果：{fact_text(card)}")
        return {"ok": True, "text": "\n".join(lines)}

    def _draft_coach(self) -> dict[str, Any]:
        if self.coach is None:
            return {"ok": False, "text": "AI 军师不可用。"}
        token = self.game.coach_token()
        result = self.coach.draft(token)
        if not result.get("ok"):
            return {"ok": False, "text": f"AI 军师这次没给出建议：{result.get('message')}"}
        advice, evidence = result["advice"], result["evidence"]
        chosen = evidence["chosen"]
        lines = [
            f"推荐这一手选：{pet_name(advice['pet'])}({advice['pet']})",
            f"整套阵容计划（按站位）：{'、'.join(f'{pet_name(k)}({k})' for k in advice['plan'])}",
            f"理由：{advice['reason']}",
            f"打法：{advice['strategy']}",
            f"推演胜率（{evidence['label']}）：{_pct(chosen['win_rate'])}；最坏情况（对手补 "
            f"{'、'.join(pet_name(k) for k in chosen['worst_reply'])}）胜率 {_pct(chosen['worst_win_rate'])}",
        ]
        if advice.get("opp_read"):
            lines.append(f"对手判断：{advice['opp_read']}")
        for alt in advice.get("alternatives") or []:
            lines.append(f"备选：{pet_name(alt['pet'])}({alt['pet']})，计划 {'、'.join(alt['plan'])}，"
                         f"胜率 {_pct(alt['win_rate'])}。{alt['reason']}")
        ranking = "；".join(f"{pet_name(row['pet'])} {_pct(row['win_rate'])}" for row in evidence.get("ranking", []))
        if ranking:
            lines.append(f"这一手各候选的推演胜率：{ranking}")
        self._advice[token] = advice["pet"]
        return {"ok": True, "text": "\n".join(lines)}

    def _battle_coach(self) -> dict[str, Any]:
        if self.coach is None:
            return {"ok": False, "text": "AI 战斗指导不可用。"}
        token = self._require_my_battle_turn()
        if token is None:
            return {"ok": False, "text": "现在不是我方的战斗行动回合。"}
        result = self.coach.battle(token)
        if not result.get("ok"):
            return {"ok": False, "text": f"AI 战斗指导这次没给出建议：{result.get('message')}"}
        advice = result["advice"]
        best = result["evidence"]["moves"][0]
        text = (f"推荐：act({advice['skill']}, {advice['target']})。理由：{advice['reason']}"
                + (f" 风险：{advice['risk']}" if advice.get("risk") else "")
                + f"（推演胜率 {_pct(best['win_rate'])}）")
        self._advice[token] = [advice["skill"], advice["target"]]
        return {"ok": True, "text": text}

    # ------------------------------------------------------------ 结果

    def result(self) -> dict[str, Any]:
        match = self.match
        battle = match.battle
        summary: dict[str, Any] = {
            "match_id": match.id, "seed": self.seed, "finished": self.finished,
            "picks": match.draft.picks,
            "lineups": {side: match.draft.lineup(side) for side in ("me", "opp")},
        }
        if battle is not None:
            summary.update(winner=battle.winner, rounds=battle.round, actions=len(battle.actions),
                           reason=battle.events[-1].get("reason") if battle.finished else None)
        acts = [d for d in self.decisions if d["kind"] == "act" and d.get("audit")]
        if acts:
            summary["audit"] = {
                "decisions": len(acts),
                "best": sum(d["audit"]["rank"] == 1 for d in acts),
                "near_best": sum(d["audit"]["tier"] in ("最优", "与最优相当") for d in acts),
                "mean_gap": round(sum(d["audit"]["gap"] for d in acts) / len(acts), 4),
                "big_mistakes": sum(d["audit"]["gap"] >= 0.1 for d in acts),
            }
        return summary

