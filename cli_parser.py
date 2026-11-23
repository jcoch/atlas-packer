#!/usr/bin/env python3
"""Stage 1: Pure CLI argument parsing for the Texture Atlas Packer.

Collects global execution options and pairs each target path (including
``--default`` or an empty CLI target list) with the ``--glob`` pattern active
at that position on the command line in strict left-to-right appearance order.
Performs zero filesystem I/O.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Optional, Sequence

DEFAULT_GLOB = "wtxpck.*.conf"
SCRIPT_DIR = str(Path(__file__).resolve().parent)


def _normalize_target_path(raw_path: str) -> str:
    """Resolve user-home markers, relative segments, and trailing slashes cleanly."""
    return os.path.abspath(os.path.expanduser(raw_path))


@dataclass(frozen=True)
class TargetSpec:
    """A single CLI target path paired with its active glob pattern."""

    raw_path: str
    glob_pattern: str


@dataclass(frozen=True)
class CliOptions:
    """Normalized, immutable result of Stage 1 CLI parsing."""

    targets: tuple[TargetSpec, ...]
    force: bool
    parallel: bool
    share: bool
    pretty: bool


class _SetGlobAction(argparse.Action):
    """Updates ``namespace.current_glob`` and tracks the flag position."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: Optional[str] = None,
    ) -> None:
        if not isinstance(values, str) or not values:
            parser.error(f"{option_string or '--glob'} requires a non-empty pattern")
        namespace.current_glob = values
        namespace.pending_glob_flag = option_string or "--glob"


class _DefaultTargetAction(argparse.Action):
    """Appends ``(SCRIPT_DIR, current_glob)`` at the exact CLI position of ``--default``."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: Optional[str] = None,
    ) -> None:
        namespace.targets.append(
            TargetSpec(
                raw_path=_normalize_target_path(SCRIPT_DIR),
                glob_pattern=namespace.current_glob,
            )
        )
        namespace.pending_glob_flag = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas-packer",
        description="Deterministic, dependency-aware texture atlas packer.",
        allow_abbrev=False,
    )

    parser.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="Target directory or .conf file path(s).",
    )

    parser.add_argument(
        "--glob",
        action=_SetGlobAction,
        metavar="PATTERN",
        help=f"Glob pattern for subsequent directory targets (default: {DEFAULT_GLOB!r}).",
    )
    parser.add_argument(
        "--default",
        action=_DefaultTargetAction,
        nargs=0,
        help="Include the script's own directory using the active glob pattern.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass timestamp and manifest cache checks and rebuild all targets.",
    )

    exec_group = parser.add_mutually_exclusive_group()
    exec_group.add_argument(
        "--parallel",
        dest="parallel",
        action="store_true",
        default=True,
        help="Run workers in parallel sized to CPU count (default).",
    )
    exec_group.add_argument(
        "--sequential",
        dest="parallel",
        action="store_false",
        help="Run workers sequentially on a single thread.",
    )

    share_group = parser.add_mutually_exclusive_group()
    share_group.add_argument(
        "--share",
        dest="share",
        action="store_true",
        default=True,
        help="Enable cross-atlas Tier-1 sprite cache sharing (default).",
    )
    share_group.add_argument(
        "--noshare",
        dest="share",
        action="store_false",
        help="Disable cross-atlas Tier-1 sprite cache sharing.",
    )

    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        "--pretty",
        dest="pretty",
        action="store_true",
        default=False,
        help="Format output .json manifests with readable indentation.",
    )
    format_group.add_argument(
        "--compact",
        dest="pretty",
        action="store_false",
        default=False,
        help="Format output .json manifests as compact single-line JSON (default).",
    )

    return parser


def _split_long_option(token: str) -> tuple[str, Optional[str]]:
    if "=" in token:
        opt, val = token.split("=", 1)
        return opt, val
    return token, None


def parse_cli(argv: Optional[Sequence[str]] = None) -> CliOptions:
    """Parse CLI arguments in strict left-to-right appearance order.

    Args:
        argv: Argument vector excluding the program name. Defaults to
            ``sys.argv[1:]`` when ``None``.

    Returns:
        An immutable :class:`CliOptions` instance.
    """
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    namespace = argparse.Namespace()
    parser.parse_args([], namespace=namespace)
    namespace.targets = []
    namespace.current_glob = DEFAULT_GLOB
    namespace.pending_glob_flag = None

    option_actions = parser._option_string_actions
    idx = 0
    end_of_options = False

    while idx < len(raw_args):
        token = raw_args[idx]

        if not end_of_options and token == "--":
            end_of_options = True
            idx += 1
            continue

        if not end_of_options and token.startswith("-") and token != "-":
            opt_name, inline_val = _split_long_option(token)
            action = option_actions.get(opt_name)
            if action is None:
                parser.error(f"unrecognized arguments: {token}")

            if action.nargs == 0:
                if inline_val is not None:
                    parser.error(f"option {opt_name} does not take an argument")
                action(parser, namespace, None, opt_name)
                idx += 1
            else:
                if inline_val is not None:
                    arg_val = inline_val
                    idx += 1
                else:
                    if idx + 1 >= len(raw_args):
                        parser.error(f"option {opt_name} expected one argument")
                    arg_val = raw_args[idx + 1]
                    idx += 2
                action(parser, namespace, arg_val, opt_name)
            continue

        namespace.targets.append(
            TargetSpec(
                raw_path=_normalize_target_path(token),
                glob_pattern=namespace.current_glob,
            )
        )
        namespace.pending_glob_flag = None
        idx += 1

    if namespace.pending_glob_flag is not None:
        parser.error(
            f"{namespace.pending_glob_flag} requires at least one target path after it"
        )

    if not namespace.targets:
        namespace.targets.append(
            TargetSpec(
                raw_path=_normalize_target_path(SCRIPT_DIR),
                glob_pattern=namespace.current_glob,
            )
        )

    return CliOptions(
        targets=tuple(namespace.targets),
        force=namespace.force,
        parallel=namespace.parallel,
        share=namespace.share,
        pretty=namespace.pretty,
    )


if __name__ == "__main__":
    print(parse_cli())
