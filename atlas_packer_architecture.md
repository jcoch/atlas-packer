# Texture Atlas Compiler (`atlas_pack`) — Architecture Specification (v13)

## 1. Design Goals & Principles
* **Output Invariance Under Optimization:** Caching, work sharing, and incremental patching must never alter the produced atlases. Every optimization path must yield the exact same sprite geometry and metadata as a clean full build—doing the exact same task without performing redundant work.
* **Suboptimal Path vs. Broken Output Policy:**
  * *Extra Work (`WARNING`):* If an edge case prevents a fast path (e.g., incomplete child coverage, cousin config mismatch, non-power-of-2 rounding, or falling back to local disk decode after a child failure), the compiler performs the extra work safely, emits a warning so the user can optimize future runs, and preserves output correctness.
  * *Broken Output (`ERROR` + Best-Effort Continuation):* If an edge case prevents building an atlas or compromises its completeness (such as an invalid `.conf` file that prevents building the associated atlas, or an unseen `CLEAN` file whose pixels cannot be verified), the compiler logs an error, marks the batch exit code non-zero, and continues executing remaining valid work so all warnings and errors surface in one pass.
* **Batch-First Execution:** Process a closed-world batch of texture atlases together so duplicate disk reads, alpha clipping, and image preprocessing are shared across related atlases.
* **Strict Separation of Responsibilities:** Isolate CLI option collection, target/config discovery, dependency planning, pixel processing, rectangle bin packing, on-disk atlas I/O, job scheduling, and progress reporting into independent layers.
* **Zero-Oversubscription Concurrency:** Parallelize strictly at the **Job** level across a fixed worker pool auto-sized to `os.cpu_count()` (or `1` worker in sequential mode), without exposing manual thread-count flags that invite oversubscription. Multiple `Builder` steps within a single sibling `Job` execute sequentially on that job's assigned worker thread to avoid thread thrashing, nested pools, and lock contention.
* **Deterministic Dependency & Loner-First Scheduling:** Enforce a strict execution barrier between child and parent jobs while prioritizing loner (`UNMARKED`) jobs ahead of children to minimize multi-threaded tail latency.
* **Pluggable Architecture & Lifecycle Hooks:** Decouple single-page bin packing (`BinPacker`), difficulty weights, and external engine sidecar emitters (e.g., Godot `.import` files) via dynamic plugin registries.
* **Static Input Invariant:** Configuration files (`.conf`) and source asset trees are assumed to remain unmodified externally while `atlas_pack` is running.

---

## 2. Core Terminology
* **Batch:** The closed-world collection of `AtlasConfig` targets resolved from the CLI invocation.
* **Job:** A unit of shared CPU work containing **1 `Preprocessor` step** followed by **1 or more sequential `Builder` steps** (a single atlas or a sibling group).
* **Step:** A discrete execution phase inside a `Job` (`Preprocessor` or `Builder`).
* **Preprocessor:** Scans live source files on disk once per `Job` (using either BFS or DFS) strictly via filesystem timestamps and directory hierarchy (never inspecting `.json` manifests), checks modification timestamps (including `.conf` dependencies and missing target atlases) and optional `ChildCache` coverage, emits incomplete child-coverage warnings, decodes/alpha-clips dirty uncached images, physically deletes completely invalidated sibling atlases (`clean_count == 0`), and populates a job-local `PData` store that encapsulates all asset-naming and prefix translation for sibling `Builder` steps.
* **Builder:** Reconciles an individual atlas's existing manifest against `PData` via the unified `query(asset_name=None)` protocol (detecting additions, modifications, deletions, and timestamp-skewed unseen files), scales sprites, loads all existing `.webp` pages into memory when self-caching is viable, stages only added or expanded sprites in a repack buffer while keeping `ExistingAtlas` valid for all surviving and shrunk/in-place-patched sprites, and either patches existing pages in place or unlinks and writes `.webp` + `.json` files.
* **Self-Caching:** Reusing surviving unmodified and in-place-updated sprites directly from an atlas's in-memory `.webp` pages during updates or repacks rather than re-decoding source files.

