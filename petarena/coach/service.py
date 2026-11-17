"""CoachService：把 AI Coach 的各项功能串起来。

每一次建议请求（选将 / 战斗）都是同一个流程：

    快照（加锁读取局势，立即放锁）→ 校验 token → 引擎事实 + Agent（都在锁外）
    → 再次校验 token → 缓存、写日志、返回

选将、战斗由 Agent 完成（agentic/）：LLM 调用工具（经验检索、引擎推演），最后提交建议，guard 校验。
选将的 Top10 阵容表、战斗的推演都按 token 缓存：自动 / 手动重试只重新请 LLM，不重算。

对局结束后的工作在后台按顺序进行（见 process）：

    入库（引擎复盘）→ 定向推演（写入对阵经验、下结论）→ LLM 讲解（沉淀经验条目）→ 培养方案

接口一律返回普通字典：成功 {ok: true, ...}，失败 {ok: false, code, retryable, message}。
AI 只读：这里不会修改任何游戏状态；经验库只在 AI Coach 启用时创建。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import battle as battle_advice
from . import experience, growth, review, rollout
from .agentic import runtime
from .agentic.battle_agent import SESSION, BattleAgent, BattleSession, LastAdvice
from .agentic.draft_agent import DraftAgent
from .agentic.draft_context import Row, top_lineups
from .config import BACKGROUND, FAKE, GROWTH_FULL, LIVE, OFF, CoachConfig
from .draft import PREDICTED, AdviceFailure, Situation, profile_for, reply_model_for
from .experience import ExperienceBook
from .jobs import InlineRunner, ThreadRunner
from .knowledge import KnowledgeBase
from .llm import LLM, FakeLLM, HttpLLM, LLMError
from .matchup import (
    BaseTable, MatchupBook, base_roster, default_workers, load_base_table, normalize_roster, roster_hash,
)
from .memory import Memory

DISABLED = "disabled"
STALE = "stale"
NOT_YOUR_TURN = "not_your_turn"
NOT_FOUND = "not_found"
PENDING = "pending"

MESSAGES = {
    DISABLED: "AI 教练未启用",
    STALE: "局面已经变化",
    NOT_YOUR_TURN: "现在不是我方的选将回合",
    NOT_FOUND: "没有这场战斗记录",
    PENDING: "后台还在推演，稍等一下",
}
REPORT_ATTEMPTS = 3  # 后台生成讲解时最多请求几次（与前端的自动重试次数一致）
GROWTH_LOOKBACK = 6  # 培养方案参考最近几场对局里没赢的对手阵容


def _fail(token: str | None, code: str, message: str | None = None, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "token": token, "code": code, "retryable": retryable,
            "message": message or MESSAGES.get(code, "AI 请求失败")}


class CoachService:
    def __init__(
        self,
        game,
        config: CoachConfig,
        *,
        llm: LLM | None = None,
        knowledge: KnowledgeBase | None = None,
        base_table: BaseTable | None = None,
        workers: int | None = None,
        runner: Any = None,
    ) -> None:
        self.game = game
        self.config = config
        self.knowledge = knowledge or KnowledgeBase.load()
        fast = llm
        if llm is None and config.mode == LIVE:
            llm = HttpLLM(config)
            # 战斗指导每次行动都要等它：单独用更短的超时和更小的输出上限
            fast = HttpLLM(replace(config, timeout=min(config.timeout, config.fast_timeout), max_tokens=config.fast_tokens))
        elif llm is None and config.mode == FAKE:
            llm = fast = FakeLLM(self.knowledge, fail_first=config.fake_fails, delay=config.fake_delay)
        self.llm = llm
        self.advisor = DraftAgent(llm, self.knowledge) if llm is not None else None
        battle_budget = runtime.Budget(calls=4, seconds=max(10.0, 1.8 * config.fast_timeout), max_tokens=config.fast_tokens)
        self.battle_agent = BattleAgent(fast, config.battle_agent, battle_budget) if fast is not None else None
        self.review_advisor = review.ReviewAdvisor(llm) if llm is not None else None
        self.growth_advisor = growth.GrowthAdvisor(llm) if llm is not None else None
        self.data_dir = Path(game.store.root) / "coach"
        self._base_table = base_table if base_table is not None else load_base_table()
        self._workers = default_workers() if workers is None else workers

        self._lock = threading.Lock()
        self._books: dict[tuple[str, str], MatchupBook] = {}
        self._match_id: str | None = None
        self._results: dict[str, dict[str, Any]] = {}  # token → 成功结果
        self._feedback: dict[str, str] = {}  # token → 最近一次的错误反馈
        self._token_locks: dict[str, threading.Lock] = {}
        self._evals: dict[tuple[str, int], Future] = {}  # (token, 局数) → 引擎对这次行动的评估（后台算，先到先用）
        self._tops: dict[str, tuple[list[Row], bool]] = {}  # token → 选将的 Top10 阵容表
        self._last: LastAdvice | None = None  # 本局上一次交给玩家的战斗建议
        self._seen = 0  # 本局已经告诉战斗 Agent 的事件数
        self._session: BattleSession | None = None  # 会话模式：本局的整段对话
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="coach-eval")
        self._queued: set[str] = set()  # 正在后台处理的对局
        self._long_feedback: dict[str, str] = {}  # 复盘讲解、培养建议的错误反馈（不随对局清空）
        self._growth_lock = threading.Lock()

        self.memory = Memory(self.data_dir / "coach.db") if self.enabled else None
        self.jobs = runner or (ThreadRunner() if config.reflect == BACKGROUND else InlineRunner())
        if self.enabled and config.reflect != OFF:
            self.catch_up()
        if self.enabled and self._workers > 1:  # 提前把战斗指导的进程池拉起来：第一次建议不用等进程启动
            threading.Thread(target=rollout.warm_up, args=(self._workers,), daemon=True).start()

    # ------------------------------------------------------------ 状态

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.advisor is not None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.config.mode,
            "model": self.llm.name if self.llm is not None else None,
            "setup": self.config.setup_needed,  # 没有配置 API：AI 军师面板提示去配置，不退回假模型
        }

    def _token_lock(self, match_id: str, token: str) -> threading.Lock:
        with self._lock:
            if match_id != self._match_id:  # 换了一局：清掉上一局的缓存和反馈
                self._match_id = match_id
                self._results.clear()
                self._feedback.clear()
                self._token_locks.clear()
                self._evals.clear()
                self._tops.clear()
                self._last, self._seen, self._session = None, 0, None
            return self._token_locks.setdefault(token, threading.Lock())

    # ------------------------------------------------------------ 选将建议

    def draft(self, token: str, attempt: int = 1) -> dict[str, Any]:
        if not self.enabled:
            return _fail(token, DISABLED)
        snap = self.game.coach_draft_snapshot()
        if snap is None or snap["token"] != token:
            return _fail(token, STALE)
        if snap["turn"] != "me":
            return _fail(token, NOT_YOUR_TURN)

        with self._token_lock(snap["match_id"], token):  # 同一手的并发请求排队，后到的直接拿缓存
            with self._lock:
                cached = self._results.get(token)
                feedback = self._feedback.get(token)
                top = self._tops.get(token)
            if cached is not None:
                return cached

            situation = Situation.from_snapshot(snap)
            profile = profile_for(situation.opponent)
            started = time.monotonic()
            # 对阵表叠加经验（内置的 + 这位玩家自己打出来的）：Top10 的每个数字都按「照指导出招、这类对手」修正过
            book = ExperienceBook(self._book_for(situation.roster, profile.opp_policy), self.memory, situation.opponent,
                                  seed=self._seed(situation.opponent))
            try:
                if top is None:  # Top10 只算一次：重试只是重新请 LLM
                    prior = None if book.complete else self._prior(book.opp_policy)
                    if prior is not None and book.active:
                        # 属性有成长时，基础属性的表只用来挑出值得现场推演的阵容；它也要带上经验，
                        # 否则对智能对手挑出来的全是「表上好看、照指导打不赢」的阵容
                        prior = ExperienceBook(prior, self.memory, situation.opponent, seed=self._seed(situation.opponent))
                    top = top_lineups(situation, book, profile, self.knowledge, prior)
                    book.save()
                    with self._lock:
                        if self._match_id == snap["match_id"]:
                            self._tops[token] = top
                engine_ms = int((time.monotonic() - started) * 1000)
                rows, exhaustive = top
                result = self.advisor.advise(
                    situation, profile, rows, memory=self.memory,
                    record=experience.real_record(self.memory, situation.opponent),
                    attempt=attempt, feedback=feedback, exhaustive=exhaustive)
            except LLMError as error:
                self._log(token, attempt, ok=False, code=error.code, message=error.message)
                return _fail(token, error.code, error.message, error.retryable)
            except AdviceFailure as error:
                with self._lock:
                    if error.feedback and self._match_id == snap["match_id"]:
                        self._feedback[token] = error.feedback
                self._log(token, attempt, ok=False, code=error.code, message=error.message, feedback=error.feedback)
                return _fail(token, error.code, error.message, error.retryable)

            if self.game.coach_token() != token:  # 计算期间玩家已经操作：丢弃
                self._log(token, attempt, ok=False, code=STALE)
                return _fail(token, STALE)
            replies = reply_model_for(profile, situation.opp_picked)(tuple(result.advice["plan"]))
            lessons = experience.relevant_lessons(self.memory, situation.opponent, situation.my_picked, situation.opp_picked)
            result.evidence["experience"] = {**book.explain(result.advice["plan"], replies), "lessons": len(lessons)}
            response = {
                "ok": True,
                "token": token,
                "advice_id": "adv_" + secrets.token_hex(4),
                "advice": result.advice,
                "evidence": result.evidence,
                "trace": {**result.trace, "engine_ms": engine_ms, "total_ms": int((time.monotonic() - started) * 1000)},
            }
            with self._lock:
                if self._match_id == snap["match_id"]:
                    self._results[token] = response
            self.memory.note_advice(token, snap["match_id"], "draft", situation.step,
                                    {"pet": result.advice["pet"], "plan": result.advice["plan"]},
                                    result.evidence["chosen"]["win_rate"])
            self._log(token, attempt, ok=True, pet=result.advice["pet"], plan=result.advice["plan"],
                      chosen=result.evidence["chosen"]["id"], win_rate=result.evidence["chosen"]["win_rate"],
                      **{key: response["trace"][key] for key in ("llm_calls", "llm_ms", "tool_ms", "engine_ms", "usage")},
                      tools=[step["tool"] for step in result.trace["steps"] if step["kind"] == "tool"])
            return response

    def note_pick(self, token: str | None, side: str, species: str) -> None:
        """玩家确认了选择：记录这一手是否采纳了建议（仅供查看，不影响任何结算）。"""
        if not self.enabled or token is None or side != "me":
            return
        with self._lock:
            result = self._results.get(token)
        if result is None:
            return
        advised = result["advice"]["pet"]
        self.memory.note_choice(token, species, advised == species)
        self._log(token, None, type="adopted", advice_id=result["advice_id"],
                  advised=advised, picked=species, adopted=advised == species)

    # ------------------------------------------------------------ 战斗指导

    def _evaluation(self, snap: dict[str, Any], games: int) -> Future:
        """这次行动的引擎评估（后台线程计算，同一个 token 和局数只算一次）。"""
        key = (snap["token"], games)
        with self._lock:
            future = self._evals.get(key)
            if future is None:
                battle = snap["battle"].clone()  # 推演用自己的副本，不和 Agent 读局面的那份共用
                profile = profile_for(snap["opponent"])
                future = self._pool.submit(battle_advice.evaluate, {**snap, "battle": battle}, profile, games,
                                           self._workers)
                if self._match_id == snap["match_id"]:
                    self._evals[key] = future
        return future

    def _note_eval(self, snap: dict[str, Any], evaluation: battle_advice.Evaluation) -> None:
        """推演结果随对局记录保存（留给复盘复用）。"""
        self.game.coach_note_eval(snap["token"], {
            "games": evaluation.games, "salt": snap["token"],
            "moves": [{"skill": value.move[0], "target": value.move[1], "value": round(value.value, 4),
                       "win": value.win, "draw": value.draw, "games": value.games} for value in evaluation.ranked],
        })

    def battle(self, token: str, attempt: int = 1) -> dict[str, Any]:
        if not self.enabled:
            return _fail(token, DISABLED)
        snap = self.game.coach_battle_snapshot()
        if snap is None or snap["token"] != token:
            return _fail(token, STALE)
        if snap["turn"] != "me":
            return _fail(token, NOT_YOUR_TURN, "现在不是我方的行动回合")

        with self._token_lock(snap["match_id"], token):
            with self._lock:
                cached = self._results.get(token)
                feedback = self._feedback.get(token)
                last, seen = self._last, self._seen
                session = self._session
                if session is None and self.battle_agent.mode == SESSION:
                    session = self._session = BattleSession(snap["match_id"], rebuilt=bool(snap.get("marks")))
            if cached is not None:
                return cached

            profile = profile_for(snap["opponent"])
            started = time.monotonic()
            budget = self.battle_agent.budget

            def evaluate() -> battle_advice.Evaluation:
                """全部合法动作的推演（同一次行动只算一次，重试时直接复用）。LLM 要等它算完才开始。"""
                try:
                    evaluation = self._evaluation(snap, self.config.advice_games).result(timeout=budget.seconds)
                except FutureTimeout:
                    raise AdviceFailure("timeout", "推演超时") from None
                self._note_eval(snap, evaluation)
                return evaluation

            def remember(advice: LastAdvice, events: int) -> None:
                with self._lock:
                    if self._match_id == snap["match_id"]:
                        self._last, self._seen = advice, events

            try:
                result = self.battle_agent.advise(
                    snap, profile, evaluate, memory=self.memory, last=last, seen=seen, session=session,
                    attempt=attempt, feedback=feedback, on_commit=remember)
            except LLMError as error:
                self._log(token, attempt, file="battle_log.jsonl", ok=False, code=error.code, message=error.message)
                return _fail(token, error.code, error.message, error.retryable)
            except AdviceFailure as error:
                with self._lock:
                    if error.feedback and self._match_id == snap["match_id"]:
                        self._feedback[token] = error.feedback
                self._log(token, attempt, file="battle_log.jsonl", ok=False, code=error.code, message=error.message,
                          feedback=error.feedback)
                return _fail(token, error.code, error.message, error.retryable)

            if self.game.coach_token() != token:  # 计算期间玩家已经操作：丢弃（会话模式也不接这一轮）
                self._log(token, attempt, file="battle_log.jsonl", ok=False, code=STALE)
                return _fail(token, STALE)
            result.commit()
            response = {
                "ok": True,
                "token": token,
                "advice_id": "adv_" + secrets.token_hex(4),
                "advice": result.advice,
                "evidence": result.evidence,
                "trace": {**result.trace, "total_ms": int((time.monotonic() - started) * 1000)},
            }
            with self._lock:
                if self._match_id == snap["match_id"]:
                    self._results[token] = response
            move = [result.advice["skill"], result.advice["target"]]
            self.memory.note_advice(token, snap["match_id"], "battle", snap["index"], move,
                                    result.evidence["moves"][0]["win_rate"])
            self._log(token, attempt, file="battle_log.jsonl", ok=True, actor=snap["actor"], move=move,
                      **{key: response["trace"][key] for key in (
                          "mode", "basis", "moves", "chosen_rank", "evaluated", "llm_calls", "llm_ms", "tool_ms", "engine_ms",
                          "usage", "total_ms")},
                      tools=[step["tool"] for step in result.trace["steps"] if step["kind"] == "tool"])
            return response

    def judge_action(self, token: str | None, skill: str, target: str | None,
                     advice_id: str | None) -> dict[str, Any] | None:
        """玩家即将执行这个动作：对照他看到的那条建议，得出「是否采纳」的标注。只读。

        advice_id 是玩家操作时面板上正显示着的建议。服务端算好了、但玩家没看到的建议（关了指导，
        或者建议回来之前就操作了）不算数：那样的行动不标注。
        """
        if not self.enabled or token is None or advice_id is None or ":battle:" not in token:
            return None
        with self._lock:
            result = self._results.get(token)
        if result is None or result["advice_id"] != advice_id:
            return None
        advised = [result["advice"]["skill"], result["advice"]["target"]]
        return {"token": token, "advised": advised, "adopted": advised == [skill, target]}

    def note_action(self, token: str | None, mark: dict[str, Any] | None, skill: str, target: str | None,
                    result: dict[str, Any]) -> None:
        """动作已经执行：记下是否采纳；对局结束时安排后台复盘。"""
        if not self.enabled:
            return
        if mark is not None and result.get("mark") is not None:
            self.memory.note_choice(token, [skill, target], mark["adopted"])
            self._log(token, None, file="battle_log.jsonl", type="adopted", advised=mark["advised"],
                      played=[skill, target], adopted=mark["adopted"])
        if result.get("record_id") and self.config.reflect != OFF:
            self.enqueue(result["record_id"])

    # ------------------------------------------------------------ 对局结束后的后台工作

    def catch_up(self) -> None:
        """把还没进经验库的战斗记录补上，上次做到一半的接着做（启动时调用）。"""
        known = self.memory.known_battles()
        for summary in self.game.store.list_battles():
            if summary["id"] not in known:
                self.enqueue(summary["id"])
        for battle_id in self.memory.unfinished():
            self.enqueue(battle_id)

    def enqueue(self, battle_id: str) -> None:
        with self._lock:
            if battle_id in self._queued:
                return
            self._queued.add(battle_id)
        self.jobs.submit(self.process, battle_id)

    def absorb(self, battle_id: str) -> dict[str, Any] | None:
        """只做不需要 LLM 的两步：入库（引擎复盘）和定向推演。返回这场对局在经验库里的一行。"""
        row = self._ingest(battle_id)
        if row is not None and row["status"] == review.READY:
            self._reflect(battle_id)
            row = self.memory.battle(battle_id)
        return row

    def process(self, battle_id: str) -> None:
        """入库 → 定向推演 → 讲解 → 培养方案。每一步做过就跳过，所以中断后可以接着做。"""
        try:
            row = self.absorb(battle_id)
            if row is None or row["status"] != review.READY:
                return
            for _ in range(REPORT_ATTEMPTS):
                outcome = self._report(battle_id)
                if outcome is None or not outcome.get("retryable"):
                    break
            self._growth_data()
        finally:
            with self._lock:
                self._queued.discard(battle_id)

    def _shown(self, record: dict[str, Any]) -> float | None:
        """选将时面板显示的胜率：只有最终阵容就是最后一手建议的计划阵容时才有意义。"""
        rows = self.memory.advice_of(record["id"], "draft")
        if not rows:
            return None
        last = rows[-1]
        mine = [pet["species"] for pet in record["lineups"]["me"]]
        return last["shown"] if (last["advised"] or {}).get("plan") == mine else None

    def _ingest(self, battle_id: str) -> dict[str, Any] | None:
        row = self.memory.battle(battle_id)
        if row is not None:
            return row
        record = self.game.store.load_battle(battle_id)
        if record is None:
            return None
        if not review.replayable(record):
            self.memory.save_battle(review.legacy_row(record))
            return self.memory.battle(battle_id)
        profile = profile_for(record.get("opponent", "manual"))
        book = self._book_for(record["roster"], profile.opp_policy)
        prior = None if book.complete else self._prior(profile.opp_policy)
        analysis = review.analyse(record, book=book, prior=prior, profile=profile, games=self.config.review_games,
                                  goal=self.config.growth_target, shown=self._shown(record), workers=self._workers)
        book.save()
        self.memory.save_battle(analysis.row, analysis.decisions, analysis.experience)
        return self.memory.battle(battle_id)

    def _reflect(self, battle_id: str) -> None:
        row = self.memory.battle(battle_id)
        if row is None or row["status"] != review.READY or row["reflected"]:
            return
        record = self.game.store.load_battle(battle_id)
        roster = normalize_roster(record["roster"])
        profile = profile_for(row["opponent"])
        book = self._book_for(roster, profile.opp_policy)
        guide = book if book.complete else (self._prior(profile.opp_policy) or book)
        budget = 0 if self.config.reflect == OFF else self.config.reflect_games
        result = experience.reflect(row, roster=roster, memory=self.memory, book=book, guide=guide, profile=profile,
                                    budget=budget, workers=self._workers, goal=self.config.growth_target,
                                    seed=self._seed(row["opponent"]))
        ceiling = result["ceiling"]
        facts = row["facts"]
        facts["ceiling"] = ceiling
        if ceiling["guided"]:
            names = lambda keys: "、".join(review.SPECIES[key].name for key in keys)  # noqa: E731
            same = ceiling["lineup"] == row["my_lineup"]
            if same:
                text = (f"实际的{names(row['my_lineup'])}就是推演过的阵容里最好的，约 {round(ceiling['value'] * 100)}% 胜算")
            elif ceiling["value"] - ceiling["played"] < review.LINEUP_GAP:  # 差得不多：不要让它被讲成「换阵容更好」
                text = (f"推演过的阵容里没有明显更好的：最好的{names(ceiling['lineup'])}约 {round(ceiling['value'] * 100)}% 胜算，"
                        f"实际的{names(row['my_lineup'])}约 {round(ceiling['played'] * 100)}%，差别在误差之内")
            elif ceiling["replies"]:  # 对手的选将跟着我方变：换阵容之后对上的不再是这一局的对手阵容
                text = (f"我方换成{names(ceiling['lineup'])}（按站位）时，它按选将习惯多半会拿出"
                        f"{' 或 '.join(names(reply) for reply in ceiling['replies'])}，这样约 {round(ceiling['value'] * 100)}% 胜算；"
                        f"实际的{names(row['my_lineup'])}约 {round(ceiling['played'] * 100)}%")
            else:
                text = (f"{names(ceiling['lineup'])}（按站位）约 {round(ceiling['value'] * 100)}% 胜算，"
                        f"实际的{names(row['my_lineup'])}约 {round(ceiling['played'] * 100)}%")
            facts["moments"].append({
                "id": "A3", "kind": "ceiling", "round": 0, "idx": -1,
                "text": f"照战斗指导出招的整局推演（{ceiling['games']} 局）：对上{names(row['opp_lineup'])}这一局的对手，{text}",
            })
            # 归因改用「照指导出招、对上对手真实打法」的口径：对上会推演的对手，它和对阵表差得很远
            parts = review.attribute(row["outcome"], ceiling["value"], ceiling["played"], row["missed"] or 0.0)
            facts["attribution"] = {key: round(value, 4) for key, value in parts.items()}
            facts["basis"] = review.GUIDED
        parts = facts["attribution"]
        self.memory.update_battle(
            battle_id, ceiling=ceiling["value"], ceiling_lineup=ceiling["lineup"], ceiling_games=ceiling["games"],
            reflected=True, verdict=result["verdict"], facts=facts, attr_stats=parts["stats"],
            attr_lineup=parts["lineup"], attr_play=parts["play"], attr_luck=parts["luck"])

    def _report(self, battle_id: str) -> dict[str, Any] | None:
        """生成讲解并沉淀经验条目。成功或不需要做时返回 None，失败返回失败信息。"""
        row = self.memory.battle(battle_id)
        if row is None or row["status"] != review.READY or row["report"] is not None or self.review_advisor is None:
            return None
        key = f"review:{battle_id}"
        facts = {**row["facts"], "verdict": row["verdict"]}
        try:
            report = self.review_advisor.report(battle_id, facts, self._long_feedback.get(key))
        except LLMError as error:
            self._log(key, None, file="review_log.jsonl", ok=False, code=error.code, message=error.message)
            return _fail(key, error.code, error.message, error.retryable)
        except AdviceFailure as error:
            if error.feedback:
                self._long_feedback[key] = error.feedback
            self._log(key, None, file="review_log.jsonl", ok=False, code=error.code, message=error.message)
            return _fail(key, error.code, error.message, error.retryable)
        self._long_feedback.pop(key, None)
        for lesson in report["lessons"]:
            lesson["key"] = self.memory.add_lesson(battle_id, row["opponent"], lesson["scope"], lesson["polarity"],
                                                   lesson["pets"], lesson["against"], lesson["text"])
        self.memory.update_battle(battle_id, report=report)
        self._log(key, None, file="review_log.jsonl", ok=True, lessons=len(report["lessons"]), **report["trace"])
        return None

    # ------------------------------------------------------------ 复盘接口

    def _review_response(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            working = row["id"] in self._queued
        facts = row["facts"] or {}
        response = {
            "ok": True, "id": row["id"], "status": row["status"], "verdict": row["verdict"],
            "cause": review.cause_text(row["verdict"], facts.get("attribution")), "facts": facts, "report": row["report"],
            "stages": {"reflected": bool(row["reflected"]), "report": row["report"] is not None, "working": working},
            "goal": self.config.growth_target,
        }
        if row["status"] == review.READY:
            response["lessons"] = [lesson for lesson in self.memory.lessons(row["opponent"], active=False)
                                   if row["id"] in (lesson["first_battle"], lesson["last_battle"])]
            response["quality"] = self.memory.quality()
            latest = self.memory.battles(limit=1)
            response["latest"] = bool(latest) and latest[0]["id"] == row["id"]  # 培养建议只针对最近一场对局
        return response

    def review(self, battle_id: str) -> dict[str, Any]:
        """复盘：引擎分析立即可看；定向推演和讲解在后台完成后陆续出现（stages 表示进度）。"""
        if not self.enabled:
            return _fail(None, DISABLED)
        row = self.memory.battle(battle_id)
        if row is None and self.game.store.load_battle(battle_id) is None:
            return _fail(None, NOT_FOUND)
        if row is None or (row["status"] == review.READY and not row["reflected"]):
            # 还没分析，或者上次做到一半服务停了：接着做
            if self.config.reflect == OFF:
                row = self.absorb(battle_id)  # 后台工作关掉了：当场分析，结论按对阵表下（不做定向推演）
            else:
                self.enqueue(battle_id)
                row = self.memory.battle(battle_id)  # 当场执行的模式下已经处理完了
        if row is None:
            return {"ok": True, "id": battle_id, "status": PENDING,
                    "stages": {"reflected": False, "report": False, "working": True}}
        return self._review_response(row)

    def report(self, battle_id: str, attempt: int = 1) -> dict[str, Any]:
        """（重新）请求讲解：后台生成失败之后，由玩家点「重试」触发。"""
        if not self.enabled:
            return _fail(None, DISABLED)
        row = self.memory.battle(battle_id)
        with self._lock:
            working = battle_id in self._queued
        if row is None or working:
            if row is None and self.game.store.load_battle(battle_id) is None:
                return _fail(None, NOT_FOUND)
            return _fail(None, PENDING, retryable=True)
        if row["status"] != review.READY:
            return _fail(None, INVALID_RECORD, "这场对局是旧版规则下的记录，无法复盘")
        if row["report"] is None:
            self._reflect(battle_id)
            failure = self._report(battle_id)
            if failure is not None:
                return failure
        return self._review_response(self.memory.battle(battle_id))

    # ------------------------------------------------------------ 培养建议

    def growth_target(self) -> dict[str, Any] | None:
        """培养方案针对什么：最近一场进了经验库的对局、要练的我方阵容、要对付的对手阵容（最多 3 套）。

        练哪套阵容：复盘的结论是「换阵容」时练推荐的那套，否则练上一局实际用的那套。
        对付谁：对手的选将会跟着我方变（智能对手）时，按它的选将习惯预测它面对这套阵容最可能
        拿出的几套；否则用最近没赢的几局里实际遇到的。
        """
        rows = self.memory.battles(limit=GROWTH_LOOKBACK)
        if not rows:
            return None
        latest = rows[0]
        switch = latest["verdict"] == "lineup" and bool(latest["ceiling_lineup"])
        lineup = latest["ceiling_lineup"] if switch else latest["my_lineup"]
        profile = profile_for(latest["opponent"])
        targets = [list(reply) for reply, _ in
                   experience.foreseen(profile, latest["opp_lineup"], lineup, include_met=not switch)]
        if profile.replies != PREDICTED:  # 对手的选将不看我方：把最近没赢的几局里遇到的阵容也算上
            targets += [row["opp_lineup"] for row in rows[1:]
                        if row["opponent"] == latest["opponent"] and row["outcome"] < 1 and row["opp_lineup"] not in targets]
        return {"row": latest, "targets": targets[:3], "lineup": lineup}

    def _growth_data(self) -> dict[str, Any] | None:
        """引擎的培养方案（按对局缓存）。还不能算（对局没推演完）时返回 None。"""
        target = self.growth_target()
        if target is None:
            return {"battle_id": None, "opponent": None, "verdict": "none", "needed": False,
                    "headline": "还没有可以分析的对局：先打一局 PVP", "data": None, "advice": None}
        row = target["row"]
        key = f"growth:{row['id']}"
        with self._growth_lock:
            cached = self.memory.get(key)
            if cached is not None:
                return cached
            if not row["reflected"]:
                return None
            verdict = "none" if row["outcome"] >= 1 else (row["verdict"] or "luck")
            # 需不需要培养：对这套对手阵容最好的阵容、照指导出招，也到不了目标
            needed = verdict != "none" and row["ceiling"] is not None and row["ceiling"] < self.config.growth_target
            data = None
            if verdict != "none" and self.config.growth != GROWTH_FULL:
                # 默认：不做模拟，只按台阶给方向（立刻就有）
                data = growth.suggest(roster=normalize_roster(self.game.current_roster()),
                                      lineup=target["lineup"], targets=target["targets"])
            elif verdict != "none":
                roster = normalize_roster(self.game.current_roster())
                profile = profile_for(row["opponent"])
                data = growth.plan(
                    roster=roster, lineup=target["lineup"], targets=target["targets"], profile=profile,
                    games=self.config.growth_games, goal=self.config.growth_target, workers=self._workers,
                    verify=self.config.growth_verify if needed and self.config.reflect != OFF else 0,
                    salt=row["id"])
            headline = "上一局赢了，现在不需要培养" if verdict == "none" else growth.headline(verdict, data, needed)
            cached = {"battle_id": row["id"], "opponent": row["opponent"], "verdict": verdict, "needed": needed,
                      "headline": headline, "data": data, "advice": None}
            self.memory.put(key, cached)
            return cached

    def growth(self, attempt: int = 1) -> dict[str, Any]:
        """培养建议：引擎的方案 + LLM 的讲解。方案还在后台计算时返回 pending（不算一次失败）。"""
        if not self.enabled:
            return _fail(None, DISABLED)
        if self.config.reflect == OFF:  # 后台工作关掉了：这里把最近一场对局补进经验库（不做定向推演）
            summaries = self.game.store.list_battles()
            if summaries and self.memory.battle(summaries[-1]["id"]) is None:
                self.absorb(summaries[-1]["id"])
        target = self.growth_target()
        with self._lock:
            working = target is not None and target["row"]["id"] in self._queued
        cached = None if working else self._growth_data()
        if cached is None:
            return _fail(None, PENDING, retryable=True)
        if cached["advice"] is None and cached["data"] is not None and cached["data"]["steps"]:
            key = f"growth:{cached['battle_id']}"
            try:
                advice = self.growth_advisor.advise(cached["data"], cached["verdict"], cached["battle_id"],
                                                    self._long_feedback.get(key), cached["needed"])
            except LLMError as error:
                self._log(key, attempt, file="growth_log.jsonl", ok=False, code=error.code, message=error.message)
                return _fail(key, error.code, error.message, error.retryable)
            except AdviceFailure as error:
                if error.feedback:
                    self._long_feedback[key] = error.feedback
                self._log(key, attempt, file="growth_log.jsonl", ok=False, code=error.code, message=error.message)
                return _fail(key, error.code, error.message, error.retryable)
            self._long_feedback.pop(key, None)
            cached = {**cached, "advice": advice}
            self.memory.put(key, cached)
            self._log(key, attempt, file="growth_log.jsonl", ok=True, steps=len(cached["data"]["steps"]), **advice["trace"])
        return {"ok": True, "plan": cached}

    def memory_summary(self) -> dict[str, Any]:
        """经验库里现在有什么（给界面和排查用）。"""
        if not self.enabled:
            return _fail(None, DISABLED)
        return {"ok": True, "counts": self.memory.counts(), "quality": self.memory.quality(),
                "lessons": self.memory.lessons(active=False)[:20]}

    # ------------------------------------------------------------ 内部

    def _seed(self, opponent: str) -> tuple:
        """这类对手的内置经验（离线普查过的「照指导出招」的战绩）；没有或者关掉了就是空的。"""
        return experience.load_seed(opponent) if self.config.seed else ()

    def _book_for(self, roster: dict[str, Any], policy: str) -> MatchupBook:
        """某一套锁定属性 + 对手打法对应的对阵表；同一套属性只建一次。"""
        key = (roster_hash(roster), policy)
        with self._lock:
            book = self._books.get(key)
            if book is None:
                table = self._base_table
                if table is None or table.hash != key[0]:
                    # 属性有成长：看看有没有为这套属性离线算好的完整表（prewarm_matchups.py --data-dir）
                    table = load_base_table(self.data_dir / f"table-{key[0]}.json") or table
                book = MatchupBook(
                    roster, opp_policy=policy, base=table,
                    cache_path=self.data_dir / f"matchups-{key[0]}-{policy}.json", workers=self._workers,
                )
                if len(self._books) >= 8:
                    self._books.pop(next(iter(self._books)))
                self._books[key] = book
            return book

    def _prior(self, policy: str) -> MatchupBook | None:
        """基础属性的完整对阵表：属性有成长、算不起穷举时，用它挑出值得现场推演的阵容。"""
        table = self._base_table
        if table is None or not table.has(policy):
            return None
        key = ("base", policy)
        with self._lock:
            if key not in self._books:
                self._books[key] = MatchupBook(base_roster(), opp_policy=policy, base=table)
            return self._books[key]

    def _log(self, token: str, attempt: int | None, file: str = "draft_log.jsonl", **fields: Any) -> None:
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "token": token, "attempt": attempt,
                  "mode": self.config.mode, **fields}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_dir / file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志写不进去不影响建议


INVALID_RECORD = "legacy"
