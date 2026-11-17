"""HTTP 接口与静态前端。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .coach.config import load_config
from .coach.service import CoachService
from .service import GameError, GameService
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DEFAULT_DATA_DIR = ROOT / "data"


class StartIn(BaseModel):
    opponent: str = "manual"  # "manual" 同屏手动 / "random" 电脑随机 / "smart" 电脑智能


class PickIn(BaseModel):
    side: str
    species: str


class ActionIn(BaseModel):
    skill: str
    target: str | None = None
    advice_id: str | None = None  # 玩家操作时面板上正显示着的那条 AI 建议；没有显示（关了指导、建议还没回来）就不带


class CoachDraftIn(BaseModel):
    token: str
    attempt: int = 1


class CoachAttemptIn(BaseModel):
    attempt: int = 1


class ChallengeIn(BaseModel):
    challenge_id: str
    side: str = "me"  # 只能培养我方宠物，保留字段以兼容旧请求
    species: str
    dungeon: str
    mode: str


class NoCacheStatic(StaticFiles):
    """前端没有构建步骤，文件名不带哈希：禁用缓存，保证改完刷新即生效。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app(
    data_dir: str | Path | None = None,
    service: GameService | None = None,
    coach: CoachService | None = None,
) -> FastAPI:
    app = FastAPI(title="萌宠竞技场")
    game = service or GameService(Store(data_dir or DEFAULT_DATA_DIR))
    coach = coach or CoachService(game, load_config())
    app.state.game = game
    app.state.coach = coach

    @app.exception_handler(GameError)
    async def game_error(request: Request, error: GameError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    @app.get("/api/state")
    def state():
        return game.state()

    # ---- PVP ----

    @app.post("/api/match")
    def start_match(body: StartIn | None = None):
        return game.start_match((body or StartIn()).opponent)

    @app.delete("/api/match")
    def abandon_match():
        return game.abandon()

    @app.post("/api/match/pick")
    def pick(body: PickIn):
        token = game.coach_token()
        state = game.pick(body.side, body.species)
        coach.note_pick(token, body.side, body.species)  # 只记录是否采纳了建议
        return state

    @app.post("/api/match/action")
    def action(body: ActionIn):
        token = game.coach_token()
        mark = coach.judge_action(token, body.skill, body.target, body.advice_id)  # 只读：对照玩家看到的那条建议
        result = game.action(body.skill, body.target, mark)
        coach.note_action(token, mark, body.skill, body.target, result)  # 记录是否采纳；对局结束时安排复盘
        return result

    @app.get("/api/battles")
    def battles():
        return game.list_battles()

    @app.get("/api/battles/{battle_id}")
    def battle(battle_id: str):
        return game.get_battle(battle_id)

    # ---- PVE ----

    @app.post("/api/pve/challenges")
    def challenge(body: ChallengeIn):
        return game.pve_challenge(body.challenge_id, body.side, body.species, body.dungeon, body.mode)

    @app.get("/api/pve/records")
    def pve_records(limit: int = 50):
        return game.pve_history(max(1, min(limit, 500)))

    @app.get("/api/pve/records/{challenge_id}")
    def pve_record(challenge_id: str):
        return game.pve_record(challenge_id)

    # ---- AI Coach：失败也返回 200，用 {ok: false, code, retryable} 表示 ----

    @app.get("/api/coach/status")
    def coach_status():
        return coach.status()

    @app.post("/api/coach/draft")
    def coach_draft(body: CoachDraftIn):
        return coach.draft(body.token, max(1, body.attempt))

    @app.post("/api/coach/battle")
    def coach_battle(body: CoachDraftIn):
        return coach.battle(body.token, max(1, body.attempt))

    @app.get("/api/coach/review/{battle_id}")
    def coach_review(battle_id: str):
        return coach.review(battle_id)

    @app.post("/api/coach/review/{battle_id}/report")
    def coach_report(battle_id: str, body: CoachAttemptIn | None = None):
        return coach.report(battle_id, max(1, (body or CoachAttemptIn()).attempt))

    @app.post("/api/coach/growth")
    def coach_growth(body: CoachAttemptIn | None = None):
        return coach.growth(max(1, (body or CoachAttemptIn()).attempt))

    @app.get("/api/coach/memory")
    def coach_memory():
        return coach.memory_summary()

    app.mount("/", NoCacheStatic(directory=WEB_DIR, html=True), name="web")
    return app
