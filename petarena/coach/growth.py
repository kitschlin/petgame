"""PVE 培养建议：练哪只宠物的哪项属性、练到多少为止，以及哪些别练。

有两种做法（COACH_GROWTH）：

- light（默认）：不做模拟，只按台阶给方向（suggest）。属性的收益是台阶式的——差一点就少一次击倒、
  慢一点就后出手，过了台阶再加也没用——所以建议的都是「刚好跨过某个台阶」的项目：主攻手的攻击
  （击倒需要的次数减少）、速度（出手顺序超过对面某只）、生命（能多扛一下）。这些都是可以直接核对的
  算术，立刻就有；它不保证胜率提高多少，卡片上也不写胜率。
- full：把每种培养方案真的打一遍，给出带胜算的路线（plan）。更准，但每一步要几十秒的后台计算，
  而且样本有限时经常回答「没有找到」。下面是它的做法。

属性的收益是台阶式的：差一点就少一次击倒、慢一点就后出手，过了台阶再加也没用；
有些项目练了反而吃亏（例如生命变化让对手换了集火目标）。所以不能「哪项低补哪项」，
只能针对具体的对手阵容把每种培养方案真的打一遍：

1. 目标：玩家最近没赢的对手阵容，和我方要练的那套阵容。
2. 候选：阵容里三只宠物 × 各项属性 × 几档增量（速度另加「刚好反超对面某只」的档位），
   再加「全队同一项」「最好的两项组合」——有的对阵任何单项都没用，要几项一起才过台阶。
3. 粗筛（便宜）：按「双方常规打法」把每个候选各打若干局，与不培养时逐局配对比较
   （同一批种子）。它只用来挑出一份名单，不直接决定练什么。
4. 决选（贵，只在确实需要培养时做）：名单在玩家实际面对的口径下重打——我方照指导出招、
   对手按它真实的打法。名单要多样（同一只宠物的同一项属性只留一个），而且总是带上「全队
   攻击」这类朴素方案：实测对上会推演的智能对手，粗筛口径会高估冻结之类的效果，如果只信
   它，建议会输给「无脑堆攻击」。先全部打一小轮，留下前几名再加局数（逐轮淘汰）。
5. 重估：选中的一步用没用过的新种子再打一遍（从几个候选里挑最大值，挑中的多半是运气好的），
   展示和验收都用重估后的数字；没有变好就不推荐，路线到此为止。每一点都要好几次挑战才练
   得成，宁可少推荐。
6. 路线：按「每次 PVE 挑战的期望收益」贪心，一步之后在新属性上重新来，最多 3 步；达到
   目标胜率，或者再没有显著的增益就停。
7. 最后一关：整条路线练完之后，用和战斗指导完全一样的出招方式再量一次（与练之前同一批
   种子）。面板上的「现在 → 练完」就是这两个数；量出来没有变好，整条路线都不推荐。

不需要培养时（换阵容或练操作就够），只做粗筛，结果仅供参考。
LLM 只负责把引擎算出的路线讲明白（GrowthAdvisor），不另外推荐项目。
"""

from __future__ import annotations

import copy
import itertools
import math
import time
from pathlib import Path
from typing import Any, Sequence

from ..engine.pets import HIT_CAP, SPECIES, STAT_NAMES, TURTLE_PASSIVE_PCT, battle_speed
from ..pve import best_chance
from . import rollout
from .draft import AdviceFailure, OpponentProfile, parse_json
from .experience import GUIDED_GAMES
from .llm import LLM
from .matchup import Lineup, Roster
from .policy import EXPERT, HEURISTIC

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "growth_system.md").read_text(encoding="utf-8").strip()
LIGHT_PROMPT = (Path(__file__).resolve().parent / "prompts" / "growth_light_system.md").read_text(encoding="utf-8").strip()

LIGHT = "light"  # 只按台阶给方向，不做模拟
FULL = "full"  # 逐项模拟，给出带胜算的路线
GROWTH_MODES = (LIGHT, FULL)
FACTS = "facts"  # 建议的依据：台阶（算术），没有胜算
ATTACKER = 20  # 攻击不低于这个数的宠物才建议练攻击：辅助宠物只有 9～11，加几点也打不动对手
SUGGESTIONS = 3  # 最多给几条建议

DUNGEON = {"atk": "atk", "max_hp": "hp", "speed": "speed", "hit": "hit"}
GRID = (1, 2, 3, 4, 6, 8)  # 单项培养的增量档位
TEAM_GRID = (2, 3, 5)  # 全队同一项的增量档位
SPEED_REACH = 12  # 速度最多考虑追多少点
CHANCE = best_chance()  # 期望挑战次数按玩家能用的模式里最高的通关概率算（本版只有手动模式 30%）
MAX_STEPS = 3
MIN_GAIN = 0.03  # 增益至少这么多（并且超过两倍标准误）才值得练
HARM = 0.05  # 胜算下降这么多（并且超过两倍标准误）列入「别练」
SHORTLIST = 6  # 只做粗筛时，重估多少个候选
FINALISTS = 8  # 决选名单的大小
KEEP = 4  # 决选第一轮之后留下几个
TRIED = 3  # 路线停下时，把那一步里看起来最好的几个候选也交给界面：玩家看得到「这些都试过了」
INVALID = "invalid"

