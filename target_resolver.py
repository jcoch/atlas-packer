"""Stage 2 target resolution and `.conf` parsing for the texture atlas packer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import fnmatch
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from cli_parser import CliOptions, TargetSpec


DEFAULT_ATLAS_SIZE = 2048
DEFAULT_ATLAS_MIN = 64
DEFAULT_INPUT_SCALE = 1.0
DEFAULT_ALPHA_CUTOFF = 1
DEFAULT_PADDING = 1

# Legacy wtxpck directives parsed for compatibility but intentionally ignored.
IGNORED_DIRECTIVES = frozenset({"trim", "threads"})


class TargetResolutionError(Exception):
    """Raised when a CLI target path cannot be resolved or read."""


class ConfigParseError(Exception):
    """Raised when a `.conf` file is malformed or missing mandatory keys."""


@dataclass(frozen=True)
class CollisionRecord:
    """Describes an `(output_dir, atlas_prefix)` collision across `.conf` files."""

    output_dir: Path
    atlas_prefix: str
    conf_paths: Tuple[Path, ...]

    def format_message(self) -> str:
        files = ", ".join(str(p) for p in self.conf_paths)
        return (
            f"Output collision for prefix {self.atlas_prefix!r} in "
            f"{self.output_dir}: {files} (all colliding configs ejected)"
        )


@dataclass(frozen=True)
class AtlasConfig:
    """Normalized configuration parsed from a single `.conf` file."""

    conf_path: Path
    input_dir: Path
    output_dir: Path
    atlas_prefix: str
    prefix: str = ""
    atlas_size: int = DEFAULT_ATLAS_SIZE
    atlas_min: int = DEFAULT_ATLAS_MIN
    input_scale: float = DEFAULT_INPUT_SCALE
    alpha_cutoff: int = DEFAULT_ALPHA_CUTOFF
    padding: int = DEFAULT_PADDING
    raw_settings: Dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """Human-readable name derived by stripping leading/trailing underscores."""
        return self.atlas_prefix.strip("_") or self.atlas_prefix

    def __repr__(self) -> str:
        return (
            f"AtlasConfig(display_name={self.display_name!r}, "
            f"conf_path={self.conf_path!r}, "
            f"input_dir={self.input_dir!r}, "
            f"output_dir={self.output_dir!r}, "
            f"atlas_prefix={self.atlas_prefix!r}, "
            f"prefix={self.prefix!r}, "
            f"atlas_size={self.atlas_size!r}, "
            f"atlas_min={self.atlas_min!r}, "
            f"input_scale={self.input_scale!r}, "
            f"alpha_cutoff={self.alpha_cutoff!r}, "
            f"padding={self.padding!r})"
        )


@dataclass(frozen=True)
class ResolutionResult:
    """Output of Stage 2 target discovery, parsing, and collision ejection."""

    configs: Tuple[AtlasConfig, ...]
    collisions: Tuple[CollisionRecord, ...] = ()
    warnings: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()


def _next_power_of_two(value: int) -> int:
    """Return the smallest power of two >= value (for positive integers)."""
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def _strip_inline_comment(line: str) -> str:
    """Strip `#` or `//` comments outside single/double quotes."""
    in_single = False
    in_double = False
    i = 0
    length = len(line)
    while i < length:
        ch = line[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "#":
                return line[:i]
            if ch == "/" and i + 1 < length and line[i + 1] == "/":
                return line[:i]
        i += 1
    return line


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_positive_int(key: str, raw: str, conf_path: Path) -> int:
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigParseError(
            f"{conf_path}: '{key}' must be an integer, got {raw!r}"
        ) from exc
    if val <= 0:
        raise ConfigParseError(
            f"{conf_path}: '{key}' must be > 0, got {val}"
        )
    return val


def _parse_non_negative_int(key: str, raw: str, conf_path: Path) -> int:
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigParseError(
            f"{conf_path}: '{key}' must be an integer, got {raw!r}"
        ) from exc
    if val < 0:
        raise ConfigParseError(
            f"{conf_path}: '{key}' must be >= 0, got {val}"
        )
    return val


def _parse_positive_float(key: str, raw: str, conf_path: Path) -> float:
    try:
        val = float(raw)
    except ValueError as exc:
        raise ConfigParseError(
            f"{conf_path}: '{key}' must be a number, got {raw!r}"
        ) from exc
    if val <= 0.0:
        raise ConfigParseError(
            f"{conf_path}: '{key}' must be > 0, got {val}"
        )
    return val


def parse_conf_file(
    conf_path: Path,
    warnings: Optional[List[str]] = None,
    errors: Optional[List[str]] = None,
) -> AtlasConfig:
    """Parse a single `wtxpck`-compatible `.conf` file into an `AtlasConfig`."""
    resolved_conf = Path(os.path.realpath(conf_path))
    if not resolved_conf.is_file():
        raise ConfigParseError(f"Config file not found: {conf_path}")

    try:
        content = resolved_conf.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigParseError(f"Unable to read {resolved_conf}: {exc}") from exc

    directives: Dict[str, str] = {}
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        cleaned = _strip_inline_comment(raw_line).strip()
        if not cleaned:
            continue
        if cleaned.endswith(";"):
            cleaned = cleaned[:-1].strip()
        if not cleaned:
            continue
        if "=" not in cleaned:
            raise ConfigParseError(
                f"{resolved_conf}:{line_no}: expected 'key=value;', got {raw_line.strip()!r}"
            )
        key, raw_val = cleaned.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigParseError(
                f"{resolved_conf}:{line_no}: empty directive key"
            )
        directives[key] = _unquote(raw_val)

    missing = [
        req
        for req in ("input", "output", "atlas_prefix")
        if not directives.get(req, "").strip()
    ]
    if missing:
        raise ConfigParseError(
            f"{resolved_conf}: missing mandatory directive(s): {', '.join(missing)}"
        )

    base_dir = resolved_conf.parent
    raw_input = Path(os.path.expanduser(directives["input"]))
    raw_output = Path(os.path.expanduser(directives["output"]))

    input_dir = Path(
        os.path.realpath(raw_input if raw_input.is_absolute() else base_dir / raw_input)
    )
    output_dir = Path(
        os.path.realpath(raw_output if raw_output.is_absolute() else base_dir / raw_output)
    )
    atlas_prefix = directives["atlas_prefix"]
    display_name = atlas_prefix.strip("_") or atlas_prefix
    prefix = directives.get("prefix", "")

    atlas_size = DEFAULT_ATLAS_SIZE
    if "atlas_size" in directives:
        raw_size = _parse_positive_int(
            "atlas_size", directives["atlas_size"], resolved_conf
        )
        pot_size = _next_power_of_two(raw_size)
        if pot_size != raw_size:
            msg = (
                f"[{display_name}] ({resolved_conf}): atlas_size={raw_size} is not a "
                f"power of two; rounded up to {pot_size} (pages may be larger than expected)"
            )
            if errors is not None:
                errors.append(msg)
        atlas_size = pot_size

    atlas_min = DEFAULT_ATLAS_MIN
    if "atlas_min" in directives:
        raw_min = _parse_positive_int(
            "atlas_min", directives["atlas_min"], resolved_conf
        )
        pot_min = _next_power_of_two(raw_min)
        if pot_min != raw_min:
            msg = (
                f"[{display_name}] ({resolved_conf}): atlas_min={raw_min} is not a "
                f"power of two; rounded up to {pot_min} (reduced small-page optimization)"
            )
            if warnings is not None:
                warnings.append(msg)
        atlas_min = pot_min

    if atlas_min > atlas_size:
        msg = (
            f"[{display_name}] ({resolved_conf}): atlas_min ({atlas_min}) exceeds "
            f"atlas_size ({atlas_size}); clamping atlas_min to {atlas_size}"
        )
        if warnings is not None:
            warnings.append(msg)
        atlas_min = atlas_size

    input_scale = (
        _parse_positive_float("input_scale", directives["input_scale"], resolved_conf)
        if "input_scale" in directives
        else DEFAULT_INPUT_SCALE
    )
    alpha_cutoff = (
        _parse_non_negative_int("alpha_cutoff", directives["alpha_cutoff"], resolved_conf)
        if "alpha_cutoff" in directives
        else DEFAULT_ALPHA_CUTOFF
    )
    padding = (
        _parse_non_negative_int("padding", directives["padding"], resolved_conf)
        if "padding" in directives
        else DEFAULT_PADDING
    )

    known_keys = {
        "input",
        "output",
        "atlas_prefix",
        "prefix",
        "atlas_size",
        "atlas_min",
        "input_scale",
        "alpha_cutoff",
        "padding",
    } | IGNORED_DIRECTIVES

    raw_settings = {
        k: v for k, v in directives.items() if k not in known_keys
    }

    return AtlasConfig(
        conf_path=resolved_conf,
        input_dir=input_dir,
        output_dir=output_dir,
        atlas_prefix=atlas_prefix,
        prefix=prefix,
        atlas_size=atlas_size,
        atlas_min=atlas_min,
        input_scale=input_scale,
        alpha_cutoff=alpha_cutoff,
        padding=padding,
        raw_settings=raw_settings,
    )


def _iter_matching_files_recursive(root_dir: Path, glob_pattern: str) -> Iterable[Path]:
    """Recursively yield sorted files under `root_dir` matching `glob_pattern`."""
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames.sort()
        filenames.sort()
        base = Path(dirpath)
        for name in filenames:
            if fnmatch.fnmatch(name, glob_pattern):
                yield base / name


def resolve_targets(targets: Iterable[TargetSpec]) -> ResolutionResult:
    """Expand CLI `TargetSpec` entries, deduplicate `.conf` files, and parse configs."""
    seen_real_paths: set[Path] = set()
    ordered_conf_paths: List[Path] = []

    for spec in targets:
        candidate = Path(spec.raw_path)
        if not candidate.exists():
            raise TargetResolutionError(f"Target path does not exist: {candidate}")

        if candidate.is_file():
            real_path = Path(os.path.realpath(candidate))
            if real_path not in seen_real_paths:
                seen_real_paths.add(real_path)
                ordered_conf_paths.append(real_path)
        elif candidate.is_dir():
            for match in _iter_matching_files_recursive(candidate, spec.glob_pattern):
                if match.is_file():
                    real_path = Path(os.path.realpath(match))
                    if real_path not in seen_real_paths:
                        seen_real_paths.add(real_path)
                        ordered_conf_paths.append(real_path)
        else:
            raise TargetResolutionError(
                f"Target path is neither a regular file nor directory: {candidate}"
            )

    warnings: List[str] = []
    errors: List[str] = []
    parsed_configs: List[AtlasConfig] = [
        parse_conf_file(path, warnings=warnings, errors=errors)
        for path in ordered_conf_paths
    ]

    by_output_key: Dict[Tuple[Path, str], List[AtlasConfig]] = defaultdict(list)
    for cfg in parsed_configs:
        by_output_key[(cfg.output_dir, cfg.atlas_prefix)].append(cfg)

    collisions: List[CollisionRecord] = []
    colliding_conf_paths: set[Path] = set()
    for (out_dir, prefix), group in by_output_key.items():
        if len(group) > 1:
            record = CollisionRecord(
                output_dir=out_dir,
                atlas_prefix=prefix,
                conf_paths=tuple(c.conf_path for c in group),
            )
            collisions.append(record)
            for c in group:
                colliding_conf_paths.add(c.conf_path)

    valid_configs = tuple(
        cfg for cfg in parsed_configs if cfg.conf_path not in colliding_conf_paths
    )

    return ResolutionResult(
        configs=valid_configs,
        collisions=tuple(collisions),
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def resolve_cli_targets(options: CliOptions) -> ResolutionResult:
    """Convenience wrapper resolving `CliOptions.targets`."""
    return resolve_targets(options.targets)


if __name__ == "__main__":
    from cli_parser import parse_cli

    cli_opts = parse_cli()
    result = resolve_cli_targets(cli_opts)
    for warn in result.warnings:
        print(f"WARNING: {warn}")
    for err in result.errors:
        print(f"ERROR: {err}")
    for col in result.collisions:
        print(f"COLLISION: {col.format_message()}")
    print(f"Resolved {len(result.configs)} valid atlas config(s):")
    for cfg in result.configs:
        print(f"  {cfg}")