---

## 3. High-Level Pipeline Flow

```
CLI Arguments
     │
     ▼
1. CliParser (Stage 1 — Pure Option Collection, Zero Filesystem I/O)
     │  Parses CLI flags left-to-right via custom argparse Actions.
     │  Collects execution toggles (--parallel/--sequential, --share/--noshare,
     │  --pretty/--compact, --force, --default) and pairs each positional target
     │  path with the active --glob pattern in appearance order.
     │  Rejects dangling --glob flags that have no subsequent target path.
     ▼
2. TargetResolver (Stage 2 — Filesystem Scan & .conf Validation)
     │  Resolves paired TargetSpec(raw_path, glob_pattern) entries (cwd)
     │  + optional --default (script dir using DEFAULT_GLOB).
     │  Validates .conf syntax (emitting ERROR on invalid/unbuildable .conf)
     │  and power-of-2 bounds (emitting WARNING on rounding).
     │  Produces a deduplicated, closed-world list[AtlasConfig].
     ▼
3. BatchPlanner (Stage 3 — Dependency & Job Graph)
     │  If --noshare: 1 Job per AtlasConfig (UNMARKED).
     │  If --share: resolves Parent/Child & Sibling graphs, demotes dual Parent+Child
     │  atlases to strictly Parent (guaranteeing disjoint child subtrees), groups
     │  Siblings into Jobs, emits cousin warnings, and builds global
     │  (alpha_cutoff, input_scale) -> dict[Path, ChildCacheSpec] maps.
     ▼
4. Dispatcher / Scheduler (Stage 4 — Unified 1-Worker & N-Worker Execution + Watchdog)
     │  Runs Jobs sequentially (1 worker) or in parallel (N = os.cpu_count())
     │  through the exact same dispatcher loop.
     │  Dispatches UNMARKED (loners) first, then CHILD jobs immediately as workers
     │  free up, and holds PARENT jobs behind a strict barrier until all CHILD jobs finish.
     │  Evicts failed CHILD cache entries from global cache maps before Wave 2.
     │  Owns all stdout/stderr output: prints "[#/total] Launched: ..." on dispatch,
     │  buffered warnings/errors on completion, and a 10s quiet-period heartbeat from
     │  lock-free worker_slots tuples.
     │
     └──▶ Inside Each Job (silent worker thread, writes only to worker_slots[worker_id]):
            ├─ Step 1: Preprocessor
            │    ├─ Scans live input trees (BFS or DFS) against cutoff_mtime
            │    ├─ If clean_count == 0 (force, newer .conf, missing atlas, or all dirty):
            │    │    physically deletes all existing sibling atlas .webp/.json files upfront
            │    └─ Produces PData (live source catalog + clipped buffers)
            ├─ Step 2..K: Builder (sequential per sibling atlas)
            │    ├─ Loads all existing .webp pages into ExistingAtlas (if present on disk)
            │    ├─ Reconciles manifest vs. PData (reset(prefix) -> unified query(name / None))
            │    ├─ Applies in-place updates onto ExistingAtlas (self-cache remains valid)
            │    ├─ Stages added or expanded sprites in RepackStaging
            │    ├─ If clean: NO-OP
            │    ├─ If RepackStaging is empty: InPlacePatcher (flush dirty ExistingAtlas pages)
            │    └─ If RepackStaging is non-empty: FullRepacker (unlink old pages, write new)
            └─ Release PData buffers upon Job completion
```

---

## 4. Configuration Constants & Legacy Protection

