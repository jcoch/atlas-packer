# Texture Atlas Packer — Architecture & Design Specification (v14)

## 1. Core Architectural Goals

The texture atlas packer builds and incrementally updates 2D texture atlases and their accompanying JSON manifests from `.conf` definitions. The architecture is driven by three primary constraints:

1. **Eliminate Redundant Work:** Never re-decode, re-alpha-clip, or re-blit sprites whose source files and atlas configurations have not changed since the last build.
2. **Strict Separation of Responsibilities:**
   - **Startup (`CliParser` & `TargetResolver`):** Collect CLI options cleanly without touching disk, then scan the filesystem and parse `.conf` files to establish the exact build graph.
   - **Preprocessor:** Strictly filesystem- and timestamp-driven. Walks asset directories, compares source `mtime` against the atlas's last build timestamp (`T_build`), and performs disk decoding and alpha-clipping only for dirty sprites. The `Preprocessor` **never** inspects `.json` manifests.
   - **Builder:** Owns `.json` manifest inspection, sprite cache draining, bin-packing layout, and final PNG/JSON output generation.
3. **Predictable Concurrency & Safe Defaults:** Avoid user-configurable thread-count foot-guns (`--workers` is intentionally omitted). Parallel execution automatically sizes worker pools to `os.cpu_count()`.

---

## 2. Four-Stage Top-to-Bottom Execution Pipeline

Execution proceeds linearly through four distinct stages:

```text
[1. CliParser] ──> [2. TargetResolver] ──> [3. BatchPlanner] ──> [4. Scheduler]
 (Pure CLI)         (FS Scan & .conf)       (DAG & Tiering)       (Preproc/Build)
```

### Stage 1: Collect CLI Options (`CliParser`)
- **Zero Filesystem I/O:** `CliParser` uses standard Python `argparse` with custom left-to-right `Action` handlers (`_SetGlobAction` and `_AppendTargetAction`) and never touches the filesystem or mutates `sys.argv`.
- **Stateful Left-to-Right `--glob` Pairing:** As `argparse` scans tokens left-to-right, `--glob PATTERN` updates `current_glob` (defaulting to `"wtxpck.*.conf"`). Every positional `TARGET` argument is captured as an immutable `TargetSpec(raw_path, glob_pattern)` paired with the glob active at that point in the command line.
- **Dangling `--glob` Validation:** If `--glob PATTERN` appears without at least one subsequent positional `TARGET` argument, `CliParser` immediately fails with a parser usage error. (Whether that subsequent target is a directory or a file is verified in Stage 2.)
- **Implicit `--default` Rule:** When no positional `TARGET` paths are provided (`targets == ()`), `use_default` automatically resolves to `True`. When one or more positional `TARGET` paths are specified, `use_default` is `False` unless `--default` is explicitly passed on the command line.
- **Supported v1 CLI Flags:**
  - `--glob PATTERN` — Stateful glob pattern for subsequent directory targets (default: `"wtxpck.*.conf"`).
  - `--default` — Scan the script's default target root using `trailing_glob` (automatically enabled when no positional targets are supplied).
  - `--force` — Bypass timestamp and manifest caches and rebuild all targeted atlases from scratch.
  - `--parallel` / `--sequential` — Run worker pools sized to `os.cpu_count()` (default: `--parallel`) vs. single-threaded execution.
  - `--share` / `--noshare` — Enable (default: `--share`) or disable cross-atlas Tier-1 sprite cache sharing (`global_cache_map`).
  - `--pretty` / `--compact` — Output human-readable indented JSON vs. compact single-line JSON (default: `--compact`).

### Stage 2: Scan & Collect Atlas Configs (`TargetResolver`)
- Iterates through `CliOptions.targets` in order:
  - If `raw_path` is a **file**, it is accepted directly as an atlas configuration target (ignoring its paired `glob_pattern`).
  - If `raw_path` is a **directory**, it is scanned using its paired `glob_pattern` to collect matching `.conf` files.
  - If `raw_path` does not exist, a clear error is reported.
- If `CliOptions.use_default` is `True`, the default root directory is also scanned using `CliOptions.trailing_glob`.
- Deduplicates resolved `.conf` paths (by canonical path) and parses each `.conf` into an `AtlasConfig` object.

