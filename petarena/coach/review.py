"""战后复盘：把打完的一局重演一遍，逐步对照推演，给出归因和可以沉淀的经验。

引擎部分（analyse，不联网）：

1. 先确认这份记录能逐事件重演；规则改过之后的旧记录做不到，只保留结果，不进经验库。
2. 我方每个决策点：把全部合法动作各推演一遍，找出推演最优的动作；再用一批没参与挑选的
   新种子，把「最优 / 玩家实际 / 常规打法会选的」这三个动作重新估一遍（从很多动作里挑
   最大值，挑中的往往是运气好的，直接比会夸大失误）。差距超过两倍标准误才评为失误。
3. 归因（attribute）：

       结果 − 0.5 = 属性底子 + 阵容 + 操作 + 运气与对手发挥

   属性底子 = 现有属性下对这套对手阵容最好的阵容的胜算 − 0.5；阵容 = 实际阵容 − 最好的阵容；
   操作 = −（各步比推演最优少赚的胜算之和，不超过实际阵容本来有的胜算）；最后一项是余下的
   部分（冻结、补刀、同速，以及对手比预想的打得好或差）。这是一条恒等式，四项相加正好等于
   结果，而且每一项都在 ±100% 以内。
   「胜算」先按对阵表（双方常规打法）算；定向推演完成后改用「照指导出招、对上对手真实的打法」
   的整局推演结果（见 experience.reflect）：对上会推演的智能对手时，两个口径差得很远。

LLM 部分（ReviewAdvisor）：把带编号的事实讲成复盘，并提炼最多 3 条经验条目。经验必须
引用给出的事实编号、只涉及合法的宠物，不合格的逐条丢弃。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..engine import Battle
from ..engine.pets import SPECIES
from .battle import MODEL_LABELS, OWN_LABEL
from .draft import PREDICTED, AdviceFailure, OpponentProfile, parse_json
from .llm import LLM
from .matchup import ORDERED_LINEUPS, Lineup, MatchupBook
from .memory import REAL, signature
from .policy import Move, choose
from .rollout import OWN, ROLLOUT_GAMES, evaluate_moves, near_best, paired_diff, pool_map, rank

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "review_system.md").read_text(encoding="utf-8").strip()

BLUNDER, MISTAKE, SLIP = 0.25, 0.10, 0.05  # 损失多少算重大失误 / 失误 / 小瑕疵
GRADE_NAMES = {"best": "最优", "good": "不错", "only": "别无选择", "slip": "小瑕疵", "mistake": "失误", "blunder": "重大失误"}
BAD_GRADES = ("slip", "mistake", "blunder")
SWING = 0.2  # 两次我方决策之间局面价值变化超过这么多，算一次转折
PRAISE = 0.15  # 比常规打法多赚这么多，值得表扬
LINEUP_GAP = 0.15  # 最好的阵容比实际阵容高出这么多，主因算阵容
PLAY_GAP = 0.15  # 操作上一共少赚这么多，主因算操作
SHORTLIST = 20  # 属性有成长、算不起穷举时，用基础属性的表挑出这么多套阵容来现场估计
GUIDE_GAMES = 48

CAUSES = {
    "win": "赢下了这一局", "train": "属性底子不够", "lineup": "阵容被克制", "play": "操作上有明显失误",
    "luck": "运气和对手发挥", None: "还在分析",
}
UNLUCKY = 0.4  # 结论是「属性」，但运气那一项比这还差（实际阵容本来有四成以上的胜算）：标题里两件事都说


def cause_text(verdict: str | None, attribution: dict[str, float] | None = None) -> str:
    """结论的标题。最好的阵容离目标不远（例如 55% 对 60%）时，结论是「属性底子不够」，但输掉这一局本身
    多半是运气：归因图上属性那一项是正的、运气那一项是一大截负的。标题只写「属性底子不够」就和图对不上。"""
    if verdict == "train" and attribution and attribution.get("luck", 0.0) < min(attribution.get("stats", 0.0), -UNLUCKY):
        return "这一局输在运气上；想赢得更稳，属性底子还要补"
    return CAUSES.get(verdict, CAUSES[None])


TABLE = "table"  # 归因的口径：对阵表（双方常规打法）
GUIDED = "guided"  # 归因的口径：照指导出招、对上对手真实打法的整局推演
SCOPES = ("lineup", "matchup", "play")
POLARITIES = ("do", "avoid")
INVALID = "invalid"

LEGACY = "legacy"
READY = "ready"


def outcome_of(winner: str) -> float:
    return 1.0 if winner == "me" else 0.5 if winner == "draw" else 0.0


def replayable(record: dict[str, Any]) -> bool:
    """这份记录按当前规则能否逐事件重演。只比胜负不够：结局相同、过程不同的旧记录是有的。"""
    if not record.get("roster"):
        return False
    try:
        replay = Battle.replay(record["lineups"], record["seed"], record["actions"])
    except Exception:
        return False
    return replay.events == record["events"]


def attribute(outcome: float, ceiling: float, played: float, missed: float) -> dict[str, float]:
    """四项归因。ceiling / played 是最好的阵容和实际阵容的胜算，missed 是各步比推演最优少赚的胜算之和。"""
    cost = min(max(missed, 0.0), played)  # 操作再差，丢掉的也不会超过本来有的胜算
    return {"stats": ceiling - 0.5, "lineup": played - ceiling, "play": -cost, "luck": outcome - played + cost}


def decide_verdict(outcome: float, ceiling: float | None, played: float | None, missed: float, goal: float) -> str | None:
    """这一局主要该从哪里改进。ceiling / played 是「照指导出招」时最好的阵容和实际阵容的价值。

    先看不花成本就能改的：有明显更好的阵容 → 阵容。阵容没问题但最好的阵容也到不了目标 → 属性
    （要去培养）。再往下才是操作和运气。换了阵容还到不了目标的情况，由培养建议接着处理。
    """
    if outcome >= 1.0:
        return "win"
    if ceiling is None or played is None:
        return None  # 还没推演过：先不下结论
    if ceiling - played >= LINEUP_GAP:
        return "lineup"
    if ceiling < goal:
        return "train"
    if missed >= PLAY_GAP:
        return "play"
    return "luck"


# ---------------------------------------------------------------- 逐步分析

def _text(battle: Battle, move: Sequence[str | None]) -> str:
    skill, target = move
    name = next(option["name"] for option in battle.options() if option["skill"] == skill)
    if target is None:
        return name
    return f"{name} → {'自己' if target == battle.actor else battle.label(target)}"


def _decision(battle: Battle, index: int, played: Move, token: str, profile: OpponentProfile,
              games: int, mark: dict[str, Any] | None) -> dict[str, Any]:
    actor = battle.pets[battle.actor]
    moves = battle.legal_moves()
    heur = tuple(choose(battle, random.Random(0)))
    models = {"own": OWN, "other": profile.model}  # 与战斗指导相同：往前看一整轮
    if len(moves) == 1:
        only = evaluate_moves(battle, games=games, salt=token + "|fresh", moves=moves, **models)[0]
        best, grade, loss, se = played, "only", 0.0, 0.0
        v_best = v_played = v_heur = only.value
        ranked = [only]
    else:
        # 与战斗指导用同一个 salt：玩家照着建议打的那一步，复盘时不会被判成失误
        values = evaluate_moves(battle, games=games, salt=token, **models)
        ranked = rank(values)
        best = ranked[0].move
        near = {value.move for value in near_best(values)}
        focus = list(dict.fromkeys([best, played, heur]))
        fresh = {value.move: value for value in
                 evaluate_moves(battle, games=games * 2, salt=token + "|fresh", moves=focus, **models)}
        v_best, v_played, v_heur = fresh[best].value, fresh[played].value, fresh[heur].value
        loss, se = paired_diff(fresh[best], fresh[played]) if played != best else (0.0, 0.0)
        if played == best:
            grade = "best"
        elif played in near or loss <= 2 * se:
            grade = "good"
        elif loss >= BLUNDER:
            grade = "blunder"
        elif loss >= MISTAKE:
            grade = "mistake"
        elif loss >= SLIP:
            grade = "slip"
        else:
            grade = "good"
        if mark and mark["adopted"] and grade in BAD_GRADES:
            grade = "good"  # 照着建议打的一步不算失误：建议本身就是引擎当时认可的候选
    return {
        "idx": index, "round": battle.round, "actor": actor.id, "species": actor.species,
        "label": battle.label(actor.id),
        "played": list(played), "best": list(best), "heur": list(heur),
        "played_text": _text(battle, played), "best_text": _text(battle, best), "heur_text": _text(battle, heur),
        "v_played": round(v_played, 4), "v_best": round(v_best, 4), "v_heur": round(v_heur, 4),
        "loss": round(loss, 4), "se": round(se, 4), "grade": grade,
        "advised": mark["advised"] if mark else None, "adopted": mark["adopted"] if mark else None,
        "options": [{"skill": v.move[0], "target": v.move[1], "text": _text(battle, v.move), "value": round(v.value, 3)}
                    for v in ranked[:5]],
    }


def _lineups(book: MatchupBook, prior: MatchupBook | None, my: Lineup, opp: Lineup) -> dict[str, Any]:
    """常规打法口径下：实际阵容的价值，以及对这套对手阵容最好的几套阵容。"""
    if book.complete:
        candidates = list(ORDERED_LINEUPS)
    elif prior is not None and prior.complete:
        guide = prior.get_many([(lineup, opp) for lineup in ORDERED_LINEUPS])
        order = sorted(range(len(ORDERED_LINEUPS)), key=lambda i: -guide[i].value)
        candidates = list(dict.fromkeys([ORDERED_LINEUPS[i] for i in order[:SHORTLIST]] + [my]))
    else:
        candidates = [my]
    results = book.get_many([(lineup, opp) for lineup in candidates], GUIDE_GAMES)
    values = {lineup: result.value for lineup, result in zip(candidates, results)}
    top = sorted(candidates, key=lambda lineup: -values[lineup])[:3]
    if not book.complete and len(candidates) > 1:  # 现场估计的前几名用新种子复核，避免挑中运气好的
        for lineup, result in zip(top, book.verify([(lineup, opp) for lineup in top], GUIDE_GAMES * 2)):
            values[lineup] = result.value
        top.sort(key=lambda lineup: -values[lineup])
    p0 = values[my]
    best = top[0] if values[top[0]] > p0 else my
    return {
        "p0": round(p0, 4), "best": list(best), "best_p0": round(values[best], 4),
        "top": [{"lineup": list(lineup), "p0": round(values[lineup], 3)} for lineup in top],
    }


def _names(keys: Sequence[str]) -> str:
    return "、".join(SPECIES[key].name for key in keys)


def _random_notes(events: Sequence[dict[str, Any]]) -> list[str]:
    notes = []
    for event in events:
        if event["type"] in ("pursuit_check", "freeze_check", "frozen") and event.get("text"):
            notes.append(event["text"])
        elif event["type"] == "round_start" and event.get("tied"):
            notes.append(f"第 {event['round']} 回合同速的宠物随机决定了先后")
    return notes


@dataclass
class Analysis:
    row: dict[str, Any]  # battles 表的一行（不含讲解）
    decisions: list[dict[str, Any]] = field(default_factory=list)
    experience: list[dict[str, Any]] = field(default_factory=list)

    @property
    def facts(self) -> dict[str, Any]:
        return self.row["facts"]


def legacy_row(record: dict[str, Any]) -> dict[str, Any]:
    """按当前规则无法重演的旧记录：只留结果。"""
    lineups = record["lineups"]
    winner = record["result"]["winner"]
    return {
        "id": record["id"], "ended_at": record.get("ended_at"), "opponent": record.get("opponent", "manual"),
        "winner": winner, "rounds": record["result"]["rounds"], "status": LEGACY, "outcome": outcome_of(winner),
        "my_lineup": [p["species"] for p in lineups["me"]], "opp_lineup": [p["species"] for p in lineups["opp"]],
        "reflected": True, "verdict": None, "facts": {"status": LEGACY},
    }


def _decision_job(job: tuple) -> dict[str, Any]:
    """进程池任务：评估一个决策点。"""
    return _decision(*job)


def analyse(record: dict[str, Any], *, book: MatchupBook, profile: OpponentProfile,
            prior: MatchupBook | None = None, games: int = ROLLOUT_GAMES, goal: float = 0.6,
            shown: float | None = None, workers: int = 0) -> Analysis:
    """引擎复盘。book 是这一局锁定属性、对应对手打法的对阵表；prior 是基础属性的完整表（可选）。
    workers > 1 时各个决策点分给进程池去评估（一局十几个决策点，每个都要往前看一整轮）。"""
    started = time.monotonic()
    if not replayable(record):
        return Analysis(legacy_row(record))

    opponent = record.get("opponent", "manual")
    lineups = record["lineups"]
    my = tuple(p["species"] for p in lineups["me"])
    opp = tuple(p["species"] for p in lineups["opp"])
    winner = record["result"]["winner"]
    outcome = outcome_of(winner)
    marks = {int(index): mark for index, mark in (record.get("marks") or {}).items()}
    action_events = [event["i"] for event in record["events"] if event["type"] == "action"]

    battle = Battle.new(lineups, record["seed"], record=False)
    points: list[tuple] = []  # 我方的每个决策点：当时的局面和实际的动作
    for index, (skill, target) in enumerate(record["actions"]):
        if battle.pets[battle.actor].side == "me":
            token = f"{record['id']}:battle:{index}"
            points.append((battle.clone(), index, (skill, target), token, profile, games, marks.get(index)))
        battle.act(skill, target)
    decisions: list[dict[str, Any]] = pool_map(_decision_job, points, workers)

    table = _lineups(book, prior, my, opp)
    p0, best_p0 = table["p0"], table["best_p0"]
    real = [d for d in decisions if d["grade"] != "only"]
    gap = sum(d["v_best"] - d["v_heur"] for d in real)
    captured = sum(d["v_played"] - d["v_heur"] for d in real)
    missed = sum(d["v_best"] - d["v_played"] for d in real)
    attribution = attribute(outcome, best_p0, p0, missed)

    # ---- 带编号的事实：讲解和经验条目只能引用这些
    moments: list[dict[str, Any]] = []
    bad = sorted((d for d in decisions if d["grade"] in BAD_GRADES), key=lambda d: -d["loss"])
    for d in bad[:3]:
        moments.append({
            "id": f"D{d['idx']}", "kind": d["grade"], "round": d["round"], "idx": d["idx"],
            "text": (f"第 {d['round']} 回合 {d['label']}：实际「{d['played_text']}」，推演更好的是「{d['best_text']}」，"
                     f"这一步少了约 {round(d['loss'] * 100)}% 的胜算（{GRADE_NAMES[d['grade']]}）"),
        })
    gains = sorted((d for d in real if d["grade"] in ("best", "good") and d["v_played"] - d["v_heur"] >= PRAISE),
                   key=lambda d: d["v_heur"] - d["v_played"])
    for d in gains[:2]:
        moments.append({
            "id": f"G{d['idx']}", "kind": "good", "round": d["round"], "idx": d["idx"],
            "text": (f"第 {d['round']} 回合 {d['label']}：「{d['played_text']}」比常规打法（{d['heur_text']}）"
                     f"多赚约 {round((d['v_played'] - d['v_heur']) * 100)}% 的胜算"),
        })
    swings = []
    for a, b in zip(decisions, decisions[1:] + [None]):
        after = b["v_heur"] if b is not None else outcome
        change = after - a["v_played"]
        if abs(change) < SWING:
            continue
        start = action_events[a["idx"]]
        stop = action_events[b["idx"]] if b is not None else len(record["events"])
        notes = _random_notes(record["events"][start + 1:stop])
        if notes:
            swings.append((abs(change), a, change, notes))
    swings.sort(key=lambda item: -item[0])
    for _, a, change, notes in swings[:2]:
        moments.append({
            "id": f"L{a['idx']}", "kind": "luck", "round": a["round"], "idx": a["idx"],
            "text": (f"第 {a['round']} 回合之后局面对我方{'有利' if change > 0 else '不利'}了约 {round(abs(change) * 100)}%："
                     + "；".join(notes[:3])),
        })
    if profile.replies == PREDICTED:
        # 对手的补选会跟着我方变：「换一套阵容对上同一套对手阵容」不成立，这里只说实际这套；换阵容的结论等定向推演（A3）
        moments.append({
            "id": "A1", "kind": "lineup", "round": 0, "idx": -1,
            "text": f"对上{_names(opp)}，实际的{_names(my)}按常规打法约 {round(p0 * 100)}% 的胜算（双方都不推演时的参考值）",
        })
    elif list(my) != table["best"] and best_p0 - p0 >= 0.05:
        moments.append({
            "id": "A1", "kind": "lineup", "round": 0, "idx": -1,
            "text": (f"对上{_names(opp)}，按常规打法{_names(table['best'])}（按站位）有约 {round(best_p0 * 100)}% 的胜算，"
                     f"实际的{_names(my)}约 {round(p0 * 100)}%"),
        })
    else:
        moments.append({
            "id": "A1", "kind": "lineup", "round": 0, "idx": -1,
            "text": f"对上{_names(opp)}，实际的{_names(my)}已经是按常规打法最好的阵容之一（约 {round(p0 * 100)}% 的胜算）",
        })

    good = sum(d["grade"] in ("best", "good") for d in real)
    advised = [d for d in decisions if d["advised"] is not None]
    verdict = decide_verdict(outcome, None, None, missed, goal)
    facts = {
        "status": READY, "winner": winner, "outcome": outcome, "rounds": record["result"]["rounds"],
        "opponent": opponent, "my_lineup": list(my), "opp_lineup": list(opp),
        "table": table, "attribution": {key: round(value, 4) for key, value in attribution.items()},
        "basis": TABLE, "decisions": decisions, "moments": moments,
        "quality": {
            "decisions": len(real), "good": good, "gap": round(gap, 4), "captured": round(captured, 4),
            "missed": round(missed, 4), "advised": len(advised), "adopted": sum(bool(d["adopted"]) for d in advised),
        },
        "games": games, "shown": shown,
        "label": f"逐步评估：每个动作推演 {games} 局，{OWN_LABEL}，{MODEL_LABELS.get(opponent, MODEL_LABELS['manual'])}",
        "ms": int((time.monotonic() - started) * 1000),
    }
    sig = signature(record["roster"], my)
    row = {
        "id": record["id"], "ended_at": record.get("ended_at"), "opponent": opponent, "winner": winner,
        "rounds": record["result"]["rounds"], "my_lineup": list(my), "opp_lineup": list(opp), "my_sig": sig,
        "status": READY, "outcome": outcome, "p0": p0, "best_p0": best_p0, "best_lineup": table["best"],
        "shown": shown, "attr_stats": attribution["stats"], "attr_lineup": attribution["lineup"],
        "attr_play": attribution["play"], "attr_luck": attribution["luck"],
        "n_decisions": len(real), "n_good": good, "n_advised": len(advised),
        "n_adopted": sum(bool(d["adopted"]) for d in advised), "gap": gap, "captured": captured, "missed": missed,
        "reflected": False, "verdict": verdict, "facts": facts,
    }
    experience = [{
        "opponent": opponent, "my_lineup": my, "opp_lineup": opp, "my_sig": sig, "source": REAL,
        "win": float(winner == "me"), "draw": float(winner == "draw"), "games": 1.0, "p0": p0,
    }]
    return Analysis(row, decisions, experience)


# ---------------------------------------------------------------- LLM 讲解

def _signed(value: float) -> str:
    points = round(value * 100)
    return "0%" if points == 0 else f"{'+' if points > 0 else '−'}{abs(points)}%"


def lesson_pets(facts: dict[str, Any]) -> list[str]:
    """经验条目里可以提到的我方宠物：这一局上场的，加上事实里推荐过的阵容中的。"""
    keys = list(facts["my_lineup"])
    for lineup in ((facts.get("table") or {}).get("best"), (facts.get("ceiling") or {}).get("lineup")):
        keys += [key for key in lineup or [] if key not in keys]
    return keys


def better_lineup(facts: dict[str, Any]) -> list[str] | None:
    """引擎有没有找到明显更好的阵容（和「阵容」结论同一个门槛）：有就返回它，没有返回 None。"""
    ceiling = facts.get("ceiling") or {}
    if ceiling.get("lineup") and ceiling["value"] - ceiling["played"] >= LINEUP_GAP:
        return list(ceiling["lineup"])
    return None


def lineup_conflict(facts: dict[str, Any], polarity: str, pets: Sequence[str]) -> str | None:
    """阵容方面的经验条目是否与引擎的结论矛盾；矛盾就返回原因。

    实测 LLM 会拿「常规打法下只有 4%」这条参考值，把引擎认定最好的那套阵容写成「别用」——这样的条目
    进了经验库，下一次选将时会把军师往更差的阵容上带。
    """
    played, better = set(facts["my_lineup"]), better_lineup(facts)
    if polarity == "avoid" and better is None and set(pets) <= played:
        return "与引擎的结论矛盾：推演里没有明显更好的阵容，不能把实际这套写成「别用」（胜算不够是属性的问题）"
    if polarity == "do" and better is not None and set(pets) <= played and not set(pets) <= set(better):
        return "与引擎的结论矛盾：引擎推荐换阵容，不能把实际这套写成推荐"
    return None


def build_report_message(facts: dict[str, Any], feedback: str | None = None) -> str:
    result = {"me": "我方获胜", "opp": "对手方获胜", "draw": "平局"}[facts["winner"]]
    attribution = facts["attribution"]
    quality = facts["quality"]
    lines = [
        "【对局】",
        f"结果：{result}（第 {facts['rounds']} 回合结束）。对手方由{ {'random': '电脑随机', 'smart': '电脑智能', 'manual': '玩家手动'}.get(facts['opponent'], '玩家手动') }操作。",
        f"我方阵容（按站位）：{_names(facts['my_lineup'])}；对手方阵容：{_names(facts['opp_lineup'])}。",
        f"引擎判断的主因：{cause_text(facts.get('verdict'), facts.get('attribution'))}。",
        "",
        f"【四项归因】（正数对我方有利；口径：{'照指导出招的整局推演' if facts.get('basis') == GUIDED else '对阵表，双方常规打法'}）",
        f"属性底子 {_signed(attribution['stats'])}｜阵容 {_signed(attribution['lineup'])}｜操作 {_signed(attribution['play'])}"
        f"｜运气与对手发挥 {_signed(attribution['luck'])}",
        f"我方有选择余地的决策 {quality['decisions']} 次，其中 {quality['good']} 次打到了推演最优或与之相当；"
        + (f"AI 给了 {quality['advised']} 次建议，采纳 {quality['adopted']} 次。" if quality["advised"] else "这一局没有使用 AI 指导。"),
    ]
    if facts.get("ceiling"):
        ceiling = facts["ceiling"]
        lines.append(
            f"照指导出招的推演（{ceiling['games']} 局）：对这套对手阵容最好的是{_names(ceiling['lineup'])}，约 "
            f"{round(ceiling['value'] * 100)}% 胜算；实际阵容约 {round(ceiling['played'] * 100)}%。")
    lines += ["", "【事实】（只能引用这些编号）"]
    lines += [f"{moment['id']}：{moment['text']}" for moment in facts["moments"]]
    lines += ["", "lessons 里可以用的宠物 key —— pets（我方）：" + "、".join(f"{key} {SPECIES[key].name}" for key in lesson_pets(facts))
              + "；against（对手方）：" + "、".join(f"{key} {SPECIES[key].name}" for key in facts["opp_lineup"])]
    if feedback:
        lines += ["", "【上次输出的问题】", feedback]
    return "\n".join(lines)


def hint_for(facts: dict[str, Any], battle_id: str) -> dict[str, Any]:
    return {
        "kind": "review", "token": f"review:{battle_id}",
        "result": {"me": "赢了", "opp": "输了", "draw": "打平了"}[facts["winner"]],
        "cause": cause_text(facts.get("verdict"), facts.get("attribution")),
        "moments": [{"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in facts["moments"]],
        "my_lineup": facts["my_lineup"], "opp_lineup": facts["opp_lineup"],
    }


def read_report(text: str, finish_reason: str, facts: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """LLM 原始输出 → (讲解, 被丢弃条目的原因)。缺少总评或建议时抛 AdviceFailure。"""
    ids = {moment["id"] for moment in facts["moments"]}
    mine, theirs = set(lesson_pets(facts)), set(facts["opp_lineup"])

    def fail(problem: str) -> AdviceFailure:
        return AdviceFailure(INVALID, "AI 输出不合格", feedback=(
            f"你上一次的输出存在以下问题：\n- {problem}\n可以引用的事实编号：{', '.join(sorted(ids))}。请重新输出完整 json。"))

    try:
        data = parse_json(text)
    except ValueError:
        raise fail("输出不是完整的 json" + ("（被截断了，请写短一些）" if finish_reason == "length" else "")) from None
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise fail("缺少 summary")
    tips = [tip.strip()[:80] for tip in data.get("tips") or [] if isinstance(tip, str) and tip.strip()][:3]
    if not tips:
        raise fail("tips 至少要有 1 条")

    dropped: list[str] = []
    moments = []
    for item in data.get("moments") or []:
        if isinstance(item, dict) and item.get("id") in ids and isinstance(item.get("comment"), str) and item["comment"].strip():
            if len(moments) < 3:
                moments.append({"id": item["id"], "comment": item["comment"].strip()[:120]})
        else:
            dropped.append(f"点评 {item!r}：编号不在给出的事实里")
    lessons = []
    for item in data.get("lessons") or []:
        label = f"经验 {item.get('text') if isinstance(item, dict) else item!r}"
        if not isinstance(item, dict):
            dropped.append(f"{label}：不是对象")
            continue
        pets, against, evidence = item.get("pets") or [], item.get("against") or [], item.get("evidence") or []
        text_ = item.get("text")
        if item.get("scope") not in SCOPES or item.get("polarity") not in POLARITIES:
            dropped.append(f"{label}：scope / polarity 不合法")
        elif not isinstance(text_, str) or not text_.strip():
            dropped.append(f"{label}：缺少 text")
        elif not isinstance(pets, list) or not pets or any(key not in mine for key in pets):
            dropped.append(f"{label}：pets 必须是这一局我方上场的宠物，或事实里推荐过的阵容中的宠物")
        elif not isinstance(against, list) or any(key not in theirs for key in against):
            dropped.append(f"{label}：against 必须是这一局对手方上场的宠物")
        elif not isinstance(evidence, list) or not evidence or any(key not in ids for key in evidence):
            dropped.append(f"{label}：evidence 必须引用给出的事实编号")
        elif item["scope"] == "lineup" and lineup_conflict(facts, item["polarity"], pets):
            dropped.append(f"{label}：{lineup_conflict(facts, item['polarity'], pets)}")
        elif len(lessons) >= 3:
            dropped.append(f"{label}：超过 3 条，已忽略")
        else:
            lessons.append({
                "scope": item["scope"], "polarity": item["polarity"], "pets": sorted(set(pets)),
                "against": sorted(set(against)), "text": text_.strip()[:80], "evidence": list(evidence),
            })
    return {"summary": summary.strip()[:160], "moments": moments, "tips": tips, "lessons": lessons}, dropped


class ReviewAdvisor:
    """复盘讲解的 LLM 部分。LLMError / AdviceFailure 由调用方处理。"""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def report(self, battle_id: str, facts: dict[str, Any], feedback: str | None = None) -> dict[str, Any]:
        reply = self.llm.complete(SYSTEM_PROMPT, build_report_message(facts, feedback), hint_for(facts, battle_id))
        report, dropped = read_report(reply.text, reply.finish_reason, facts)
        report["trace"] = {"llm_ms": reply.ms, "usage": reply.usage, "dropped": dropped}
        return report