### User-Editable Execution, Discovery & Output Defaults (`atlas_pack.py`)
Defined near the top of `atlas_pack.py` so users can customize execution, discovery, and output preferences without affecting `.conf` semantics:
* `DEFAULT_GLOB = "wtxpck.*.conf"` (initial active glob for directory discovery and `--default`; overridden per-directory sequence by `--glob`)
* `DEFAULT_PARALLEL = True` (overridden by `--parallel` / `--sequential`)
* `DEFAULT_SHARE = True` (overridden by `--share` / `--noshare`)
* `DEFAULT_PRETTY = False` (overridden by `--pretty` / `--compact` for JSON manifest formatting)
* `DEFAULT_WEBP_METHOD = 1`
* `DEFAULT_WEBP_QUALITY = 50`
* `DEFAULT_SCRUB_ALPHA = True` (zeroes RGB channels on pixels where `alpha <= alpha_cutoff` immediately before WebP encoding)

### Internal Constants & Protected Per-`.conf` Fallbacks
To prevent accidental breakage of legacy `.conf` files, per-atlas `.conf` fallback values are **not** placed in the user-editable defaults section:
* `DEFAULT_PACKER = "maxrects_bssf"` (sole v1 release algorithm; initial pipeline verification uses a temporary single-texture-per-page stub before swapping in `MaxRectsBSSFPacker`)
* `DEFAULT_WEIGHT = "area_max_side"`
* `--force` always defaults to `False` (CLI-only flag).
* **Protected Per-`.conf` Fallbacks:**
  * `alpha_cutoff = 0`
  * `input_scale = 1.0`
  * `atlas_min = 64`
  * `atlas_size = 2048`
  * `padding = 1`
  * `prefix = ""`
* **Hard WebP Lossless Invariant:** `lossless=True` is **hardcoded** inside the WebP encoder and cannot be disabled. Under the "an atlas is a cache" paradigm, lossy encoding would introduce generational compression artifacts when sprites are extracted from child or self-cached atlases.

---

## 5. Layer-by-Layer Responsibilities & Lower-Level Contracts

### Layer 1A: CLI Option Collection (`CliParser`)
* **Zero Filesystem I/O:** `CliParser` collects flags and target arguments strictly from `argv` without touching the filesystem or mutating `sys.argv`.
* **Supported CLI Flags & Defaults (for paired toggles, the last flag on the command line wins):**
  * `--parallel` / `--sequential` (default: `DEFAULT_PARALLEL = True`; worker pool auto-sized to `os.cpu_count()` when parallel, `1` when sequential)
  * `--share` / `--noshare` (default: `DEFAULT_SHARE = True`)
  * `--pretty` / `--compact` (default: `DEFAULT_PRETTY = False`)
  * `--force` (default: `False`)
  * `--default` (default: `False`)
  * `--glob PATTERN` (default: `DEFAULT_GLOB = "wtxpck.*.conf"`)
  * `targets` (positional: zero or more file or directory paths)
* **Stateful Left-to-Right `--glob` Pairing:**
  * Custom `argparse.Action` handlers update `current_glob` whenever `--glob PATTERN` appears and record each positional target as an immutable `TargetSpec(raw_path, glob_pattern)` tuple in exact appearance order.
  * **Dangling `--glob` Rejection:** If `--glob PATTERN` is supplied without at least one subsequent target path appearing after it on the command line, `CliParser` raises a parser error immediately.

### Layer 1B: `.conf` Parser & Target Discovery (`TargetResolver`)
* **Default Root (`SCRIPT_DIR`):** Used automatically when `targets` is empty, or explicitly included alongside positional targets when `--default` is passed. Always uses `DEFAULT_GLOB`.
* **Explicit CLI `TargetSpec` Entries:** Resolved relative to `os.getcwd()` and canonicalized via `Path.resolve()` for deduplication:
  * **Directory:** Recursively searched using that `TargetSpec`'s paired `glob_pattern`.
  * **File:** Loaded directly as an `AtlasConfig` without enforcing filename globs.
