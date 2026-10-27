"""The image-api command line: `image-api serve` (and the subcommands registered in SUBCOMMANDS)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence

import uvicorn
from imagegen.factory import create_service

from . import cli_comfy
from .config import Settings, data_dir_arg
from .web.app import create_app

type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def register_serve(commands: Subparsers) -> None:
    settings = Settings.from_env()
    serve = commands.add_parser("serve", help="启动页面和 HTTP 接口")
    serve.add_argument("--host", default=settings.host, help=f"监听地址（默认 {settings.host}）")
    serve.add_argument("--port", type=int, default=settings.port, help=f"端口（默认 {settings.port}）")
    serve.add_argument(
        "--data-dir", type=data_dir_arg, default=settings.data_dir, help=f"数据目录（默认 {settings.data_dir}）"
    )
    serve.set_defaults(run=run_serve)


def run_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # not one line per ComfyUI request
    app = create_app(create_service(args.data_dir))
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


# The registration point. Each entry adds its subcommand(s) to `image-api`: it calls commands.add_parser(...) and
# sets run(args) -> exit code as a parser default. A group of subcommands lives in its own module (e.g. cli_comfy.py
# with register(commands)) and is added here as one more entry, plus its import above.
SUBCOMMANDS: tuple[Callable[[Subparsers], None], ...] = (register_serve, cli_comfy.register)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="image-api", description="生图入口：页面 + HTTP 接口 + 命令行")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for register in SUBCOMMANDS:
        register(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
