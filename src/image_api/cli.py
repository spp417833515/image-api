"""The image-api command line: `image-api serve` (and the subcommands registered in SUBCOMMANDS)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence

import uvicorn
from imagegen.factory import create_service

from . import cli_comfy
from .config import (
    DATA_DIR_RULE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_ENV,
    PORT_ENV,
    ConfigError,
    data_dir_arg,
    fill_from_env,
)
from .web.app import create_app

type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


class Parser(argparse.ArgumentParser):
    """Once the command line is read, options it left unset come from the environment (config.fill_from_env).

    So --help never reads the environment, an explicit option is never blocked by an unusable environment variable,
    and one that is needed but unusable is a one-line usage error (exit 2), not a traceback. Subcommand parsers are
    of this class too (argparse makes them like their parent).
    """

    def parse_known_args(
        self, args: Sequence[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> tuple[argparse.Namespace, list[str]]:
        options, extras = super().parse_known_args(args, namespace)
        try:
            fill_from_env(options)
        except ConfigError as exc:
            self.error(str(exc))
        return options, extras


def register_serve(commands: Subparsers) -> None:
    serve = commands.add_parser("serve", help="启动页面和 HTTP 接口")
    serve.add_argument("--host", help=f"监听地址（默认 ${HOST_ENV}，否则 {DEFAULT_HOST}）")
    serve.add_argument("--port", type=int, help=f"端口（默认 ${PORT_ENV}，否则 {DEFAULT_PORT}）")
    serve.add_argument("--data-dir", type=data_dir_arg, help=f"数据目录（默认 {DATA_DIR_RULE}）")
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
    parser = Parser(prog="image-api", description="生图入口：页面 + HTTP 接口 + 命令行")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for register in SUBCOMMANDS:
        register(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