* **`.conf` Parsing & Validation Rules:**
  * Semicolon-terminated `key = value;` statements. Leading and trailing whitespace around `key` and `value` are stripped.
  * **Unbuildable Config Errors (`ERROR`):** If a `.conf` file has an error that prevents building the associated atlas—such as internal whitespace inside `value`, malformed syntax, or missing mandatory keys (`input`, `output`, `atlas_prefix`)—`TargetResolver` emits an **`ERROR`**, marks the batch exit code non-zero, and skips the unbuildable atlas for the batch (regardless of `--share` or `--noshare`).
  * Relative `input` and `output` paths are resolved relative to the `.conf` file's parent directory.
  * **Power-of-2 Enforcement (`WARNING`):** `atlas_min` and `atlas_size` must be exact powers of 2. Non-power-of-2 values emit a `WARNING` and are rounded up to the next power of 2 (e.g., `2049 -> 4096`).
  * Unknown parameter names (e.g., `trim=2;`) are silently ignored.
  * Supported input extensions: `('.png', '.jpg', '.jpeg', '.webp')` (case-insensitive).
  * `prefix` is stripped of leading/trailing slashes (`prefix.strip("/")`).

### Layer 2: Relationship & Job Planning (`BatchPlanner`)
* **When `--noshare` is active:** Every `AtlasConfig` becomes its own single-step `Job` with `JobRole.UNMARKED`.
* **When `--share` is active:**
  1. **Parent/Child Detection:** Because atlas inputs are directory subtrees, any two canonical directory trees are either identical, disjoint, or one is a proper subset of the other. Identifies pairs with matching `input_scale` and `alpha_cutoff` where child input directories form a strict subset of parent input directories.
  2. **Role Normalization & Exclusive Parent Status:** Any atlas identified as both a parent and a child has its child status revoked (`Parent` only; never simultaneously a child or sibling). Consequently, no surviving child directory can be an ancestor or subset of another child directory—all child subtrees are guaranteed mathematically disjoint.
  3. **Sibling Grouping:** Remaining non-parent atlases with identical normalized input directories and `alpha_cutoff` values are grouped into sibling sets (siblings may have independent `prefix` and `input_scale` values).
  4. **Global `(alpha_cutoff, input_scale)` Cache Map:** `BatchPlanner` partitions all Tier-1 child caches into a single read-only map keyed by `(alpha_cutoff, input_scale)`:
     ```python
     global_cache_map: dict[tuple[int, float], dict[Path, ChildCacheSpec]]
     # where ChildCacheSpec = (child_input_dir: Path, child_atlas_base: Path, child_prefix: str)
     ```
     Every Tier-2 `PARENT` job receives a reference to the dictionary matching its `(alpha_cutoff, input_scale)` pair.
  5. **Planning-Time Warnings:** Emits `cousin` warnings (`Warning: Atlases <name> and <name> are cousins, not siblings.`) for pairs sharing identical input directories with mismatched `alpha_cutoff`.

### Layer 3: Image Processing (`ImageProcessor`)
Pure pixel operations isolated from filesystem, manifest, and threading concerns:
* **Alpha-Cutoff Thresholding & Tight Bounding-Box Clipping:** Masks pixels where `alpha <= alpha_cutoff` to determine `getbbox()`, then crops to the tight bounding box.
* **`0x0` Transparent Sprites:** Fully transparent sources (`getbbox() is None`) or sprites scaled down to zero width/height (`round(w * scale) == 0` or `round(h * scale) == 0`) are valid first-class `0x0` sprites (`trim_box = (0, 0, 0, 0)`, `image = None`).
* **Resizing:** Scales clipped sprites according to each `Builder`'s `input_scale`.
* **Final-Stage Pre-Encode Alpha Scrubbing:** When `DEFAULT_SCRUB_ALPHA` is `True`, zeroes all RGBA channels (`arr[arr[:, :, 3] <= alpha_cutoff] = 0`) using the atlas's `.conf` `alpha_cutoff` threshold in a **single pass immediately prior to `.webp` encoding**. Using `alpha_cutoff` eliminates faint low-alpha noise pixels (`alpha <= alpha_cutoff`), while running once right before encoding avoids redundant work and prevents resampling halos from re-polluting transparent pixels.