Item = tuple[str, str, int]  # (宠物, 属性, 增量)
Upgrade = tuple[Item, ...]


# ---------------------------------------------------------------- 培养方案的表示

def normalize(items: Sequence[Item]) -> Upgrade:
    merged: dict[tuple[str, str], int] = {}
    for pet, stat, delta in items:
        merged[(pet, stat)] = merged.get((pet, stat), 0) + delta
    return tuple(sorted((pet, stat, delta) for (pet, stat), delta in merged.items() if delta))


def apply(roster: Roster, upgrade: Upgrade) -> Roster:
    result = copy.deepcopy(roster)
    for pet, stat, delta in upgrade:
        value = result["me"][pet][stat] + delta
        result["me"][pet][stat] = min(HIT_CAP, value) if stat == "hit" else value
    return result


def points(upgrade: Upgrade) -> int:
    return sum(delta for _, _, delta in upgrade)


def attempts(upgrade: Upgrade) -> float:
    """期望要挑战多少次 PVE 才能练成。"""
    return points(upgrade) / CHANCE


def label(upgrade: Upgrade) -> str:
    return "、".join(f"{SPECIES[pet].name}{STAT_NAMES[stat]} +{delta}" for pet, stat, delta in upgrade)


def candidates(roster: Roster, lineup: Lineup, targets: Sequence[Lineup]) -> list[Upgrade]:
    singles: list[Upgrade] = []
    for pet in lineup:
        stats = roster["me"][pet]
        for stat in ("atk", "max_hp", "speed") + (("hit",) if pet == "snake" else ()):
            grid = set(GRID)
            if stat == "speed":  # 刚好反超对面某只宠物的档位
                mine = battle_speed(pet, stats["speed"])
                for opp in targets:
                    for foe in opp:
                        need = battle_speed(foe, roster["opp"][foe]["speed"]) - mine + 1
                        if 0 < need <= SPEED_REACH:
                            grid.add(need)
            if stat == "hit":
                grid = {delta for delta in (5, 10, 15, 25) if stats["hit"] + delta <= HIT_CAP}
            singles += [((pet, stat, delta),) for delta in sorted(grid)]
    team = [normalize([(pet, stat, delta) for pet in lineup]) for stat in ("atk", "max_hp") for delta in TEAM_GRID]
    team += [normalize([(pet, stat, delta) for pet in lineup for stat in ("atk", "max_hp")]) for delta in (3, 5)]
    return singles + team


# ---------------------------------------------------------------- 模拟

def outcomes(job: tuple) -> list[float]:
    """进程池任务：一种属性配置对上一套目标阵容打若干局（局号从 start 开始），返回每一局的结果。
    种子只取决于阵容和局号，所以不同配置之间可以逐局配对比较。"""
    roster, my, opp, start, games, me, foe, tag = job
    result = []
    for game in range(start, start + games):
        winner = rollout.play(roster, my, opp, rollout.game_seed(tag, my, opp, game), me, foe)
        result.append(1.0 if winner == "me" else 0.5 if winner == "draw" else 0.0)
    return result


