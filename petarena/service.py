"""应用层：管理对局相位、属性锁定、战斗记录与 PVE。

相位：idle（没有对局）→ draft（选将）→ battle（战斗）→ finished（已结束，等待玩家离开或再来一局）。
从进入选将到战斗结束，双方属性锁定为进入时的数值，期间不能进行 PVE。
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import ai, pve
from .coach import opponent as smart
from .engine import Battle, Draft, DraftError, IllegalAction
from .engine.draft import PICK_ORDER
from .engine.pets import (
    BASIC_DESC,
    BASIC_NAME,
    ENERGY_CAP,
    ENERGY_START,
    HIT_CAP,
    MAX_ROUNDS,
    SIDE_NAMES,
    SIDES,
    SPECIES,
    STAT_KEYS,
    STAT_NAMES,
    TEAM_SIZE,
)
from .store import Store

IDLE = "idle"
DRAFT = "draft"
BATTLE = "battle"
FINISHED = "finished"


class GameError(Exception):
    """请求不合法。"""

    status = 400


class Conflict(GameError):
    """当前相位下不允许的操作。"""

    status = 409


class NotFound(GameError):
    status = 404


CONFIG: dict[str, Any] = {
    "sides": [{"key": side, "name": SIDE_NAMES[side]} for side in SIDES],
    "species": [
        {
            "key": s.key, "name": s.name, "emoji": s.emoji, "role": s.role,
            "skill": {
                "name": s.skill_name, "cost": s.skill_cost,
                "target": s.skill_target, "desc": s.skill_desc,
            },
            "passive": {"name": s.passive_name, "desc": s.passive_desc},
        }
        for s in SPECIES.values()
    ],
    "basic": {"name": BASIC_NAME, "desc": BASIC_DESC},
    "stats": STAT_NAMES,
    "dungeons": [{"key": key, **info} for key, info in pve.DUNGEONS.items()],
    "modes": [{"key": key, **info} for key, info in pve.MODES.items()],
    "opponent_modes": [{"key": key, "name": name} for key, name in ai.MODES.items()],
    "rules": {
        "max_rounds": MAX_ROUNDS,
        "energy_cap": ENERGY_CAP,
        "energy_start": ENERGY_START,
        "team_size": TEAM_SIZE,
        "hit_cap": HIT_CAP,
        "pick_order": list(PICK_ORDER),
    },
}


class Match:
    """一场对局：选将 + 战斗。roster 是进入选将时锁定的双方属性。"""

    def __init__(
        self,
        match_id: str,
        created_at: str,
        roster: dict[str, dict[str, dict[str, int]]],
        picks: list[list[str]] | None = None,
        seed: int | None = None,
        opponent: str = "manual",
        ai_seed: int = 0,
    ) -> None:
        self.id = match_id
        self.created_at = created_at
        self.roster = roster
        self.draft = Draft(picks)
        self.seed = seed
        self.opponent = opponent  # 对手方操作模式："manual" 同屏手动 / "random" 电脑随机 / "smart" 电脑智能
        self.ai_seed = ai_seed
        self.battle: Battle | None = None
        # 第几次行动（序号）→ 当时 AI 建议了什么、玩家是否采纳。只用于日志标注，不参与结算和回放
        self.marks: dict[int, dict[str, Any]] = {}
        # 第几次行动（序号）→ 战斗指导当时对各合法动作的推演结果。留给复盘复用，不参与结算和回放
        self.evals: dict[int, dict[str, Any]] = {}

    @property
    def phase(self) -> str:
        if self.battle is None:
            return DRAFT
        return FINISHED if self.battle.finished else BATTLE

    def lineups(self) -> dict[str, list[dict[str, Any]]]:
        return {
            side: [
                {"species": key, **{stat: self.roster[side][key][stat] for stat in STAT_KEYS}}
                for key in self.draft.lineup(side)
            ]
            for side in SIDES
        }


class GameService:
    def __init__(
        self,
        store: Store,
        *,
        roll100: Callable[[], int] | None = None,
        new_seed: Callable[[], int] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self._roll100 = roll100 or (lambda: secrets.randbelow(100))
        self._new_seed = new_seed or (lambda: secrets.randbits(32))
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self._lock = threading.RLock()

        self.pve_records = store.load_pve()
        self._pve_by_id = {record["id"]: record for record in self.pve_records}
        self.growth = pve.zero_growth()
        for record in self.pve_records:
            pve.apply(self.growth, record)

        self.match: Match | None = None
        self._restore()

    # ------------------------------------------------------------------ 状态

    @property
    def phase(self) -> str:
        return IDLE if self.match is None else self.match.phase

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase": self.phase,
                "config": CONFIG,
                "roster": pve.roster(self.growth),
                "match": self._match_state(),
            }

    def _match_state(self) -> dict[str, Any] | None:
        match = self.match
        if match is None:
            return None
        data: dict[str, Any] = {
            "id": match.id,
            "phase": match.phase,
            "created_at": match.created_at,
            "opponent": match.opponent,
            "roster": match.roster,
            "draft": match.draft.to_dict(),
            "battle": None,
        }
        if match.battle is not None:
            data["battle"] = {
                "static": match.battle.static(),
                "view": match.battle.view(),
                "options": match.battle.options(),
                "events": match.battle.events,
                "token": self._coach_token(match) if match.phase == BATTLE else None,
                "marks": {str(index): mark for index, mark in match.marks.items()},
            }
        return data

    # ------------------------------------------------------------------ PVP

    def start_match(self, opponent: str = "manual") -> dict[str, Any]:
        with self._lock:
            if self.phase in (DRAFT, BATTLE):
                raise Conflict("已有对局正在进行，请先打完或放弃本局")
            if opponent not in ai.MODES:
                raise GameError(f"未知的对手模式：{opponent}")
            match_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            self.match = Match(
                match_id, self._clock(), pve.roster(self.growth),  # 属性锁定
                opponent=opponent, ai_seed=self._new_seed(),
            )
            self._run_ai(self.match)  # 随机模式下对手方先选
            self._save_match()
            return self.state()

    def pick(self, side: str, species: str) -> dict[str, Any]:
        with self._lock:
            match = self._require(DRAFT, "当前不在选将阶段")
            if self._ai_controls(match, side):
                raise GameError("对手方由电脑随机选择" if match.opponent == "random" else "对手方由电脑选择")
            try:
                match.draft.pick(side, species)
            except DraftError as error:
                raise GameError(str(error)) from None
            self._after_pick(match)
            self._run_ai(match)
            if match.phase == FINISHED:
                self._record_battle(match)
            self._save_match()
            return self.state()

    def action(self, skill: str, target: str | None, mark: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一次操作。mark 是 AI Coach 对这次操作的标注（建议了什么、是否采纳），只在它
        针对的正是当前这个决策点时才记下来；它不影响结算。"""
        with self._lock:
            match = self._require(BATTLE, "当前不在战斗中")
            battle = match.battle
            index = len(battle.actions)
            if mark is not None and mark.get("token") != self._coach_token(match):
                mark = None
            try:
                events = battle.act(skill, target)
            except IllegalAction as error:
                raise GameError(str(error)) from None
            if mark is not None:
                mark = {"advised": mark["advised"], "adopted": bool(mark["adopted"])}
                match.marks[index] = mark
            events += self._run_ai(match)  # 随机模式下，接着替对手方行动，直到又轮到我方
            if battle.finished:
                self._record_battle(match)
            self._save_match()
            return {
                "phase": match.phase,
                "events": events,
                "view": battle.view(),
                "options": battle.options(),
                "record_id": match.id if battle.finished else None,
                "token": self._coach_token(match) if not battle.finished else None,
                "index": index,
                "mark": mark,
            }

    def abandon(self) -> dict[str, Any]:
        """放弃进行中的对局（不计入战斗记录），或离开已结束的对局。"""
        with self._lock:
            if self.match is None:
                raise Conflict("当前没有对局")
            self.match = None
            self.store.clear_match()
            return self.state()

    def list_battles(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self.store.list_battles()))

    def get_battle(self, battle_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.store.load_battle(battle_id)
            if record is None:
                raise NotFound("没有这场战斗记录")
            return record

    # ---------------------------------------------------------- 对手方 AI

    @staticmethod
    def _ai_controls(match: Match, side: str) -> bool:
        return match.opponent in ai.AI_MODES and side == ai.AI_SIDE

    def _after_pick(self, match: Match) -> None:
        if match.draft.complete:
            match.seed = self._new_seed()
            match.battle = Battle.new(match.lineups(), match.seed)

    def _run_ai(self, match: Match) -> list[dict[str, Any]]:
        """随机 / 智能模式下替对手方选将、出招，直到轮到我方或战斗结束。返回新产生的战斗事件。"""
        events: list[dict[str, Any]] = []
        while True:
            if match.battle is None:
                turn = match.draft.turn
                if turn is None or not self._ai_controls(match, turn):
                    return events
                rng = ai.rng_for(match.ai_seed, "draft", len(match.draft.picks))
                taken = match.draft.lineup(turn)
                if match.opponent == "smart":
                    species = smart.smart_pick(taken, match.draft.lineup("me"), rng)
                else:
                    species = ai.random_pick(taken, rng)
                match.draft.pick(turn, species)
                self._after_pick(match)
                if match.battle is not None:
                    events += match.battle.events
                continue
            battle = match.battle
            if battle.finished or battle.actor is None:
                return events
            if not self._ai_controls(match, battle.pets[battle.actor].side):
                return events
            rng = ai.rng_for(match.ai_seed, "battle", len(battle.actions))
            move = smart.smart_move if match.opponent == "smart" else ai.random_move
            events += battle.act(*move(battle, rng))

    # ------------------------------------------------------------ AI Coach

    @staticmethod
    def _coach_token(match: Match) -> str:
        """标识「这一局的这一手 / 这一次行动」：局面一推进就变，用来丢弃迟到的建议。"""
        if match.battle is not None:
            return f"{match.id}:battle:{len(match.battle.actions)}"
        return f"{match.id}:draft:{len(match.draft.picks)}"

    def coach_token(self) -> str | None:
        """当前选将或战斗局面的 token；没有进行中的对局时为 None。"""
        with self._lock:
            match = self.match
            if match is None or match.phase not in (DRAFT, BATTLE):
                return None
            return self._coach_token(match)

    def coach_draft_snapshot(self) -> dict[str, Any] | None:
        """选将局势的只读副本（在锁内复制，立即放锁）；不在选将阶段时为 None。"""
        with self._lock:
            match = self.match
            if match is None or match.phase != DRAFT:
                return None
            return {
                "token": self._coach_token(match),
                "match_id": match.id,
                "step": len(match.draft.picks),
                "turn": match.draft.turn,
                "picks": [list(pick) for pick in match.draft.picks],
                "roster": {
                    side: {key: dict(stats) for key, stats in pets.items()}
                    for side, pets in match.roster.items()
                },
                "opponent": match.opponent,
            }

    def current_roster(self) -> dict[str, dict[str, dict[str, int]]]:
        """双方全部宠物现在的属性（含 PVE 成长）。"""
        with self._lock:
            return pve.roster(self.growth)

    def coach_battle_snapshot(self) -> dict[str, Any] | None:
        """战斗局面的只读副本（在锁内复制，立即放锁）；不在战斗中时为 None。

        battle 是克隆出来的局面：推演在它上面进行，碰不到真实对局，也不带本局的事件。
        """
        with self._lock:
            match = self.match
            if match is None or match.phase != BATTLE:
                return None
            battle = match.battle
            actor = battle.actor
            return {
                "token": self._coach_token(match),
                "match_id": match.id,
                "opponent": match.opponent,
                "index": len(battle.actions),
                "turn": battle.pets[actor].side if actor else None,
                "actor": actor,
                "battle": battle.clone(),
                "options": battle.options(),
                "view": battle.view(),
                "static": battle.static(),
                "events": [event.get("text") or "" for event in battle.events],  # 给战斗 Agent 讲「上次之后发生了什么」
                "marks": {str(index): dict(mark) for index, mark in match.marks.items()},
            }

    def coach_note_eval(self, token: str, data: dict[str, Any]) -> bool:
        """记下战斗指导对这个决策点的推演结果（随对局记录保存，留给复盘）。局面已经推进时忽略。"""
        with self._lock:
            match = self.match
            if match is None or match.phase != BATTLE or self._coach_token(match) != token:
                return False
            match.evals[len(match.battle.actions)] = data
            return True

    def _require(self, phase: str, message: str) -> Match:
        if self.match is None or self.match.phase != phase:
            raise Conflict(message)
        return self.match

    def _record_battle(self, match: Match) -> None:
        battle = match.battle
        result = {
            "winner": battle.winner,
            "reason": battle.events[-1]["reason"],
            "rounds": battle.round,
        }
        record = {
            "id": match.id,
            "started_at": match.created_at,
            "ended_at": self._clock(),
            "seed": match.seed,
            "opponent": match.opponent,
            "lineups": match.lineups(),
            "roster": match.roster,  # 本局锁定的双方全部属性：复盘要用它评估「换一套阵容会怎样」
            "picks": match.draft.picks,
            "static": battle.static(),
            "actions": battle.actions,
            "events": battle.events,
            "result": result,
        }
        if match.marks:
            record["marks"] = {str(index): mark for index, mark in match.marks.items()}
        if match.evals:
            record["evals"] = {str(index): data for index, data in match.evals.items()}
        summary = {
            "id": match.id,
            "started_at": record["started_at"],
            "ended_at": record["ended_at"],
            "lineups": {side: match.draft.lineup(side) for side in SIDES},
            "opponent": match.opponent,
            "actions": len(battle.actions),
            **result,
        }
        self.store.save_battle(record, summary)

    def _save_match(self) -> None:
        match = self.match
        self.store.save_match({
            "id": match.id,
            "created_at": match.created_at,
            "roster": match.roster,
            "picks": match.draft.picks,
            "seed": match.seed,
            "opponent": match.opponent,
            "ai_seed": match.ai_seed,
            "actions": match.battle.actions if match.battle else [],
            **({"marks": {str(index): mark for index, mark in match.marks.items()}} if match.marks else {}),
            **({"evals": {str(index): data for index, data in match.evals.items()}} if match.evals else {}),
        })

    def _restore(self) -> None:
        """重启后恢复进行中的对局：用种子和行动序列重演。"""
        data = self.store.load_match()
        if not data:
            return
        try:
            match = Match(
                data["id"], data["created_at"], data["roster"], data["picks"], data["seed"],
                opponent=data.get("opponent", "manual"), ai_seed=data.get("ai_seed", 0),
            )
            if match.draft.complete:
                match.battle = Battle.replay(match.lineups(), match.seed, data["actions"])
            match.marks = {int(index): mark for index, mark in (data.get("marks") or {}).items()}
            match.evals = {int(index): value for index, value in (data.get("evals") or {}).items()}
            self.match = match
        except Exception:  # 存档与当前规则不兼容：丢弃这场对局
            self.match = None
            self.store.clear_match()

    # ------------------------------------------------------------------ PVE

    def pve_challenge(
        self, challenge_id: str, side: str, species: str, dungeon: str, mode: str
    ) -> dict[str, Any]:
        # side 只能是 "me"：对手方不支持历练
        """结算一次挑战。同一个 challenge_id 只判定和结算一次，重复请求返回原结果。"""
        with self._lock:
            if not isinstance(challenge_id, str) or not 1 <= len(challenge_id) <= 64:
                raise GameError("challenge_id 不合法")
            record = self._pve_by_id.get(challenge_id)
            if record is None:
                if self.phase in (DRAFT, BATTLE):
                    raise Conflict("选将和战斗期间不能进入 PVE")
                try:
                    pve.validate(side, species, dungeon, mode)  # 不合法的请求不消耗随机数
                    record = pve.settle(
                        self.growth, challenge_id=challenge_id, side=side, species=species,
                        dungeon=dungeon, mode=mode, roll=self._roll100(), time=self._clock(),
                    )
                except pve.PveError as error:
                    raise GameError(str(error)) from None
                self.store.append_pve(record)
                self.pve_records.append(record)
                self._pve_by_id[challenge_id] = record
            return self._pve_result(record)

    def pve_record(self, challenge_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._pve_by_id.get(challenge_id)
            if record is None:
                raise NotFound("没有这条 PVE 记录")
            return self._pve_result(record)

    def pve_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self.pve_records[-limit:]))

    def _pve_result(self, record: dict[str, Any]) -> dict[str, Any]:
        stats = pve.roster(self.growth)[record["side"]][record["species"]]
        return {
            "record": record,
            "stats": stats,
            "capped": pve.is_capped(stats, record["dungeon"]),
        }