### Layer 4: Preprocessor, Eager Invalid-Atlas Deletion & `PData` Protocol
* **Strictly Filesystem & Timestamp Driven:** `Preprocessor` never parses or inspects `.json` manifests; its change-detection decisions rely solely on directory structure and `mtime` comparisons against `cutoff_mtime`.
* **Cutoff Timestamp Calculation:** Evaluates the oldest target atlas mtime across all `Builder` targets in the `Job` (missing target atlas = `0`). If `--force` is set, any target atlas is missing, or any `Builder`'s `.conf` file mtime is newer than its existing target atlas mtime, `cutoff_mtime = 0`.
* **Traversal-Agnostic Directory Scan (BFS or DFS):**
  * `Preprocessor` scans the job's input directories using either a queue (BFS) or recursion/stack (DFS)—traversal order is not constrained because `BatchPlanner` guarantees all `ChildCacheSpec` subtrees in `cache_map` are disjoint.
  * Each queued/visited directory carries its `active_cache: ChildCacheSpec | None`:
    * When entering a directory `dir_path`, `active_cache = cache_map.get(dir_path, active_cache)`.
    * Every file in `dir_path` inherits that `active_cache` directly (`O(1)` lookup, zero prefix scanning, and no need to restore parent state on exit).
  * Emits an incomplete child coverage warning (`Warning: Atlas <name> has a missing child.`) if `cache_map` is non-empty for the job and any discovered live file lacks an active or valid child cache entry.
* **Eager Physical Deletion of Completely Invalidated Atlases (`clean_count == 0`):**
  * When `Preprocessor` finishes scanning, if `clean_count == 0`—which occurs automatically under `--force`, when any sibling's `.conf` is newer than its existing atlas, when any sibling atlas is missing, or when every source image on disk is newer than `cutoff_mtime`—**all existing sibling atlases in the `Job` are completely invalid and are physically unlinked (`<atlas_prefix>*.webp` and `<atlas_prefix>*.json`) before any `Builder` step executes**.
  * Removing invalid atlases upfront guarantees by construction that a `Builder` cannot accidentally read from an invalidated atlas as a self-cache, and eliminates the need for separate post-production page-shrink deletion logic.