### Stage 3: Discover Relationships (`BatchPlanner`)
- Inspects parsed `AtlasConfig` sources to determine whether an atlas draws from raw image directories or references other atlases as inputs:
  - **Tier-1 (Leaf Atlases):** Depend only on raw source image files on disk.
  - **Tier-2 (Composite Atlases):** Reference one or more Tier-1 (or lower-tier) atlases in addition to or instead of raw image directories.
- Constructs a Directed Acyclic Graph (DAG), detects dependency cycles early, and produces a topologically ordered execution plan.

### Stage 4: Schedule Jobs (`Scheduler`)
- Executes Tier-1 jobs before Tier-2 jobs so completed Tier-1 outputs can populate `global_cache_map` (when `--share` is active).
- Dispatches each atlas through its `Preprocessor` phase followed by its `Builder` phase, either via a `ThreadPoolExecutor(max_workers=os.cpu_count())` (`--parallel`) or inline on the main thread (`--sequential`).

---

## 3. Incremental Build & Cache Eviction Protocol

### Timestamp-Driven `Preprocessor`
For each target atlas, the `Preprocessor` determines `T_build` from the existing output `.webp` and `.json` artifacts (`0` if missing or if `--force` is set, or if the `.conf` `mtime` is newer than the outputs).

Every candidate source sprite is classified solely by timestamp and `global_cache_map` membership:
1. **`DIRTY_CACHED`:** Sprite belongs to a valid shared Tier-1 atlas currently registered in `global_cache_map`. Local disk decoding is skipped; pixels will be cropped from the cached Tier-1 atlas during the `Builder` phase.
2. **`DIRTY_LOCAL`:** `file_mtime > T_build` (and not `DIRTY_CACHED`). The `Preprocessor` reads the image from disk, performs alpha-clipping, and stages the trimmed bitmap in memory.
3. **`CLEAN`:** `file_mtime <= T_build` (and not `DIRTY_CACHED`). The `Preprocessor` does not decode the file from disk; the `Builder` expects to copy its pixels from the existing atlas `.webp` using coordinates in the existing `.json` manifest.

### `Builder` Manifest Drain & Child Eviction
During the `Builder` phase:
- All `DIRTY_LOCAL` and `DIRTY_CACHED` sprites are gathered alongside existing `.json` manifest entries for `CLEAN` sprites.
- **Unseen `CLEAN` Detection:** While draining sprites present in the filesystem scan that were not newly decoded (`CLEAN`), the `Builder` verifies that every `CLEAN` sprite actually exists in the existing `.json` manifest.
- **Why Incomplete Child Atlases Must Be Evicted:** If a child atlas is missing a `CLEAN` sprite from its `.json` manifest (for example, due to a manual file touch with an older timestamp or a partial prior artifact), that child atlas cannot be trusted as a complete cache source. The `Builder` **must evict that child atlas from `global_cache_map`**:
  - Even though a parent atlas with `T_parent >= T_child` would also see `file_mtime <= T_child <= T_parent` (`CLEAN`) and fail anyway, a parent atlas with `T_parent < file_mtime <= T_child` (e.g., an unbuilt parent with `T_parent = 0` or an older parent build) legitimately sees the file as `DIRTY_LOCAL` and **can** decode it from disk to build a valid parent atlas.
  - Keeping an incomplete child in `global_cache_map` would cause that parent's `Preprocessor` to classify the sprite as `DIRTY_CACHED` and skip disk decoding, causing an avoidable parent build failure. Evicting the child forces the parent's `Preprocessor` to evaluate raw timestamps accurately without ever inspecting `.json` manifests.

---

## 4. Packing Engine & Phased Implementation Roadmap

### v1 Packing Algorithm: MaxRects Best Short Side Fit (BSSF)
- **Sole v1 Algorithm:** `MaxRectsBSSFPacker` (Maximal Rectangles, Best Short Side Fit) is the single production packing algorithm for v1.
- **Development Scaffolding (`SingleTextureTestPacker`):** Initial pipeline bring-up uses a trivial one-sprite-per-page stub packer to verify dependency discovery, scheduling, and cache draining in isolation before plugging in `MaxRectsBSSFPacker`.
- **Non-Invasive Future Extension:** Because `Builder` interacts with the packer strictly through a narrow layout interface, per-atlas algorithm selection via `.conf` can be added in a future release without modifying `Preprocessor`, `global_cache_map`, or `Scheduler`.
- **Explicit Out-of-Scope Algorithms:** Global multi-pass optimizers requiring full-set evaluation (such as genetic algorithms or simulated annealing) are explicitly excluded as antithetical to the packer's low-latency incremental build goals.
