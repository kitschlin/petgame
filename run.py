"""启动《萌宠竞技场》：python run.py [--port 8765]"""

from __future__ import annotations

import argparse

import uvicorn

from petarena.api import DEFAULT_DATA_DIR, create_app
from petarena.service import GameService
from petarena.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="萌宠竞技场")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="存档目录")
    parser.add_argument("--seed", type=int, default=None, help="固定每场战斗的随机种子（用于测试、复现问题）")
    args = parser.parse_args()
    new_seed = None if args.seed is None else (lambda: args.seed)
    service = GameService(Store(args.data_dir), new_seed=new_seed)
    print(f"萌宠竞技场已启动：http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(service=service), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