def _paired(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    mean = sum(diffs) / n
    if n < 2:
        return mean, 1.0
    return mean, (sum((d - mean) ** 2 for d in diffs) / (n - 1) / n) ** 0.5


def _summary(result: Sequence[float], base: Sequence[float]) -> dict[str, float]:
    gain, se = _paired(result, base)
    return {"gain": gain, "se": se, "value": sum(result) / len(result)}


def run_configs(
    roster: Roster, lineup: Lineup, targets: Sequence[Lineup], upgrades: Sequence[Upgrade], *, games: int,
    me: Any = HEURISTIC, foe: Any = HEURISTIC, tag: str = "growth", workers: int = 0,
) -> list[list[float]]:
    """不培养 + 每个培养方案，各自对上全部目标阵容打 games 局。返回逐局结果，第一项是不培养。

    常规打法的对局很便宜（每局不到 1 毫秒），一个配置对一套阵容算一个任务；带推演的对局
    每局要一秒上下，两局一个任务，这样配置很少时也能把进程池用满。
    """
    lineup = tuple(lineup)
    cheap = isinstance(me, str) and isinstance(foe, str) and foe != EXPERT
    size = games if cheap else 2
    rosters = [roster] + [apply(roster, upgrade) for upgrade in upgrades]
    jobs = [(config, lineup, tuple(opp), start, min(size, games - start), me, foe, tag)
            for config in rosters for opp in targets for start in range(0, games, size)]
    pieces = iter(rollout.pool_map(outcomes, jobs, workers))
    per_config = len(targets) * len(range(0, games, size))
    return [[value for _ in range(per_config) for value in next(pieces)] for _ in rosters]


def screen(
    roster: Roster, lineup: Lineup, targets: Sequence[Lineup], upgrades: Sequence[Upgrade], *, games: int,
    me: Any = HEURISTIC, foe: Any = HEURISTIC, tag: str = "growth", workers: int = 0,
) -> tuple[float, dict[Upgrade, dict[str, float]]]:
    """每个培养方案相对于不培养的增益。返回（不培养时的价值，{方案: {gain, se, value}}）。"""
    results = run_configs(roster, lineup, targets, upgrades, games=games, me=me, foe=foe, tag=tag, workers=workers)
    base = results[0]
    return sum(base) / len(base), {upgrade: _summary(result, base) for upgrade, result in zip(upgrades, results[1:])}


def _significant(entry: dict[str, float]) -> bool:
    return entry["gain"] >= max(MIN_GAIN, 2 * entry["se"])


def _harmful(entry: dict[str, float]) -> bool:
    return entry["gain"] <= -max(HARM, 2 * entry["se"])


# ---------------------------------------------------------------- 台阶事实

def _hits(health: float, damage: float) -> int:
    return math.ceil(health / damage) if damage > 0 else 99


def breakpoints(roster: Roster, upgrade: Upgrade, lineup: Lineup, target: Lineup, limit: int = 4) -> list[str]:
    """这次培养跨过了哪些台阶：击倒需要的次数、出手顺序。都是可以直接核对的算术。"""
    shield = TURTLE_PASSIVE_PCT / 100 if "turtle" in target else 0.0
    my_shield = TURTLE_PASSIVE_PCT / 100 if "turtle" in lineup else 0.0
    facts: list[str] = []
    for pet, stat, delta in upgrade:
        before = roster["me"][pet][stat]
        after = before + delta
        head = f"{SPECIES[pet].name}{STAT_NAMES[stat]} {before}→{after}"
        if stat == "hit":
            facts.append(f"{head}%：寒毒结界每次冻结对手的概率提高 {delta} 个百分点")
            continue
        grouped: dict[str, list[str]] = {}  # 同一种变化涉及的对手宠物合并成一句
        for foe in target:
            theirs = roster["opp"][foe]
            foe_name = SPECIES[foe].name
            if stat == "atk":
                health = theirs["max_hp"] * (1 + shield)
                kinds = [("普攻", 1.0)]
                if pet == "cat":
                    kinds = [("猛爪突袭加一次普攻", 3.0), ("猛爪突袭", 2.0), ("普攻", 1.0)]
                if pet == "dog":
                    kinds = [("汪汪冲击", 0.8), ("普攻", 1.0)]
                for kind, ratio in kinds:
                    a, b = _hits(health, before * ratio), _hits(health, after * ratio)
                    if b < a:
                        text = (f"一套{kind}就能击倒" if b == 1 and ratio == 3.0 else f"用{kind}击倒需要的次数从 {a} 次减到 {b} 次")
                        grouped.setdefault(text, []).append(foe_name)
            elif stat == "max_hp":
                kinds = [("猛爪突袭", 2.0), ("普攻", 1.0)] if foe == "cat" else [("普攻", 1.0)]
                for kind, ratio in kinds:
                    a = _hits(before * (1 + my_shield), theirs["atk"] * ratio)
                    b = _hits(after * (1 + my_shield), theirs["atk"] * ratio)
                    if b > a:
                        grouped.setdefault(f"能多扛一次{kind}（要 {b} 次才倒，原来 {a} 次）", []).append(foe_name)
            elif stat == "speed":
                speed = battle_speed(foe, theirs["speed"])
                if battle_speed(pet, before) <= speed < battle_speed(pet, after):
                    grouped.setdefault("出手顺序超过", []).append(f"{foe_name}（速度 {speed}）")
                elif battle_speed(pet, before) < speed == battle_speed(pet, after):
                    grouped.setdefault("出手顺序追平", []).append(f"{foe_name}（速度 {speed}，同速时随机决定先后）")
        for text, foes in grouped.items():
            who = "对手" + "、".join(foes)
            if text.startswith("出手顺序"):
                facts.append(f"{head}：{text}{who}")
            elif text.startswith("能多扛"):
                facts.append(f"{head}：面对{who}{text}")
            else:
                facts.append(f"{head}：打{who}，{text}")
    return facts[:limit]


# ---------------------------------------------------------------- 不做模拟的建议

def _crossed(roster: Roster, pet: str, stat: str, delta: int, lineup: Lineup, targets: Sequence[Lineup]) -> float:
    """这次培养跨过了几个台阶（对上这些对手阵容里的每一只）。与 breakpoints() 数的是同一批台阶。"""
    stats = roster["me"][pet]
    before, after = stats[stat], stats[stat] + delta
    count = 0.0
    seen: set[tuple] = set()
    for target in targets:
        shield = TURTLE_PASSIVE_PCT / 100 if "turtle" in target else 0.0
        my_shield = TURTLE_PASSIVE_PCT / 100 if "turtle" in lineup else 0.0
        for foe in target:
            theirs = roster["opp"][foe]
            if stat == "atk":
                ratios = {"cat": (3.0, 2.0, 1.0), "dog": (0.8, 1.0)}.get(pet, (1.0,))
                health = theirs["max_hp"] * (1 + shield)
                for ratio in ratios:
                    key = (foe, shield, ratio)
                    if key not in seen and _hits(health, after * ratio) < _hits(health, before * ratio):
                        seen.add(key)
                        count += 1
            elif stat == "max_hp":
                for ratio in ((2.0, 1.0) if foe == "cat" else (1.0,)):
                    key = (foe, ratio)
                    if key not in seen and (_hits(after * (1 + my_shield), theirs["atk"] * ratio)
                                            > _hits(before * (1 + my_shield), theirs["atk"] * ratio)):
                        seen.add(key)
                        count += 1
            elif stat == "speed" and foe not in seen:
                speed = battle_speed(foe, theirs["speed"])
                if battle_speed(pet, before) <= speed < battle_speed(pet, after):
                    seen.add(foe)
                    count += 1
    return count


def suggest(*, roster: Roster, lineup: Sequence[str], targets: Sequence[Sequence[str]], limit: int = SUGGESTIONS) -> dict[str, Any]:
    """不做模拟的培养建议：每只宠物的每项属性，找「每点跨过的台阶最多」的那一档，再按经验排个先后——

    先是主攻手的攻击，其次是速度（出手顺序），最后是生命。这个先后来自逐项模拟那套做法（plan）在评测里
    实际选出来的项目：几乎都是小狗、小猫的攻击和「刚好反超对面某只」的速度。
    返回的结构和 plan() 一样（卡片共用），只是没有胜算：basis = "facts"，各步的 gain 是 None。
    """
    started = time.monotonic()
    lineup = tuple(lineup)
    targets = [tuple(opp) for opp in targets]
    best: dict[str, list[tuple[float, Upgrade]]] = {"atk": [], "speed": [], "max_hp": []}
    for pet in lineup:
        for stat in best:
            if stat == "atk" and roster["me"][pet]["atk"] < ATTACKER:
                continue
            reach = SPEED_REACH if stat == "speed" else max(GRID)
            options = [(_crossed(roster, pet, stat, delta, lineup, targets) / delta, -delta, delta) for delta in range(1, reach + 1)]
            ratio, _, delta = max(options)
            if ratio > 0:
                best[stat].append((ratio, ((pet, stat, delta),)))
    for items in best.values():
        items.sort(key=lambda item: (-item[0], item[1]))
    # 攻击、速度、攻击、生命……轮着取：一条路线上别全是同一类
    order = [("atk", 0), ("speed", 0), ("atk", 1), ("max_hp", 0), ("speed", 1), ("atk", 2), ("max_hp", 1)]
    chosen = [best[stat][index][1] for stat, index in order if index < len(best[stat])][:limit]
    steps = []
    for number, upgrade in enumerate(chosen, start=1):
        facts: list[str] = []
        for target in targets:
            facts += [fact for fact in breakpoints(roster, upgrade, lineup, target) if fact not in facts]
        steps.append({
            "id": f"S{number}",
            "items": [{"pet": pet, "stat": stat, "delta": delta, "dungeon": DUNGEON[stat],
                       "from": roster["me"][pet][stat], "to": roster["me"][pet][stat] + delta}
                      for pet, stat, delta in upgrade],
            "text": label(upgrade), "points": points(upgrade), "attempts": round(attempts(upgrade), 1),
            "gain": None, "se": None, "facts": facts[:3],
        })
    weak = [pet for pet in lineup if roster["me"][pet]["atk"] < ATTACKER]
    notes = ["这几项建议都练成，练到写的数值就停：属性的效果是台阶式的，只练其中一项不一定见效，再往上加也要到下一个台阶才有变化。"]
    if weak:
        notes.append(f"别把挑战次数花在{'、'.join(SPECIES[pet].name for pet in weak)}的攻击上：只有 "
                     f"{'、'.join(str(roster['me'][pet]['atk']) for pet in weak)}，加几点也打不动对手。")
    total = sum(step["points"] for step in steps)
    return {
        "lineup": list(lineup), "targets": [list(opp) for opp in targets], "goal": None,
        "basis": FACTS, "now": None, "after": None, "reached": False,
        "steps": steps, "avoid": [], "tried": [], "notes": notes,
        "points": total, "attempts": round(total / CHANCE, 1), "sample": 0,
        # 实测（eval_ladder.py 的 D 部分）：整组练成之后胜率都提高了，但中途不一定——有一组对阵练成第一项时
        # 胜率反而低了一截，练完三项才升上去。所以卡片上写「建议都练成」，不说每一项单独都有用。
        "label": "只按台阶算出（击倒需要的次数、出手顺序、能不能多扛一下），没有逐项模拟胜率",
        "games": 0, "ms": int((time.monotonic() - started) * 1000),
    }


def suggest_headline(verdict: str, data: dict[str, Any] | None) -> str:
    """不做模拟时的结论（一句话）。"""
    if verdict == "none":
        return "最近没有输过，现在不需要培养"
    names = "、".join(SPECIES[key].name for key in data["lineup"])
    lead = {
        "train": "建议培养",
        "lineup": f"先换成{names}（下一局军师会这样推荐）。想再稳一些，可以练",
        "play": "先照着战斗指导把操作打好。想再稳一些，可以练",
    }.get(verdict, "这一局主要是运气和对手发挥，照这样再打赢面不小。想再稳一些，可以练")
    if not data["steps"]:
        return {"train": f"{names}这套阵容上没有找到明显的培养台阶：先多打几局，让军师积累经验"}.get(
            verdict, lead.split("。")[0] if "。" in lead else "现在不需要培养")
    items = "、".join(step["text"] for step in data["steps"])
    return f"{lead}：{items}（都练成共 {data['points']} 点，期望约 {round(data['attempts'])} 次挑战）"


# ---------------------------------------------------------------- 方案

def finalists(roster: Roster, lineup: Lineup, ranked: Sequence[Upgrade]) -> list[Upgrade]:
    """决选名单：粗筛靠前的（同一只宠物的同一项属性只留最划算的一个），加上几个朴素方案。"""
    chosen: list[Upgrade] = []
    kinds: set[tuple] = set()
    for upgrade in ranked:
        kind = tuple((pet, stat) for pet, stat, _ in upgrade)
        if kind not in kinds:
            kinds.add(kind)
            chosen.append(upgrade)
        if len(chosen) >= FINALISTS - 3:
            break
    ace = max(lineup, key=lambda pet: roster["me"][pet]["atk"])  # 主攻手
    naive = [normalize([(pet, "atk", 3) for pet in lineup]), normalize([(pet, "atk", 5) for pet in lineup]),
             ((ace, "atk", 8),)]
    return list(dict.fromkeys(chosen + naive))[:FINALISTS]


def _real_model(profile: OpponentProfile) -> Any:
    """整局模拟里「照指导出招」时对对手的假设：用便宜的那一档（不逐步推演对手的应对）。"""
    model = profile.model
    return model[2] if not isinstance(model, str) and model[0] == rollout.REPLY else model


def _tried_item(upgrade: Upgrade, entry: dict[str, float]) -> dict[str, Any]:
    """一个试过、但增益没过关的候选。gain / se 是它相对不培养的增益和标准误（逐局配对）。"""
    return {"text": label(upgrade), "points": points(upgrade), "attempts": round(attempts(upgrade), 1),
            "gain": round(entry["gain"], 3), "se": round(entry["se"], 3)}


def tried_text(item: dict[str, Any]) -> str:
    gain = round(item["gain"] * 100)
    effect = f"约 +{gain}%，误差 ±{round(2 * item['se'] * 100)}%" if gain > 0 else "没有增益"
    return f"{item['text']}（{item['points']} 点）：{effect}"


def _avoid_item(upgrade: Upgrade, loss: float) -> dict[str, Any]:
    pet, stat, delta = upgrade[0]
    return {"pet": pet, "stat": stat, "delta": delta, "loss": round(loss, 3),
            "text": f"{SPECIES[pet].name}{STAT_NAMES[stat]} +{delta}：胜算反而下降约 {round(loss * 100)}%"}


GUIDED = "guided"  # 胜算的口径：我方照指导出招、对手按它真实的打法
PLAIN = "plain"  # 胜算的口径：双方按常规打法
BASIS_NAMES = {GUIDED: "照战斗指导出招、对手按它真实打法的整局模拟", PLAIN: "双方按常规打法的整局模拟"}


def plan(
    *, roster: Roster, lineup: Sequence[str], targets: Sequence[Sequence[str]], profile: OpponentProfile,
    games: int = 120, verify: int = 48, goal: float = 0.6, workers: int = 0, salt: str = "",
    max_steps: int = MAX_STEPS,
) -> dict[str, Any]:
    """针对这些对手阵容，为这套我方阵容找一条最划算的培养路线。

    games：粗筛时每个候选对每套目标阵容打几局（常规打法，便宜）。
    verify：决选时每个候选一共打几局（平摊到各套目标阵容上；照指导出招、对手按真实打法，贵）。
            目标阵容有几套，花的算力和结论的把握都一样。0 表示只做粗筛，胜算都是常规打法的口径。
    """
    started = time.monotonic()
    lineup = tuple(lineup)
    targets = [tuple(opp) for opp in targets]
    decided = verify > 0
    foe = profile.opp_policy  # 粗筛口径：我方常规打法，对手按对阵表里的打法
    real = {"me": rollout.guided(_real_model(profile), GUIDED_GAMES), "foe": profile.foe}  # 决选口径
    each = max(2, -(-verify // len(targets)))  # 决选时每个候选对每套目标阵容打几局
    first, second = max(2, each // 3), max(2, each - max(2, each // 3))  # 分两轮：先少打几局筛掉一半
    current = copy.deepcopy(roster)
    steps: list[dict[str, Any]] = []
    done: Upgrade = ()
    avoid: list[dict[str, Any]] = []
    tried: list[dict[str, Any]] = []  # 路线因为「再没有显著的增益」停下时，那一步里看起来最好的几个候选
    simulated = 0

    def near(upgrades: Sequence[Upgrade], scores: dict[Upgrade, dict[str, float]]) -> list[dict[str, Any]]:
        best = sorted(upgrades, key=lambda u: -scores[u]["gain"])[:TRIED]
        return [_tried_item(u, scores[u]) for u in best]

    def play(upgrades: Sequence[Upgrade], count: int, tag: str, **how: Any) -> list[list[float]]:
        nonlocal simulated
        simulated += (len(upgrades) + 1) * count * len(targets)
        return run_configs(current, lineup, targets, upgrades, games=count, tag=tag, workers=workers, **how)

    def measure() -> list[float]:
        """当前属性下的胜算，用和战斗指导完全一样的出招方式来打（比决选时更贵，只在开头和结尾各做一次）。"""
        how = {"me": rollout.guided(profile.model, GUIDED_GAMES), "foe": profile.foe}
        return play([], 2 * (first + second), f"growth|{salt}|measure", **how)[0]

    before = measure() if decided else None  # 逐局结果：最后一关要和练完之后逐局配对
    now = sum(before) / len(before) if decided else None  # 不培养时的胜算；只做粗筛时等第一轮重估后再填
    running = now  # 沿着路线走到当前这一步的胜算估计：决定要不要再往下练
    by_ratio = lambda scores: (lambda u: -scores[u]["gain"] / attempts(u))  # noqa: E731

    for number in range(1, max_steps + 1):
        pool = candidates(current, lineup, targets)
        tag = f"growth|{salt}|{number}"
        results = play(pool, games, tag, foe=foe)
        table = {upgrade: _summary(result, results[0]) for upgrade, result in zip(pool, results[1:])}
        ranked = sorted((u for u in pool if _significant(table[u])), key=by_ratio(table))
        if number == 1:  # 最好的几个单项两两组合：有的台阶要两项一起才跨得过
            singles = [u for u in ranked if len(u) == 1][:4]
            pairs = [normalize(a + b) for a, b in itertools.combinations(singles, 2) if a[0][:2] != b[0][:2]]
            if pairs:
                extra = play(pairs, games, tag, foe=foe)
                table.update({upgrade: _summary(result, results[0]) for upgrade, result in zip(pairs, extra[1:])})
                pool = pool + pairs
                ranked = sorted((u for u in pool if _significant(table[u])), key=by_ratio(table))
        harmful = sorted((u for u in pool if len(u) == 1 and _harmful(table[u])), key=lambda u: table[u]["gain"])
        seen: set[tuple[str, str]] = set()
        harmful = [u for u in harmful if u[0][:2] not in seen and not seen.add(u[0][:2])]  # 每只宠物的每项属性只留一个

        if not decided:
            # ---- 只做粗筛：靠前的候选用新种子重估，按重估后的数字选
            short = ranked[:SHORTLIST]
            check = list(dict.fromkeys(short + (harmful[:4] if number == 1 else [])))
            if not check:
                tried = near([u for u in pool if table[u]["gain"] > 0], table)
                break
            again = play(check, games, tag + "|fresh", foe=foe)
            fresh = {upgrade: _summary(result, again[0]) for upgrade, result in zip(check, again[1:])}
            if number == 1:
                now = running = sum(again[0]) / len(again[0])
                avoid = [_avoid_item(u, -fresh[u]["gain"]) for u in harmful[:4] if _harmful(fresh[u])]
            winners = sorted((u for u in short if _significant(fresh[u])), key=by_ratio(fresh))
            if not winners:
                tried = near(short, fresh)
                break
            best, proof = winners[0], fresh[winners[0]]
        else:
            # ---- 决选：名单在玩家实际面对的口径下重打，逐轮淘汰，选中的再用新种子重估
            short = finalists(current, lineup, ranked)
            extra = harmful[:2] if number == 1 else []
            everyone = list(dict.fromkeys(short + extra))
            round1 = play(everyone, first, tag + "|real1", **real)
            scores = {upgrade: _summary(result, round1[0]) for upgrade, result in zip(everyone, round1[1:])}
            # 留下谁：最划算的两个，加上增益最大的两个。只按「每次挑战的收益」留的话，第一轮才十几局，便宜的
            # 小项目碰巧多赢一局，比值就压过真正有效的大项目——而它们过不了后面的显著性检验
            thrifty = sorted(short, key=by_ratio(scores))
            biggest = sorted(short, key=lambda u: -scores[u]["gain"])
            keep = list(dict.fromkeys(thrifty[:KEEP - 2] + biggest[:2] + thrifty))[:KEEP]
            again = list(dict.fromkeys(keep + extra))
            round2 = play(again, second, tag + "|real2", **real)
            baseline = round1[0] + round2[0]
            scores = {upgrade: _summary(round1[1 + everyone.index(upgrade)] + result, baseline)
                      for upgrade, result in zip(again, round2[1:])}
            if number == 1:
                avoid = [_avoid_item(u, -scores[u]["gain"]) for u in extra if _harmful(scores[u])]
            winners = sorted((u for u in keep if _significant(scores[u])), key=by_ratio(scores))
            if not winners:
                tried = near(keep, scores)
                break
            # 从最划算的开始，用新种子重估：增益还在就选它；换一批种子就不见了的，换下一个显著的候选
            # （先只重估第一个，没过再把其余的一起重估，多数时候只花一份算力）。都没过，路线才到此为止。
            best, failed = None, []
            for group in (winners[:1], winners[1:]):
                if not group:
                    continue
                check = play(group, first + second, tag + "|real-fresh", **real)
                for upgrade, result in zip(group, check[1:]):
                    proof = _summary(result, check[0])
                    if proof["gain"] >= MIN_GAIN:
                        best = upgrade
                        break
                    failed.append(_tried_item(upgrade, proof))
                if best is not None:
                    break
            if best is None:
                tried = (failed + near([u for u in keep if u not in winners], scores))[:TRIED]
                break

        steps.append({
            "id": f"S{number}",
            "items": [{"pet": pet, "stat": stat, "delta": delta, "dungeon": DUNGEON[stat],
                       "from": current["me"][pet][stat], "to": current["me"][pet][stat] + delta}
                      for pet, stat, delta in best],
            "text": label(best), "points": points(best), "attempts": round(attempts(best), 1),
            "gain": round(proof["gain"], 3), "se": round(proof["se"], 3),  # 这一步的增益（新种子重估，逐局配对）
            "facts": breakpoints(current, best, lineup, targets[0]),
        })
        done = normalize(done + best)
        current = apply(current, best)
        running = min(1.0, running + proof["gain"])
        if running >= goal:
            break

    if now is None:  # 只做粗筛，而且第一轮就没有候选
        now = running = sum(results[0]) / len(results[0])
    after = running
    if decided:
        # 最后一关：整条路线练完之后，用和战斗指导完全一样的出招方式再量一次（与练之前同一批种子）。
        # 面板上的「现在 → 练完」就是这两个数；各步的增益按决选时的估计把这个总增益分摊开，这样加起来对得上。
        final = measure() if steps else before
        after = sum(final) / len(final)
        if steps and after - now < MIN_GAIN:  # 量出来没有变好：不推荐，并且如实列出这条路线量出来的结果
            tried = [{"text": " → ".join(step["text"] for step in steps), "points": points(done),
                      "attempts": round(attempts(done), 1), "gain": round(after - now, 3),
                      "se": round(_paired(final, before)[1], 3)}]
            steps, done, after = [], (), now
        elif steps:
            scale = (after - now) / sum(step["gain"] for step in steps)
            for step in steps:
                step["gain"], step["se"] = round(step["gain"] * scale, 3), round(step["se"] * scale, 3)
    total = points(done)
    return {
        "lineup": list(lineup), "targets": [list(opp) for opp in targets], "goal": goal,
        "basis": GUIDED if decided else PLAIN,  # now / after / 各步增益的口径
        "now": round(now, 3), "after": round(after, 3), "reached": bool(steps) and after >= goal,
        "steps": steps, "avoid": avoid[:3],
        # 路线为什么到此为止：已经到目标（reached）；或者接着模拟过的候选增益都不显著（tried 里是其中看起来最好的几个）；
        # 都不是，就是一条路线的步数用完了
        "tried": tried,
        "points": total, "attempts": round(total / CHANCE, 1),
        "sample": 2 * (first + second) * len(targets) if decided else games * len(targets),  # now / after 各基于多少局
        "label": (f"先按常规打法粗筛（每个候选 {games * len(targets)} 局），再把名单放到实际的口径下决选，选中的一步用新种子重估"
                  if decided else f"每个候选 {games * len(targets)} 局，入选的用新种子重估"),
        "games": simulated, "ms": int((time.monotonic() - started) * 1000),
    }


def headline(verdict: str, data: dict[str, Any] | None, needed: bool = True) -> str:
    """引擎的结论（一句话）。verdict 是复盘的结论（train / lineup / play / luck / none）；
    needed 表示「最好的阵容、照指导出招也到不了目标」，也就是确实需要培养。"""
    if verdict == "none":
        return "最近没有输过，现在不需要培养"
    if data is not None and data.get("basis") == FACTS:
        return suggest_headline(verdict, data)
    names = "、".join(SPECIES[key].name for key in data["lineup"]) if data else ""
    switch = f"先换成{names}（下面按这套阵容安排）；光换阵容还到不了目标，" if verdict == "lineup" and needed else ""
    if data is None or not data["steps"]:
        if needed:  # 如实说：没有找到，而不是随便推荐一项
            tried = f"单项 {max(GRID)} 点以内、全队每项 {max(TEAM_GRID)} 点以内的方案都模拟过，增益都不明显"
            if verdict == "lineup":
                return f"先换成{names}。光换阵容还到不了目标，但这套阵容上没有找到立竿见影的培养项目（{tried}）：先用新阵容打几局，再回来看"
            return f"没有找到立竿见影的培养项目（{tried}）：先多打几局让军师积累经验，它可能找到更合适的阵容"
        return {"lineup": "问题在阵容，先换阵容，不用急着培养", "play": "问题在操作，先照着战斗指导把操作打好，不用急着培养"}.get(
            verdict, "这一局主要是运气和对手发挥，现在不需要培养")
    route = (f"{' → '.join(step['text'] for step in data['steps'])}（共 {data['points']} 点，期望约 {round(data['attempts'])} 次挑战），"
             f"胜算约 {round(data['now'] * 100)}% → {round(data['after'] * 100)}%")
    if needed:
        if not data["reached"]:
            route += f"；还到不了 {round(data['goal'] * 100)}% 的目标，练完再打一局，按新的结果接着安排"
        return f"{switch}建议培养：{route}"
    lead = {"lineup": "先换阵容更划算", "play": "先把操作打好更划算"}.get(verdict, "暂时不用培养")
    return f"{lead}；如果仍想培养，最划算的是：{route}"


# ---------------------------------------------------------------- LLM 讲解

def build_light_message(data: dict[str, Any], verdict: str, feedback: str | None = None) -> str:
    """不做模拟时给 LLM 的上下文：只有台阶事实，没有胜算。"""
    names = lambda keys: "、".join(SPECIES[key].name for key in keys)  # noqa: E731
    lines = [
        "【目标】",
        f"我方阵容（按站位）：{names(data['lineup'])}。要对付的对手阵容：" + "；".join(names(opp) for opp in data["targets"]) + "。",
        f"引擎的结论：{suggest_headline(verdict, data)}",
        "",
        "【培养建议】（按建议的先后排列，建议都练成；引擎只核对了台阶，没有模拟胜率）",
    ]
    for step in data["steps"]:
        facts = "；".join(step["facts"]) or "（没有对应到具体的台阶）"
        lines.append(f"{step['id']}：{step['text']}（期望约 {round(step['attempts'])} 次挑战）｜跨过的台阶：{facts}")
    if not data["steps"]:
        lines.append("（没有找到明显的台阶）")
    lines += ["", "【提醒】"] + [f"- {note}" for note in data.get("notes") or []]
    if feedback:
        lines += ["", "【上次输出的问题】", feedback]
    return "\n".join(lines)


def build_user_message(data: dict[str, Any], verdict: str, feedback: str | None = None, needed: bool = True) -> str:
    if data.get("basis") == FACTS:
        return build_light_message(data, verdict, feedback)
    names = lambda keys: "、".join(SPECIES[key].name for key in keys)  # noqa: E731
    lines = [
        "【目标】",
        f"我方阵容（按站位）：{names(data['lineup'])}。要对付的对手阵容：" + "；".join(names(opp) for opp in data["targets"]) + "。",
        f"目标胜算：{round(data['goal'] * 100)}%。现在约 {round(data['now'] * 100)}%，整条路线练完约 {round(data['after'] * 100)}%"
        f"（口径：{BASIS_NAMES[data['basis']]}）。",
        f"引擎的结论：{headline(verdict, data, needed)}",
        "",
        "【培养路线】（引擎逐项模拟后选出的，按先后顺序）",
    ]
    for step in data["steps"]:
        facts = "；".join(step["facts"]) or "引擎模拟里胜算有提高，但没有对应到某一个单独的台阶（多半来自和队友伤害的配合）"
        lines.append(
            f"{step['id']}：{step['text']}（期望约 {round(step['attempts'])} 次挑战）｜这一步让胜算提高约 "
            f"{round(step['gain'] * 100)} 个百分点｜事实：{facts}")
    if not data["steps"]:
        lines.append("（没有找到值得练的项目）")
    lines += ["", "【别练】（引擎模拟后发现会让胜算下降的项目）"]
    lines += [f"- {item['text']}" for item in data["avoid"]] or ["（没有）"]
    if data["reached"]:
        lines.append("【到此为止】练完这条路线就到目标了，不用再往上练。")
    elif data.get("tried"):
        lines.append("【到此为止】接着往下模拟过的方案里，看起来最好的几个增益也在误差范围内，不值得为它们花挑战次数："
                     + "；".join(tried_text(item) for item in data["tried"]) + "。")
    else:
        lines.append("【到此为止】一条路线最多安排这么几步：练完再打一局，按新的结果接着安排。")
    if feedback:
        lines += ["", "【上次输出的问题】", feedback]
    return "\n".join(lines)


def hint_for(data: dict[str, Any], verdict: str, key: str, needed: bool = True) -> dict[str, Any]:
    return {"kind": "growth", "token": f"growth:{key}", "headline": headline(verdict, data, needed),
            "steps": [{"id": step["id"], "text": step["text"]} for step in data["steps"]], "avoid": data["avoid"]}


def read_advice(text: str, finish_reason: str, data: dict[str, Any]) -> dict[str, Any]:
    ids = [step["id"] for step in data["steps"]]

    def fail(problem: str) -> AdviceFailure:
        return AdviceFailure(INVALID, "AI 输出不合格", feedback=(
            f"你上一次的输出存在以下问题：\n- {problem}\nsteps 里必须正好包含这些 id：{', '.join(ids) or '（空列表）'}。请重新输出完整 json。"))

    try:
        raw = parse_json(text)
    except ValueError:
        raise fail("输出不是完整的 json" + ("（被截断了，请写短一些）" if finish_reason == "length" else "")) from None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise fail("缺少 summary")
    why = {}
    for item in raw.get("steps") or []:
        if isinstance(item, dict) and item.get("id") in ids and isinstance(item.get("why"), str) and item["why"].strip():
            why[item["id"]] = item["why"].strip()[:100]
        else:
            raise fail(f"steps 里的 {item!r} 不是路线中的步骤，或者缺少 why")
    missing = [step_id for step_id in ids if step_id not in why]
    if missing:
        raise fail(f"缺少对 {', '.join(missing)} 的说明")
    text_of = lambda key: raw[key].strip()[:100] if isinstance(raw.get(key), str) else ""  # noqa: E731
    return {"summary": summary.strip()[:120], "steps": [{"id": step_id, "why": why[step_id]} for step_id in ids],
            "stop": text_of("stop"), "avoid": text_of("avoid") if data["avoid"] else ""}


class GrowthAdvisor:
    """培养建议的 LLM 部分。LLMError / AdviceFailure 由调用方处理。"""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def advise(self, data: dict[str, Any], verdict: str, key: str, feedback: str | None = None,
               needed: bool = True) -> dict[str, Any]:
        system = LIGHT_PROMPT if data.get("basis") == FACTS else SYSTEM_PROMPT
        reply = self.llm.complete(system, build_user_message(data, verdict, feedback, needed),
                                  hint_for(data, verdict, key, needed))
        advice = read_advice(reply.text, reply.finish_reason, data)
        advice["trace"] = {"llm_ms": reply.ms, "usage": reply.usage}
        return advice