* **`PData` States, Asset-Name Encapsulation & 2-Tuple Cache Reference:**
  * `PData` owns all asset-naming translation between canonical relative paths (relative to the job's `input_dir`) and each sibling `Builder`'s asset namespace. Once initialized for a sibling via `pdata.reset(prefix)`, all `query(...)` calls operate strictly in that `Builder`'s manifest asset names.
  * `CLEAN`: `file_mtime <= cutoff_mtime` (no pixels loaded; Builder self-caches from `ExistingAtlas`).
  * `DIRTY_CACHED`: Dirty file covered by `active_cache`. `Preprocessor` strips `child_input_dir` from `file_path`, prepends `child_prefix` (if non-empty) to form `child_asset_name`, and stores the **2-tuple `(child_atlas_base, child_asset_name)`** in `PData` (keeping `child_input_dir` and `child_prefix` encapsulated inside `Preprocessor`).
  * `DIRTY_LOCAL`: Dirty uncached file decoded and alpha-clipped once via `ImageProcessor`.
* **Unified `PData.query(asset_name=None)` Protocol & Unseen `CLEAN` Quarantine:**
  1. `pdata.reset(prefix)` sets the active `Builder` prefix and resets `seen = False` on all entries.
  2. **Unified `query(asset_name: str | None = None) -> tuple[str, AssetStatus, Any] | None`:**
     * **Named Lookup (`asset_name is not None`):** Used during the `Builder`'s manifest reconciliation pass. Marks the matching entry `seen = True` (if found) and returns `(asset_name, status, payload)`:
       * `(asset_name, UNKNOWN, None)` — deleted or mismatched prefix
       * `(asset_name, CLEAN, None)` — unmodified on disk; self-cache from `ExistingAtlas`
       * `(asset_name, DIRTY_LOCAL, clipped_sprite)` — scale from `PData`
       * `(asset_name, DIRTY_CACHED, (child_atlas_base, child_asset_name))` — fetch from child atlas
     * **Unseen Drain (`asset_name is None`):** Used during the `Builder`'s unseen asset pass after all manifest entries have been queried. Pops/yields the next `seen == False` entry formatted with the active `Builder`'s `prefix` as `(prefixed_asset_name, status, payload)`, or `None` when no unseen entries remain:
       * `DIRTY_LOCAL` / `DIRTY_CACHED`: Added as new sprites.
       * **`CLEAN` (Timestamp-Skewed Unseen File):** File exists on disk with `mtime <= cutoff_mtime` (e.g., renamed/copied preserving old timestamp) so `Preprocessor` loaded no pixels, yet the `Builder` has no manifest entry to self-cache from. `Builder` logs an **Error**, sets the batch failure flag, **omits the asset**, writes out the remaining valid atlas pages, and **immediately evicts its `child_input_dir` from the global `(alpha_cutoff, input_scale)` cache map**. Even though a Tier-2 parent would encounter the same `CLEAN` error when `T_parent >= T_child`, eviction is still mandatory for cases where `T_parent < file_mtime <= T_child` (e.g., a fresh or invalidated parent with `T_parent = 0`, or an older parent built prior to the child): in those cases the file is `DIRTY` relative to the parent, so evicting the incomplete child lets the parent fall back to `DIRTY_LOCAL` disk decoding and produce a complete atlas without requiring `Preprocessor` to inspect child `.json` manifests.

### Layer 5: Atlas I/O, Manifest Schema & Builder Execution
* **Exact JSON Manifest Schema (`<atlas_prefix>0000.json`):**
  * Each page writes `<atlas_prefix>{page:04d}.json` and `<atlas_prefix>{page:04d}.webp`.
  * JSON serialization respects `--pretty` / `--compact` (`DEFAULT_PRETTY`).
  * Frame format:
    ```json
    {
      "frame": {"x": x, "y": y, "w": w, "h": h},
      "rotated": false,
      "trimmed": true,
      "spriteSourceSize": {"x": trim_x, "y": trim_y, "w": w, "h": h},
      "sourceSize": {"w": scaled_source_w, "h": scaled_source_h}
    }
    ```
  * `0x0` sprites are packed normally with `item_w = 0 + padding`, `item_h = 0 + padding`, receiving valid `(x, y)` slots and `w: 0, h: 0` in the manifest.
  * `meta` contains `"image"`, `"format": "RGBA8888"`, `"size": {"w": canvas_w, "h": canvas_h}`, `"scale": f"{scale:.6f}"`, and `"related_multi_packs"` (listing sibling `.json` filenames excluding itself when `total_pages > 1`).
* **Full In-Memory `ExistingAtlas` & Repack Staging Model:**
  * Because completely invalid atlases (`clean_count == 0`) were already physically unlinked by `Preprocessor`, any surviving on-disk atlas has `clean_count > 0` and is eligible for self-caching.
  * When `Builder` starts on an atlas present on disk, it reads the manifest and **loads all existing `.webp` pages into memory up front** (`ExistingAtlas.pages`). Decoding the entire existing atlas once avoids per-sprite page hunting and guarantees that on-disk files are no longer needed for reading if a full repack replaces them.
  * During manifest reconciliation (`pdata.reset(prefix)` followed by `pdata.query(name)` for each manifest entry, then `while (item := pdata.query(None)) is not None:`):
    * **Unchanged (`CLEAN`) sprites:** Remain untouched in `ExistingAtlas`.
    * **Deleted (`UNKNOWN`) sprites:** Their slot rectangles in `ExistingAtlas` are cleared to `(0, 0, 0, 0)` and removed from the manifest.
    * **Modified sprites that fit within their prior slot (`new_w <= old_w` and `new_h <= old_h`):** Replaced in-place directly onto `ExistingAtlas` (clearing the old slot to `(0, 0, 0, 0)` and blitting the updated sprite at `(old_x, old_y)`), marking that page dirty. Because `ExistingAtlas`'s in-memory RGBA buffer now holds the updated sprite pixels, **`ExistingAtlas` remains 100% valid as a self-caching source** even if a subsequent sprite triggers a full repack.
    * **Added sprites OR modified sprites that exceed their prior slot (`new_w > old_w` or `new_h > old_h`):** Stored in a separate **`RepackStaging`** buffer (and if it was an expanded modification, its old slot in `ExistingAtlas` is cleared and removed from `ExistingAtlas`).
  * **Automatic Action Selection:**
    * **No-Op:** Zero pages marked dirty in `ExistingAtlas`, zero deletions, and `RepackStaging` is empty.
    * **`InPlacePatcher`:** `RepackStaging` is empty (and at least one page was modified/deleted). Scrubs zero-alpha pixels if `DEFAULT_SCRUB_ALPHA` is enabled, writes only dirty `.webp` pages and updated `.json` manifests.
    * **`FullRepacker`:** `RepackStaging` is **non-empty** (no inspection of why entries exist in `RepackStaging` is needed—any entry guarantees a repack is required). Collects all surviving/in-place-updated sprites directly from the in-memory `ExistingAtlas` alongside the staged sprites from `RepackStaging`, packs all items via `MultiPagePacker`, scrubs zero-alpha pixels (`alpha <= alpha_cutoff`) if `DEFAULT_SCRUB_ALPHA` is enabled, unlinks any existing `<atlas_prefix>*.webp` and `<atlas_prefix>*.json` files on disk, and writes the new lossless `.webp` + `.json` files.

### Layer 6: Modular Rectangle Packing (`BinPacker` & `MultiPagePacker`)
* **Uniform Single-Bin Interface (`BinPacker`):**
  * `score(w, h) -> PlacementScore | None`: Non-mutating fit query returning a comparable tuple (for MaxRects-BSSF: `(short_side_fit, long_side_fit, y, x)`) or `None`.
  * `insert(item, w, h, score=None) -> PlacedItem | None`: Commits placement and updates internal free rectangles.
  * `items`: Append-only list of placed items preserved in exact insertion order.
* **v1 Scope & Phased Implementation Roadmap:**
  * **Phase 1 (Pipeline Verification Stub):** Begin with a hardcoded single-texture-per-page stub (`SingleTextureTestPacker`) to validate target discovery, relationship planning, job scheduling, and cache draining without bin-packing geometry complexity.
  * **Phase 2 (v1 Release Target):** Replace the stub with **`MaxRectsBSSFPacker`** (Maximal Rectangles Best Short Side Fit, axis-aligned, no rotation) as the sole active v1 packing algorithm.
  * **Future Extension Boundary:** Per-atlas packer selection via `.conf` remains a non-invasive future hook via `PACKER_REGISTRY`, while full-set global optimizers (such as genetic algorithms or simulated annealing) are explicitly out of scope as contrary to fast incremental build throughput.
* **`MultiPagePacker` Orchestration & Page Sizing Algorithm:**
  1. **Difficulty Weight Sorting:** Sorts all items once upfront by descending `area * max(width, height)` (`-(w * h * max(w, h))`), relying on Python's stable Timsort without extra string key tie-breakers.
  2. **Initial Page Pre-Allocation:**
     * Computes guaranteed full pages: `full_pages = (total_area + max_page_area // 2) // max_page_area`.
     * If `full_pages == 0`, allocates one minimum POT page capable of fitting `(max_item_w, max_item_h)` (clamped to `atlas_min`), doubling its smaller dimension until `w * h >= total_area` (up to `atlas_size`).
     * *(Optional refinement)* When `full_pages > 0` and `remainder_area = total_area - full_pages * max_page_area > 0`, allocates one additional fractional POT page sized from `atlas_min` up to `remainder_area`.
  3. **Bin Best Fit Selection:** For each sprite, queries `bin.score(w, h)` across all open bins and inserts into the bin with the minimum `PlacementScore`.
  4. **Transactional POT Bin Doubling on Overflow:**
     * If no open bin fits sprite `S`, selects the smallest open bin `B` strictly smaller than `atlas_size`.
     * Creates a `temp_bin` at the next POT step (doubling `B`'s smaller dimension within `atlas_size`) and attempts to pack `B.items` followed by `S` **in existing append-only insertion order (no re-sorting needed)**.
     * Repeats doubling `temp_bin` up to `atlas_size` until all items fit (replacing `B` with `temp_bin` in `open_bins`) or `atlas_size` fails (leaving `B` untouched with zero rollback state needed).
     * If no bin can grow to fit `S`, allocates a new minimum POT bin for `S` (clamped to `[atlas_min, atlas_size]`).

### Layer 7: Extension Registry, Dispatcher & Silent-Worker Progress Model
* **Modular Plugin Registry (`plugins.py`):**
  * Supports `PACKER_REGISTRY`, `WEIGHT_REGISTRY`, and lifecycle hooks (`on_page_saved`, `on_atlas_built`, `on_batch_complete`—e.g., generating Godot `.import` sidecar files right after a `.webp` page is saved).
  * User `.py` plugins are loaded once in the main process via `importlib` before worker threads spawn, with hook calls wrapped in `try...except` inside `Builder` for worker fault isolation.
* **Unified Dispatcher & Watchdog Loop (`Scheduler`):**
  * Runs the **exact same dispatcher loop** for both `--sequential` (`max_workers = 1`) and `--parallel` (`max_workers = os.cpu_count()`), keeping single-threaded and multi-threaded execution identical and avoiding thread-oversubscription or nested sub-job pools.
  * Dispatches `UNMARKED` (loners) first, then `CHILD` jobs immediately as worker slots open, holds `PARENT` jobs behind a strict barrier until all `CHILD` jobs finish (evicting any failed child from `global_cache_map`), and runs `PARENT` jobs in Wave 2.
  * Frees `Job.pdata` immediately upon job completion so peak RAM stays bounded to active workers.
* **Silent-Worker Output & Lock-Free `worker_slots` Heartbeat:**
  * **Single Console Writer:** Worker threads **never** write to `stdout` or `stderr`. Warnings and errors generated inside a `Job` are collected on the job's result object and emitted cleanly by the dispatcher upon job completion, preventing interleaved output lines.
  * **Batch-Start Orientation & Dispatch Lines:**
    * Before Wave 1 begins, the dispatcher prints one orientation line:
      ```text
      Batch: 162 jobs (110 loners, 38 children, 14 parents) across 4 workers
      ```
    * Whenever a job is dispatched, the dispatcher prints one deterministic line and resets `last_output_time`:
      ```text
      [14/162] Launched: ui_icons, hud_elements
      ```
  * **Zero-Lock Worker Telemetry Slots:**
    * Pre-allocates `worker_slots: list[tuple | None] = [None] * max_workers`, where each worker thread `worker_id` exclusively writes to its own index `worker_slots[worker_id]`.
    * Inside fine-grained loops—such as clipping raw images (`(atlas_name, "clipping", idx, total, phase_start)`), packing rectangles (`(atlas_name, "packing", idx, total, phase_start)`), or encoding finished pages (`(atlas_name, "encoding", page_idx, total_pages, phase_start)`)—the worker simply overwrites `worker_slots[worker_id]` with a raw tuple. Under Python's GIL, single-index tuple assignment is atomic and lock-free, incurring zero string formatting or I/O overhead when jobs complete rapidly.
  * **10-Second Quiet-Period Heartbeat:**
    * The dispatcher waits on active job futures with a 1-second timeout (`wait(..., timeout=1.0, return_when=FIRST_COMPLETED)`).
    * While fast jobs launch and complete within 10 seconds, `worker_slots` updates are never formatted or printed.
    * Only when `now - last_output_time >= 10.0` seconds (such as during a 2-to-10-minute mega-atlas build) does the dispatcher inspect `worker_slots` and emit a single calm status line with rate-based ETA (shown once `idx >= 3` and `elapsed >= 10s`):
      ```text
      [148/162] Working — mega_world: encoding page 19/62 (~1m 48s left)
      ```
