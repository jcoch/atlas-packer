#!/usr/bin/env python3
import os
import sys
import json
import glob
import fnmatch
import argparse
import threading
import subprocess
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

# ==============================================================================
# DEFAULT PIPELINE SETTINGS (Overridable via CLI flags or top-level constants)
# ==============================================================================
DEFAULT_PARALLEL = False      # True = multi-threaded (--parallel), False = single-threaded (--sequential)
DEFAULT_COMPACT_JSON = False  # True = single-line JSON (--compact), False = indented JSON (--pretty)
DEFAULT_SHARE = True          # True = cross-atlas sharing (--share), False = independent jobs (--noshare)

DEFAULT_WEBP_METHOD = 1       # Lossless WebP encoding method (0-6; 0/1 = fastest, 6 = densest)
DEFAULT_WEBP_QUALITY = 50     # Lossless WebP compression effort (0-100; higher = smaller files, slower)

# Establish base path execution environment
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

# Explicit whitelist of runtime image extensions (avoids working files like .psd)
VALID_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp')


def next_pot(n):
    return 1 << (n - 1).bit_length() if n > 0 else 1


def is_pot(n):
    return n > 0 and (n & (n - 1)) == 0


class ProgressCursor:
    """
    Tracks single-line overwriting without ever moving upward to a previous line.
    When an active 1-up counter line exists, the next step overwrites it in place
    with \\r\\033[K (or space padding) rather than stacking stale counter clutter.
    """

    def __init__(self, enabled=True, stream=None):
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stdout
        self._line_dirty = False
        self._last_len = 0

    def _clear_current_line(self):
        if not self._line_dirty:
            return
        self.stream.write("\r" + (" " * self._last_len) + "\r")
        self._line_dirty = False
        self._last_len = 0

    def print_line(self, text=""):
        """Overwrites any active counter line on the current row and advances to a new line."""
        if not self.enabled:
            return
        self._clear_current_line()
        self.stream.write(text + "\n")
        self.stream.flush()

    def start_inline_banner(self, text):
        """Prints banner text and leaves the cursor at the end of that line."""
        if not self.enabled:
            return
        self._clear_current_line()
        self.stream.write(text)
        self.stream.flush()
        self._line_dirty = True
        self._last_len = len(text)

    def finish_inline_banner(self, suffix):
        """Appends suffix to an active inline banner and advances to a new line."""
        if not self.enabled:
            return
        self.stream.write(suffix + "\n")
        self.stream.flush()
        self._line_dirty = False
        self._last_len = 0

    def update_counter(self, text, advance_first=False):
        """
        Updates the 1-up status/counter line in place.
        If advance_first is True (e.g., cursor is sitting at the end of an inline banner),
        advances to the next line on the first update without moving upward.
        """
        if not self.enabled:
            return
        if advance_first:
            self.stream.write("\n")
            self._line_dirty = False
            self._last_len = 0

        pad = " " * max(0, self._last_len - len(text))
        self.stream.write("\r" + text + pad)
        self.stream.flush()
        self._line_dirty = True
        self._last_len = len(text)

    def commit_counter(self):
        """Promotes the current counter line into a permanent line by advancing the cursor."""
        if not self.enabled:
            return
        if self._line_dirty:
            self.stream.write("\n")
            self.stream.flush()
            self._line_dirty = False
            self._last_len = 0

    def finalize(self):
        """Clears any trailing transient counter line at the end of the pipeline."""
        if not self.enabled:
            return
        if self._line_dirty:
            self._clear_current_line()
            self.stream.flush()


class PureMaxRectsPacker:
    def __init__(self, width, height):
        self.bin_width = width
        self.bin_height = height
        self.free_rectangles = [(0, 0, width, height)]

    def add(self, width, height):
        best_short_side_fit = float('inf')
        best_long_side_fit = float('inf')
        best_rect_idx = -1
        best_x, best_y = 0, 0

        for i, (fx, fy, fw, fh) in enumerate(self.free_rectangles):
            if fw >= width and fh >= height:
                leftover_w = fw - width
                leftover_h = fh - height
                short_side_fit = min(leftover_w, leftover_h)
                long_side_fit = max(leftover_w, leftover_h)

                if short_side_fit < best_short_side_fit or (
                    short_side_fit == best_short_side_fit and long_side_fit < best_long_side_fit
                ):
                    best_short_side_fit = short_side_fit
                    best_long_side_fit = long_side_fit
                    best_rect_idx = i
                    best_x, best_y = fx, fy

        if best_rect_idx == -1:
            return None

        new_free_rects = []
        for fx, fy, fw, fh in self.free_rectangles:
            if (
                best_x >= fx + fw
                or best_x + width <= fx
                or best_y >= fy + fh
                or best_y + height <= fy
            ):
                new_free_rects.append((fx, fy, fw, fh))
                continue

            if fx < best_x < fx + fw:
                new_free_rects.append((fx, fy, best_x - fx, fh))
            if best_x + width < fx + fw:
                new_free_rects.append((best_x + width, fy, fx + fw - (best_x + width), fh))
            if fy < best_y < fy + fh:
                new_free_rects.append((fx, fy, fw, best_y - fy))
            if best_y + height < fy + fh:
                new_free_rects.append((fx, best_y + height, fw, fy + fh - (best_y + height)))

        pruned_free = []
        for i, r1 in enumerate(new_free_rects):
            r1_x, r1_y, r1_w, r1_h = r1
            is_contained = False
            for j, r2 in enumerate(new_free_rects):
                if i != j:
                    r2_x, r2_y, r2_w, r2_h = r2
                    if (
                        r1_x >= r2_x
                        and r1_y >= r2_y
                        and r1_x + r1_w <= r2_x + r2_w
                        and r1_y + r1_h <= r2_y + r2_h
                    ):
                        is_contained = True
                        break
            if not is_contained:
                pruned_free.append(r1)

        self.free_rectangles = pruned_free
        return best_x, best_y


def clear_rgba_rect(buf, buf_w, x, y, w, h):
    """Zeroes out a rectangle (RGBA = 0,0,0,0) in a flat bytearray RGBA buffer."""
    if w <= 0 or h <= 0:
        return
    stride = buf_w * 4
    row_bytes = w * 4
    zero_row = bytes(row_bytes)
    offset = y * stride + x * 4
    for _ in range(h):
        buf[offset : offset + row_bytes] = zero_row
        offset += stride


def blit_rgba_rect(dst_buf, dst_w, dst_x, dst_y, src_buf, src_w, src_x, src_y, w, h):
    """Copies a rectangle of pixels between two flat RGBA buffers row-by-row."""
    if w <= 0 or h <= 0:
        return
    src_stride = src_w * 4
    dst_stride = dst_w * 4
    row_bytes = w * 4
    src_offset = src_y * src_stride + src_x * 4
    dst_offset = dst_y * dst_stride + dst_x * 4

    src_view = memoryview(src_buf)
    for _ in range(h):
        dst_buf[dst_offset : dst_offset + row_bytes] = src_view[
            src_offset : src_offset + row_bytes
        ]
        src_offset += src_stride
        dst_offset += dst_stride


class RawPageStore:
    """
    Holds decoded atlas pages as mutable flat RGBA bytearrays, loaded lazily on demand.
    Supports upfront slot erasure, in-place slot writes, and zero-copy blitting into a full repack.
    """

    def __init__(self, sheet_webp_paths):
        self._webp_paths = sheet_webp_paths
        self._pages = {}  # sheet_idx -> (width, height, bytearray_rgba)

    def get_page(self, sheet_idx):
        entry = self._pages.get(sheet_idx)
        if entry is None:
            with Image.open(self._webp_paths[sheet_idx]) as sheet_img:
                if sheet_img.mode != "RGBA":
                    sheet_img = sheet_img.convert("RGBA")
                entry = (sheet_img.width, sheet_img.height, bytearray(sheet_img.tobytes()))
            self._pages[sheet_idx] = entry
        return entry

    def clear_slot(self, sheet_idx, ox, oy, ow, oh):
        """Erases the old sprite rectangle (RGBA = 0,0,0,0) in the raw page buffer."""
        page_w, _, page_buf = self.get_page(sheet_idx)
        clear_rgba_rect(page_buf, page_w, ox, oy, ow, oh)

    def write_slot(self, sheet_idx, ox, oy, pil_img):
        """Writes a prepared sprite directly into an already-cleared slot in the raw page buffer."""
        if pil_img is None:
            return
        nw, nh = pil_img.size
        if nw <= 0 or nh <= 0:
            return
        page_w, _, page_buf = self.get_page(sheet_idx)
        if pil_img.mode != "RGBA":
            pil_img = pil_img.convert("RGBA")
        blit_rgba_rect(page_buf, page_w, ox, oy, pil_img.tobytes(), nw, 0, 0, nw, nh)

    def blit_to(self, dst_buf, dst_w, dst_x, dst_y, sheet_idx, src_x, src_y, w, h):
        if w <= 0 or h <= 0:
            return
        src_w, _, src_buf = self.get_page(sheet_idx)
        blit_rgba_rect(dst_buf, dst_w, dst_x, dst_y, src_buf, src_w, src_x, src_y, w, h)

    def save_page(self, sheet_idx, webp_path):
        page_w, page_h, page_buf = self._pages[sheet_idx]
        atlas_image = Image.frombytes("RGBA", (page_w, page_h), bytes(page_buf))
        atlas_image.save(
            webp_path,
            "WEBP",
            lossless=True,
            method=DEFAULT_WEBP_METHOD,
            quality=DEFAULT_WEBP_QUALITY,
        )
        atlas_image.close()

    def clear(self):
        self._pages.clear()


# ==============================================================================
# CONFIGURATION DISCOVERY, PARSING & VALIDATION
# ==============================================================================

class AtlasConfig:
    def __init__(
        self,
        conf_path,
        rel_conf_path,
        input_dir,
        output_dir,
        prefix,
        atlas_prefix,
        atlas_size,
        atlas_min,
        input_scale,
        alpha_cutoff,
        padding,
    ):
        self.conf_path = conf_path
        self.rel_conf_path = rel_conf_path
        self.input_dir = input_dir
        self.norm_input_dir = os.path.normcase(os.path.normpath(input_dir))
        self.output_dir = output_dir
        self.prefix = prefix
        self.atlas_prefix = atlas_prefix
        self.output_base = os.path.normpath(os.path.join(output_dir, atlas_prefix))
        self.display_name = atlas_prefix.rstrip("_") or rel_conf_path
        self.atlas_size = atlas_size
        self.atlas_min = atlas_min
        self.input_scale = input_scale
        self.alpha_cutoff = alpha_cutoff
        self.padding = padding


def normalize_prefix(raw_prefix):
    p = raw_prefix.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    if p == ".":
        return ""
    while "//" in p:
        p = p.replace("//", "/")
    return p


def parse_conf_file(conf_path, root_dir):
    """
    Parses a wtxpck.*.conf file.
    Returns (AtlasConfig or None, warnings_list, error_str or None).
    """
    warnings = []
    rel_conf_path = os.path.relpath(conf_path, root_dir).replace("\\", "/")

    if not os.path.exists(conf_path):
        return None, warnings, f"Config missing: {rel_conf_path}"

    try:
        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return None, warnings, f"Failed to read {rel_conf_path}: {e}"

    raw_kv = {}
    for statement in content.split(";"):
        stmt = statement.strip()
        if not stmt:
            continue
        if "=" not in stmt:
            continue
        raw_k, raw_v = stmt.split("=", 1)
        k = raw_k.strip()
        v = raw_v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]

        if any(ch.isspace() for ch in v):
            warnings.append(
                f"Warning [{rel_conf_path}]: Option '{k}' contains internal whitespace ('{v}')."
            )
        raw_kv[k] = v

    missing = [req for req in ("input", "output", "atlas_prefix") if not raw_kv.get(req)]
    if missing:
        return (
            None,
            warnings,
            f"Config {rel_conf_path} missing mandatory option(s): {', '.join(missing)}",
        )

    conf_folder = os.path.dirname(os.path.abspath(conf_path))
    resolved_input = os.path.normpath(os.path.join(conf_folder, raw_kv["input"]))
    resolved_output = os.path.normpath(os.path.join(conf_folder, raw_kv["output"]))
    prefix = normalize_prefix(raw_kv.get("prefix", ""))
    atlas_prefix = raw_kv["atlas_prefix"]

    try:
        raw_size = int(raw_kv.get("atlas_size", "2048"))
        raw_min = int(raw_kv.get("atlas_min", "64"))
        input_scale = float(raw_kv.get("input_scale", "1.0"))
        alpha_cutoff = int(raw_kv.get("alpha_cutoff", "0"))
        padding = int(raw_kv.get("padding", "1"))
    except ValueError as e:
        return None, warnings, f"Invalid numeric parameter in {rel_conf_path}: {e}"

    atlas_size = next_pot(raw_size)
    if not is_pot(raw_size):
        warnings.append(
            f"Warning [{rel_conf_path}]: atlas_size={raw_size} is not a power of 2; rounded up to {atlas_size}."
        )

    atlas_min = next_pot(raw_min)
    if not is_pot(raw_min):
        warnings.append(
            f"Warning [{rel_conf_path}]: atlas_min={raw_min} is not a power of 2; rounded up to {atlas_min}."
        )

    cfg = AtlasConfig(
        conf_path=os.path.abspath(conf_path),
        rel_conf_path=rel_conf_path,
        input_dir=resolved_input,
        output_dir=resolved_output,
        prefix=prefix,
        atlas_prefix=atlas_prefix,
        atlas_size=atlas_size,
        atlas_min=atlas_min,
        input_scale=input_scale,
        alpha_cutoff=alpha_cutoff,
        padding=padding,
    )
    return cfg, warnings, None


def discover_conf_files(root_dir, explicit_configs=None):
    """Finds all wtxpck.*.conf files recursively under root_dir (or uses explicit CLI paths)."""
    if explicit_configs:
        found = []
        for target in explicit_configs:
            abs_target = os.path.abspath(target)
            found.append(abs_target)
        return found

    found = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames.sort()
        for fname in sorted(filenames):
            if fnmatch.fnmatch(fname, "wtxpck.*.conf"):
                found.append(os.path.join(dirpath, fname))
    return found


# ==============================================================================
# EXISTING ATLAS CACHE INSPECTION
# ==============================================================================

def inspect_existing_atlas(conf_path, output_base, expected_scale):
    """
    Validates existing atlas JSON/WEBP files on disk and loads their page & frame manifests.
    Atlas age is the MAX mtime across all files belonging to the atlas.
    Returns (is_valid, atlas_age_mtime, page_manifests, manifest_frames, sheet_webp_paths)
    """
    first_json = f"{output_base}0000.json"
    if not os.path.exists(first_json):
        return False, None, [], {}, []

    sheet_webp_paths = []
    page_manifests = []
    expected_paths = set()
    manifest_frames = {}

    idx = 0
    while True:
        json_path = f"{output_base}{idx:04d}.json"
        webp_path = f"{output_base}{idx:04d}.webp"
        if not os.path.exists(json_path):
            break
        if not os.path.exists(webp_path):
            return False, None, [], {}, []

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False, None, [], {}, []

        if data.get("meta", {}).get("scale") != f"{expected_scale:.6f}":
            return False, None, [], {}, []

        for name, frame_info in data.get("frames", {}).items():
            if name in manifest_frames:
                return False, None, [], {}, []
            manifest_frames[name] = {
                "sheet_idx": idx,
                "frame": frame_info["frame"],
                "spriteSourceSize": frame_info["spriteSourceSize"],
                "sourceSize": frame_info["sourceSize"],
            }

        page_manifests.append(data)
        sheet_webp_paths.append(webp_path)
        expected_paths.update((json_path, webp_path))
        idx += 1

    if not sheet_webp_paths:
        return False, None, [], {}, []

    all_existing_files = glob.glob(f"{glob.escape(output_base)}[0-9][0-9][0-9][0-9].*")
    if set(all_existing_files) != expected_paths:
        return False, None, [], {}, []

    # Age of a preexisting atlas is that of its NEWEST file
    atlas_age_mtime = max(os.path.getmtime(p) for p in expected_paths)

    if os.path.getmtime(conf_path) > atlas_age_mtime:
        return False, None, [], {}, []

    return True, atlas_age_mtime, page_manifests, manifest_frames, sheet_webp_paths


def cleanup_stale_sheets(output_base, valid_paths):
    """Removes leftover numbered atlas sheets from previous builds if sheet count shrank."""
    pattern = f"{glob.escape(output_base)}[0-9][0-9][0-9][0-9].*"
    for filepath in glob.glob(pattern):
        if os.path.normpath(filepath) not in valid_paths:
            try:
                os.remove(filepath)
            except OSError:
                pass


# ==============================================================================
# PREPROCESSOR & PDATA STATE MACHINE
# ==============================================================================

STATE_NOT_FOUND = "NOT_FOUND"
STATE_FOUND_CLEAN = "FOUND_CLEAN"
STATE_FOUND_DIRTY_LOCAL = "FOUND_DIRTY_LOCAL"
STATE_FOUND_DIRTY_CACHE = "FOUND_DIRTY_CACHE"
STATE_NO_MORE = "NO_MORE"


class PDataEntry:
    __slots__ = (
        "asset_key",
        "full_path",
        "kind",           # "CLEAN", "DIRTY_LOCAL", "DIRTY_CACHE"
        "unscaled_img",   # PIL.Image RGBA cropped (unscaled) for DIRTY_LOCAL
        "orig_trim_x",
        "orig_trim_y",
        "orig_w",
        "orig_h",
        "cache_base",     # Normalized output_base of covering child atlas for DIRTY_CACHE
        "observed",
    )

    def __init__(
        self,
        asset_key,
        full_path,
        kind,
        unscaled_img=None,
        orig_trim_x=0,
        orig_trim_y=0,
        orig_w=0,
        orig_h=0,
        cache_base=None,
    ):
        self.asset_key = asset_key
        self.full_path = full_path
        self.kind = kind
        self.unscaled_img = unscaled_img
        self.orig_trim_x = orig_trim_x
        self.orig_trim_y = orig_trim_y
        self.orig_w = orig_w
        self.orig_h = orig_h
        self.cache_base = cache_base
        self.observed = False


class PreprocessorData:
    """
    Authoritative source dataset produced by the job's preprocessor step.
    Builders interact via reset(), query(asset_key), and next_unobserved().
    """

    def __init__(self, entries_in_order):
        self._ordered = entries_in_order
        self._by_key = {e.asset_key: e for e in entries_in_order}
        self._unobserved_iter = None

    @property
    def total_files(self):
        return len(self._ordered)

    @property
    def ordered_keys(self):
        return [e.asset_key for e in self._ordered]

    def reset(self):
        for entry in self._ordered:
            entry.observed = False
        self._unobserved_iter = iter(self._ordered)

    def query(self, asset_key):
        entry = self._by_key.get(asset_key)
        if entry is None:
            return STATE_NOT_FOUND, None
        entry.observed = True
        if entry.kind == "CLEAN":
            return STATE_FOUND_CLEAN, None
        if entry.kind == "DIRTY_CACHE":
            return STATE_FOUND_DIRTY_CACHE, entry.cache_base
        return STATE_FOUND_DIRTY_LOCAL, entry

    def next_unobserved(self):
        if self._unobserved_iter is None:
            self._unobserved_iter = iter(self._ordered)
        for entry in self._unobserved_iter:
            if not entry.observed:
                entry.observed = True
                if entry.kind == "CLEAN":
                    return STATE_FOUND_CLEAN, entry.asset_key, entry
                if entry.kind == "DIRTY_CACHE":
                    return STATE_FOUND_DIRTY_CACHE, entry.asset_key, entry.cache_base
                return STATE_FOUND_DIRTY_LOCAL, entry.asset_key, entry
        return STATE_NO_MORE, None, None

    def close(self):
        for entry in self._ordered:
            if entry.unscaled_img is not None:
                try:
                    entry.unscaled_img.close()
                except Exception:
                    pass
                entry.unscaled_img = None


def decode_and_alpha_clip(full_path, alpha_cutoff):
    """
    Reads a raw image file, converts to RGBA, clips alpha <= alpha_cutoff,
    and returns cropped unscaled PIL image + trim offsets + original dimensions.
    Executed exclusively by the preprocessor.
    """
    with Image.open(full_path) as img:
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        orig_w, orig_h = img.size
        alpha_channel = img.getchannel("A")
        cleaned_alpha = alpha_channel.point(lambda p: p if p > alpha_cutoff else 0)
        bbox = cleaned_alpha.getbbox()

        if bbox:
            orig_trim_x, orig_trim_y, _, _ = bbox
            unscaled_trimmed = img.crop(bbox).copy()
        else:
            orig_trim_x, orig_trim_y = 0, 0
            unscaled_trimmed = None

        return unscaled_trimmed, orig_trim_x, orig_trim_y, orig_w, orig_h


def preprocess_sources(
    target_configs,
    child_caches,
    force=False,
    cursor=None,
):
    """
    Step 1 of a Job:
      - Phase 1: Depth-first directory walk, benchmark timestamp classification, and
                 child-cache matching (no image decoding).
      - Prints the Preprocessing summary banner.
      - Phase 2: Decodes/alpha-clips DIRTY_LOCAL entries with a 1-up counter left on
                 the current line so the next step overwrites it cleanly.
    Returns (pdata, effective_force, progress_lines, warning_lines, error_str or None).
    """
    primary_cfg = target_configs[0]
    input_root = os.path.normpath(primary_cfg.input_dir)
    prefix = primary_cfg.prefix
    alpha_cutoff = primary_cfg.alpha_cutoff

    progress_lines = []
    warning_lines = []

    if not os.path.isdir(input_root):
        return None, force, progress_lines, warning_lines, f"Input directory not found: {input_root}"

    # Group age is the OLDEST member in the group; each member's age is its NEWEST file.
    # If any member atlas is missing/invalid or any target/child .conf was modified, force a clean rebuild.
    effective_force = force
    if effective_force:
        benchmark_mtime = 0.0
    else:
        member_ages = []
        child_conf_mtimes = [
            os.path.getmtime(c_conf) for _, _, _, c_conf in child_caches if os.path.exists(c_conf)
        ]
        newest_child_conf = max(child_conf_mtimes) if child_conf_mtimes else 0.0

        for cfg in target_configs:
            valid, age_mtime, _, _, _ = inspect_existing_atlas(
                cfg.conf_path, cfg.output_base, cfg.input_scale
            )
            if not valid or age_mtime is None or newest_child_conf > age_mtime:
                member_ages = []
                effective_force = True
                break
            member_ages.append(age_mtime)
        benchmark_mtime = min(member_ages) if member_ages else 0.0

    # Map normalized child input directory -> (norm_child_input_dir, norm_child_output_base, child_prefix)
    dir_to_cache = {
        os.path.normcase(os.path.normpath(c_dir)): (
            os.path.normpath(c_dir),
            os.path.normpath(c_base),
            c_prefix,
        )
        for c_dir, c_base, c_prefix, _ in child_caches
    }

    has_expected_children = len(dir_to_cache) > 0
    missing_child_warned = False
    parent_atlas_label = ", ".join(cfg.display_name for cfg in target_configs)
    input_root_prefix_len = len(input_root)

    # ----------------------------------------------------
    # PHASE 1: DISCOVERY & CACHE CLASSIFICATION (NO DECODING)
    # ----------------------------------------------------
    discovered = []  # list of (asset_key, full_path, mtime, active_cache_tuple)

    def walk_dfs(current_dir, inherited_cache):
        nonlocal missing_child_warned
        norm_curr = os.path.normcase(os.path.normpath(current_dir))
        active_cache = dir_to_cache.get(norm_curr, inherited_cache)

        try:
            entries = sorted(os.scandir(current_dir), key=lambda e: e.name)
        except OSError as exc:
            raise RuntimeError(f"Failed scanning directory {current_dir}: {exc}") from exc

        subdirs = []
        files = []
        for entry in entries:
            if entry.is_dir(follow_symlinks=True):
                subdirs.append(entry.path)
            elif entry.is_file(follow_symlinks=True) and entry.name.lower().endswith(VALID_EXTENSIONS):
                files.append(entry)

        for entry in files:
            full_path = os.path.normpath(entry.path)
            rel_path = full_path[input_root_prefix_len:].lstrip("/\\").replace("\\", "/")
            asset_key = f"{prefix}/{rel_path}" if prefix else rel_path

            if has_expected_children and active_cache is None and not missing_child_warned:
                w_msg = f"Warning: Atlas {parent_atlas_label} has missing children."
                warning_lines.append(w_msg)
                if cursor is not None:
                    cursor.print_line(w_msg)
                missing_child_warned = True

            src_mtime = entry.stat().st_mtime
            discovered.append((asset_key, full_path, src_mtime, active_cache))

        for subdir_path in subdirs:
            walk_dfs(subdir_path, active_cache)

    try:
        walk_dfs(input_root, None)
    except RuntimeError as e:
        return None, effective_force, progress_lines, warning_lines, str(e)

    if not discovered:
        return None, effective_force, progress_lines, warning_lines, f"No assets discovered inside: {input_root}"

    pdata_entries = []
    dirty_local_entries = []
    cache_hit_count = 0

    for asset_key, full_path, src_mtime, active_cache in discovered:
        if not effective_force and src_mtime <= benchmark_mtime:
            pdata_entries.append(
                PDataEntry(asset_key=asset_key, full_path=full_path, kind="CLEAN")
            )
        elif active_cache is not None:
            cache_hit_count += 1
            c_input_dir, c_output_base, c_prefix = active_cache
            child_rel = full_path[len(c_input_dir):].lstrip("/\\").replace("\\", "/")
            child_asset_key = f"{c_prefix}/{child_rel}" if c_prefix else child_rel
            pdata_entries.append(
                PDataEntry(
                    asset_key=asset_key,
                    full_path=full_path,
                    kind="DIRTY_CACHE",
                    cache_base=(c_output_base, child_asset_key),
                )
            )
        else:
            entry = PDataEntry(asset_key=asset_key, full_path=full_path, kind="DIRTY_LOCAL")
            pdata_entries.append(entry)
            dirty_local_entries.append(entry)

    raw_count = len(dirty_local_entries)
    new_or_mod_count = raw_count + cache_hit_count

    banner_parts = [
        f"    Preprocessing: {len(discovered)} Files discovered",
        f"{new_or_mod_count} new/modified",
    ]
    if raw_count > 0:
        banner_parts.append(f"{raw_count} raw")
    if cache_hit_count > 0:
        banner_parts.append(f"{cache_hit_count} cached")
    banner_line = ", ".join(banner_parts) + "."

    if cursor is not None:
        cursor.print_line(banner_line)

    # ----------------------------------------------------
    # PHASE 2: DECODE & ALPHA-CLIP RAW FILES (1-UP COUNTER)
    # ----------------------------------------------------
    for idx, entry in enumerate(dirty_local_entries, 1):
        if cursor is not None:
            cursor.update_counter(f"        {idx}/{raw_count}")
        unscaled_img, otx, oty, ow, oh = decode_and_alpha_clip(entry.full_path, alpha_cutoff)
        entry.unscaled_img = unscaled_img
        entry.orig_trim_x = otx
        entry.orig_trim_y = oty
        entry.orig_w = ow
        entry.orig_h = oh

    # Intentionally do NOT commit/advance the 1-up counter line so the first builder overwrites it.
    progress_lines.append(
        f"Preprocessor [{parent_atlas_label}]: {len(discovered)} files "
        f"({new_or_mod_count} new/modified, {raw_count} raw, {cache_hit_count} cached)"
    )

    return PreprocessorData(pdata_entries), effective_force, progress_lines, warning_lines, None


# ==============================================================================
# CHILD CACHE READER (FOR PARENT BUILDERS)
# ==============================================================================

class ChildCacheReader:
    """
    Lazily opens and caches child atlas JSON manifests and RawPageStore instances
    so a Parent builder can copy pre-trimmed, pre-scaled sprites without image decoding.
    """

    def __init__(self):
        self._atlases = {}  # norm_output_base -> (manifest_frames_dict, RawPageStore)

    def _load_child(self, child_output_base):
        norm_base = os.path.normpath(child_output_base)
        cached = self._atlases.get(norm_base)
        if cached is not None:
            return cached

        sheet_webp_paths = []
        manifest_frames = {}
        idx = 0
        while True:
            json_path = f"{norm_base}{idx:04d}.json"
            webp_path = f"{norm_base}{idx:04d}.webp"
            if not os.path.exists(json_path):
                break
            if not os.path.exists(webp_path):
                raise RuntimeError(f"Child atlas page missing: {webp_path}")

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for name, frame_info in data.get("frames", {}).items():
                manifest_frames[name] = {
                    "sheet_idx": idx,
                    "frame": frame_info["frame"],
                    "spriteSourceSize": frame_info["spriteSourceSize"],
                    "sourceSize": frame_info["sourceSize"],
                }

            sheet_webp_paths.append(webp_path)
            idx += 1

        if not sheet_webp_paths:
            raise RuntimeError(f"Child atlas manifest missing at {norm_base}0000.json")

        store = RawPageStore(sheet_webp_paths)
        entry = (manifest_frames, store)
        self._atlases[norm_base] = entry
        return entry

    def extract_sprite(self, cache_target, asset_key):
        child_output_base, child_asset_key = cache_target
        manifest_frames, store = self._load_child(child_output_base)
        info = manifest_frames.get(child_asset_key)
        if info is None:
            raise RuntimeError(
                f"Asset '{child_asset_key}' not found in child cache '{child_output_base}'"
            )

        f = info["frame"]
        sss = info["spriteSourceSize"]
        src_sz = info["sourceSize"]
        w, h = f["w"], f["h"]

        cropped_img = None
        if w > 0 and h > 0:
            src_w, _, src_buf = store.get_page(info["sheet_idx"])
            crop_buf = bytearray(w * h * 4)
            blit_rgba_rect(crop_buf, w, 0, 0, src_buf, src_w, f["x"], f["y"], w, h)
            cropped_img = Image.frombytes("RGBA", (w, h), bytes(crop_buf))

        return {
            "name": asset_key,
            "img": cropped_img,
            "w": w,
            "h": h,
            "scaled_source_w": src_sz["w"],
            "scaled_source_h": src_sz["h"],
            "trim_x": sss["x"],
            "trim_y": sss["y"],
        }

    def close(self):
        for _, store in self._atlases.values():
            store.clear()
        self._atlases.clear()


# ==============================================================================
# BUILDER STEP (CONSTRUCTS EXACTLY ONE ATLAS FROM PDATA)
# ==============================================================================

def scale_pdata_entry(entry, scale):
    """
    Scales a cropped, unscaled PDataEntry for a specific builder's input_scale.
    Never reads raw files or performs alpha-clipping.
    """
    scaled_source_w = int(entry.orig_w * scale)
    scaled_source_h = int(entry.orig_h * scale)

    trimmed_img = None
    tw, th = 0, 0
    trim_x, trim_y = 0, 0

    if entry.unscaled_img is not None:
        if scale != 1.0:
            tw = int(entry.unscaled_img.width * scale)
            th = int(entry.unscaled_img.height * scale)
            if tw > 0 and th > 0:
                trimmed_img = entry.unscaled_img.resize((tw, th), Image.Resampling.LANCZOS)
                trim_x = int(entry.orig_trim_x * scale)
                trim_y = int(entry.orig_trim_y * scale)
            else:
                tw, th = 0, 0
        else:
            trimmed_img = entry.unscaled_img.copy()
            tw, th = trimmed_img.size
            trim_x = entry.orig_trim_x
            trim_y = entry.orig_trim_y

    return {
        "name": entry.asset_key,
        "img": trimmed_img,
        "w": tw,
        "h": th,
        "scaled_source_w": scaled_source_w,
        "scaled_source_h": scaled_source_h,
        "trim_x": trim_x,
        "trim_y": trim_y,
    }


def build_single_atlas(
    cfg,
    pdata,
    compact_json=False,
    force=False,
    cursor=None,
    warned_unobserved_paths=None,
):
    """
    Constructs or updates a single target atlas using authoritative pdata.
    Never reads raw source image files or performs alpha-clipping.
    Returns (progress_message, warning_messages, return_code).
    """
    scale = cfg.input_scale
    max_size = cfg.atlas_size
    min_size = cfg.atlas_min
    padding = cfg.padding
    output_base = cfg.output_base

    warning_msgs = []
    if warned_unobserved_paths is None:
        warned_unobserved_paths = set()

    if cursor is not None:
        cursor.start_inline_banner(f"        Working on atlas: {cfg.display_name}")

    cache_valid = False
    page_manifests = []
    manifest_frames = {}
    sheet_webp_paths = []

    if not force:
        (
            cache_valid,
            _,
            page_manifests,
            manifest_frames,
            sheet_webp_paths,
        ) = inspect_existing_atlas(cfg.conf_path, output_base, scale)

    # ----------------------------------------------------
    # PHASE 1: PDATA RECONCILIATION
    # ----------------------------------------------------
    pdata.reset()

    clean_assets = []
    modified_local = []   # list of (asset_key, PDataEntry)
    modified_cache = []   # list of (asset_key, child_output_base)
    added_local = []      # list of (asset_key, PDataEntry)
    added_cache = []      # list of (asset_key, child_output_base)
    removed_keys = []
    skipped_unobserved_keys = set()
    banner_line_open = True

    if cache_valid:
        for asset_key in sorted(manifest_frames.keys()):
            status, payload = pdata.query(asset_key)
            if status == STATE_NOT_FOUND:
                removed_keys.append(asset_key)
            elif status == STATE_FOUND_CLEAN:
                clean_assets.append(asset_key)
            elif status == STATE_FOUND_DIRTY_LOCAL:
                modified_local.append((asset_key, payload))
            elif status == STATE_FOUND_DIRTY_CACHE:
                modified_cache.append((asset_key, payload))

    while True:
        status, asset_key, payload = pdata.next_unobserved()
        if status == STATE_NO_MORE:
            break
        if status == STATE_FOUND_CLEAN:
            skipped_unobserved_keys.add(asset_key)
            norm_fp = os.path.normcase(os.path.normpath(payload.full_path))
            if norm_fp not in warned_unobserved_paths:
                warned_unobserved_paths.add(norm_fp)
                rel_fp = os.path.relpath(payload.full_path, SCRIPT_DIR).replace("\\", "/")
                w_msg = (
                    f"Warning: Unpacked file '{rel_fp}' has an older timestamp than the atlas "
                    f"and was skipped; 'touch' the file or run with --force to include it."
                )
                warning_msgs.append(w_msg)
                if cursor is not None:
                    if banner_line_open:
                        cursor.finish_inline_banner("")
                        banner_line_open = False
                    cursor.print_line(f"        {w_msg}")
            continue

        if status == STATE_FOUND_DIRTY_CACHE:
            added_cache.append((asset_key, payload))
        else:
            added_local.append((asset_key, payload))

    total_modified = len(modified_local) + len(modified_cache)
    total_added = len(added_local) + len(added_cache)

    if cache_valid and not (removed_keys or total_modified or total_added):
        if cursor is not None:
            if banner_line_open:
                cursor.finish_inline_banner(" - already built")
            else:
                cursor.print_line(f"        Working on atlas: {cfg.display_name} - already built")
        return f"--- {cfg.rel_conf_path}: already built", warning_msgs, 0

    if cursor is not None:
        if banner_line_open:
            cursor.finish_inline_banner("")
        total_reconciled = len(clean_assets) + total_modified + total_added + len(removed_keys)
        cursor.print_line(
            f"            Files: {total_reconciled}, "
            f"(unchanged: {len(clean_assets)}, "
            f"modified: {total_modified}, "
            f"added: {total_added}, "
            f"deleted: {len(removed_keys)})"
        )
    os.makedirs(cfg.output_dir, exist_ok=True)


    # ----------------------------------------------------
    # PHASE 2: PREPARE CHANGED / NEW SPRITES (SCALE OR CHILD COPY)
    # ----------------------------------------------------
    raw_pages = RawPageStore(sheet_webp_paths) if cache_valid else None
    child_reader = ChildCacheReader()
    repack_cache = {}
    dirty_webp_pages = set()
    dirty_json_pages = set()

    try:
        if cache_valid:
            for asset_key in removed_keys:
                m_info = manifest_frames[asset_key]
                s_idx = m_info["sheet_idx"]
                old_f = m_info["frame"]
                raw_pages.clear_slot(s_idx, old_f["x"], old_f["y"], old_f["w"], old_f["h"])
                page_manifests[s_idx]["frames"].pop(asset_key, None)
                dirty_webp_pages.add(s_idx)
                dirty_json_pages.add(s_idx)

            mod_items = (
                [(k, "LOCAL", p) for k, p in modified_local]
                + [(k, "CACHE", p) for k, p in modified_cache]
            )
            add_items = (
                [(k, "LOCAL", p) for k, p in added_local]
                + [(k, "CACHE", p) for k, p in added_cache]
            )

            for asset_key, kind, payload in mod_items:
                m_info = manifest_frames[asset_key]
                s_idx = m_info["sheet_idx"]
                old_f = m_info["frame"]
                ox, oy, ow, oh = old_f["x"], old_f["y"], old_f["w"], old_f["h"]
                raw_pages.clear_slot(s_idx, ox, oy, ow, oh)

                if kind == "CACHE":
                    sprite_data = child_reader.extract_sprite(payload, asset_key)
                else:
                    sprite_data = scale_pdata_entry(payload, scale)

                if sprite_data["w"] <= ow and sprite_data["h"] <= oh:
                    raw_pages.write_slot(s_idx, ox, oy, sprite_data["img"])
                    if sprite_data["img"] is not None:
                        sprite_data["img"].close()

                    updated_frame_entry = {
                        "frame": {"x": ox, "y": oy, "w": sprite_data["w"], "h": sprite_data["h"]},
                        "rotated": False,
                        "trimmed": True,
                        "spriteSourceSize": {
                            "x": sprite_data["trim_x"],
                            "y": sprite_data["trim_y"],
                            "w": sprite_data["w"],
                            "h": sprite_data["h"],
                        },
                        "sourceSize": {
                            "w": sprite_data["scaled_source_w"],
                            "h": sprite_data["scaled_source_h"],
                        },
                    }
                    page_manifests[s_idx]["frames"][asset_key] = updated_frame_entry
                    manifest_frames[asset_key] = {
                        "sheet_idx": s_idx,
                        "frame": updated_frame_entry["frame"],
                        "spriteSourceSize": updated_frame_entry["spriteSourceSize"],
                        "sourceSize": updated_frame_entry["sourceSize"],
                    }
                    dirty_webp_pages.add(s_idx)
                    dirty_json_pages.add(s_idx)
                else:
                    repack_cache[asset_key] = sprite_data

            for asset_key, kind, payload in add_items:
                if kind == "CACHE":
                    sprite_data = child_reader.extract_sprite(payload, asset_key)
                else:
                    sprite_data = scale_pdata_entry(payload, scale)
                repack_cache[asset_key] = sprite_data

            if not repack_cache:
                sorted_webp_pages = sorted(dirty_webp_pages)
                sorted_json_pages = sorted(dirty_json_pages)
                total_write_steps = max(len(sorted_webp_pages), len(sorted_json_pages))

                if cursor is not None:
                    cursor.print_line('            All updates done "in place". No packing required.')
                    cursor.print_line(f"            Writing Atlas: {total_write_steps} Pages")

                write_idx = 0
                for s_idx in sorted_webp_pages:
                    write_idx += 1
                    if cursor is not None:
                        cursor.update_counter(f"                {write_idx}/{total_write_steps}")
                    raw_pages.save_page(s_idx, f"{output_base}{s_idx:04d}.webp")

                json_only_pages = [s for s in sorted_json_pages if s not in dirty_webp_pages]
                for s_idx in sorted_json_pages:
                    if s_idx in json_only_pages:
                        write_idx += 1
                        if cursor is not None:
                            cursor.update_counter(f"                {write_idx}/{total_write_steps}")
                    with open(f"{output_base}{s_idx:04d}.json", "w", encoding="utf-8") as f:
                        if compact_json:
                            json.dump(page_manifests[s_idx], f, separators=(",", ":"))
                        else:
                            json.dump(page_manifests[s_idx], f, indent=2)

                # Leave 1-up counter un-advanced so the next step overwrites it
                return (
                    f"--- {cfg.rel_conf_path}: Atlas patched in-place "
                    f"({len(dirty_webp_pages)} webp, {len(dirty_json_pages)} json updated).",
                    warning_msgs,
                    0,
                )
        else:
            add_items = (
                [(k, "LOCAL", p) for k, p in added_local]
                + [(k, "CACHE", p) for k, p in added_cache]
            )
            for asset_key, kind, payload in add_items:
                if kind == "CACHE":
                    sprite_data = child_reader.extract_sprite(payload, asset_key)
                else:
                    sprite_data = scale_pdata_entry(payload, scale)
                repack_cache[asset_key] = sprite_data

        # Build full sprite descriptor list for bin packing (excluding skipped old-mtime entries)
        images = []
        for asset_key in pdata.ordered_keys:
            if asset_key in skipped_unobserved_keys:
                continue
            if asset_key in repack_cache:
                images.append(repack_cache[asset_key])
            else:
                m_info = manifest_frames[asset_key]
                f = m_info["frame"]
                sss = m_info["spriteSourceSize"]
                src_sz = m_info["sourceSize"]
                images.append({
                    "name": asset_key,
                    "img": None,
                    "sheet_ref": (m_info["sheet_idx"], f["x"], f["y"]),
                    "w": f["w"],
                    "h": f["h"],
                    "scaled_source_w": src_sz["w"],
                    "scaled_source_h": src_sz["h"],
                    "trim_x": sss["x"],
                    "trim_y": sss["y"],
                })

        total_files = len(images)
        images.sort(key=lambda x: max(x["w"], x["h"]) * x["w"] * x["h"], reverse=True)

        # ----------------------------------------------------
        # PHASE 3: BIN PACKING
        # ----------------------------------------------------
        if cursor is not None:
            cursor.print_line(f"            Packing Atlas: {total_files} images")

        pages = []
        for idx, item in enumerate(images):
            if cursor is not None:
                cursor.update_counter(f"                {idx + 1}/{total_files}")

            item_w = item["w"] + padding
            item_h = item["h"] + padding

            if item_w > max_size or item_h > max_size:
                if cursor is not None:
                    cursor.commit_counter()
                return (
                    f"--- {cfg.rel_conf_path}: Error: Asset {item['name']} size "
                    f"({item_w}x{item_h}) exceeds max canvas size {max_size}",
                    warning_msgs,
                    1,
                )

            placed = False
            for page in pages:
                packed = page["packer"].add(item_w, item_h)
                if packed:
                    x, y = packed
                    page["frames"][item["name"]] = (x, y, item)
                    page["final_w"] = max(page["final_w"], x + item["w"])
                    page["final_h"] = max(page["final_h"], y + item["h"])
                    placed = True
                    break
                else:
                    next_w, next_h = page["current_w"], page["current_h"]
                    while (not placed) and (next_w < max_size or next_h < max_size):
                        if next_w <= next_h:
                            next_w = min(max_size, next_w * 2)
                        else:
                            next_h = min(max_size, next_h * 2)

                        grown_packer = PureMaxRectsPacker(next_w, next_h)
                        temp_frames = {}
                        g_final_w, g_final_h = 0, 0

                        repack_success = True
                        for existing_name, (_, _, existing_item) in page["frames"].items():
                            ex_w = existing_item["w"] + padding
                            ex_h = existing_item["h"] + padding
                            p_packed = grown_packer.add(ex_w, ex_h)
                            if p_packed:
                                px, py = p_packed
                                temp_frames[existing_name] = (px, py, existing_item)
                                g_final_w = max(g_final_w, px + existing_item["w"])
                                g_final_h = max(g_final_h, py + existing_item["h"])
                            else:
                                repack_success = False
                                break

                        if repack_success:
                            p_packed = grown_packer.add(item_w, item_h)
                            if p_packed:
                                px, py = p_packed
                                temp_frames[item["name"]] = (px, py, item)
                                g_final_w = max(g_final_w, px + item["w"])
                                g_final_h = max(g_final_h, py + item["h"])

                                page["packer"] = grown_packer
                                page["current_w"] = next_w
                                page["current_h"] = next_h
                                page["final_w"] = g_final_w
                                page["final_h"] = g_final_h
                                page["frames"] = temp_frames
                                placed = True

            if not placed:
                if len(pages) == 0:
                    remaining_items = images[idx:]
                    total_remaining_area = sum(
                        (i["w"] + padding) * (i["h"] + padding) for i in remaining_items
                    )
                    max_page_area = max_size * max_size

                    guaranteed_full_sheets = (
                        total_remaining_area + (max_page_area // 2)
                    ) // max_page_area

                    if guaranteed_full_sheets >= 1:
                        start_w, start_h = max_size, max_size
                    else:
                        start_w = next_pot(
                            max(min_size, max(i["w"] for i in remaining_items) + padding)
                        )
                        start_h = next_pot(
                            max(min_size, max(i["h"] for i in remaining_items) + padding)
                        )

                        while start_w * start_h < total_remaining_area:
                            if start_w > start_h:
                                start_h *= 2
                            else:
                                start_w *= 2
                else:
                    guaranteed_full_sheets = 0
                    start_w, start_h = (
                        next_pot(max(min_size, item_w)),
                        next_pot(max(min_size, item_h)),
                    )

                new_packer = PureMaxRectsPacker(start_w, start_h)
                packed = new_packer.add(item_w, item_h)

                if not packed:
                    if cursor is not None:
                        cursor.commit_counter()
                    return (
                        f"--- {cfg.rel_conf_path}: Error: Logic error packing asset {item['name']}",
                        warning_msgs,
                        1,
                    )

                x, y = packed
                pages.append({
                    "packer": new_packer,
                    "current_w": start_w,
                    "current_h": start_h,
                    "final_w": x + item["w"],
                    "final_h": y + item["h"],
                    "frames": {item["name"]: (x, y, item)},
                })

                if guaranteed_full_sheets > 1:
                    for _ in range(guaranteed_full_sheets - 1):
                        pages.append({
                            "packer": PureMaxRectsPacker(max_size, max_size),
                            "current_w": max_size,
                            "current_h": max_size,
                            "final_w": 0,
                            "final_h": 0,
                            "frames": {},
                        })

        # ----------------------------------------------------
        # PHASE 4: WRITING ATLAS FILES
        # ----------------------------------------------------
        page_sizes = []
        valid_output_paths = set()
        total_pages = len(pages)
        all_json_filenames = [f"{os.path.basename(output_base)}{idx:04d}.json" for idx in range(total_pages)]

        # Overwrite the Packing 1-up counter line with the Writing Atlas banner
        if cursor is not None:
            cursor.print_line(f"            Writing Atlas: {total_pages} Pages")

        for idx, page in enumerate(pages):
            if cursor is not None:
                cursor.update_counter(f"                {idx + 1}/{total_pages}")

            canvas_w = min(max_size, max(min_size, next_pot(page["final_w"])))
            canvas_h = min(max_size, max(min_size, next_pot(page["final_h"])))

            atlas_buf = bytearray(canvas_w * canvas_h * 4)
            json_data = {"frames": {}, "meta": {}}

            for name, (x, y, item) in page["frames"].items():
                if item["img"] is not None:
                    pil_img = item["img"]
                    if pil_img.mode != "RGBA":
                        pil_img = pil_img.convert("RGBA")
                    blit_rgba_rect(
                        atlas_buf,
                        canvas_w,
                        x,
                        y,
                        pil_img.tobytes(),
                        item["w"],
                        0,
                        0,
                        item["w"],
                        item["h"],
                    )
                elif "sheet_ref" in item and item["w"] > 0 and item["h"] > 0:
                    src_sheet_idx, src_x, src_y = item["sheet_ref"]
                    raw_pages.blit_to(
                        atlas_buf,
                        canvas_w,
                        x,
                        y,
                        src_sheet_idx,
                        src_x,
                        src_y,
                        item["w"],
                        item["h"],
                    )

                json_data["frames"][name] = {
                    "frame": {"x": x, "y": y, "w": item["w"], "h": item["h"]},
                    "rotated": False,
                    "trimmed": True,
                    "spriteSourceSize": {
                        "x": item["trim_x"],
                        "y": item["trim_y"],
                        "w": item["w"],
                        "h": item["h"],
                    },
                    "sourceSize": {
                        "w": item["scaled_source_w"],
                        "h": item["scaled_source_h"],
                    },
                }

            webp_out_path = f"{output_base}{idx:04d}.webp"
            json_out_path = f"{output_base}{idx:04d}.json"
            image_filename = os.path.basename(webp_out_path)
            json_filename = all_json_filenames[idx]
            valid_output_paths.update((os.path.normpath(webp_out_path), os.path.normpath(json_out_path)))

            atlas_image = Image.frombytes("RGBA", (canvas_w, canvas_h), bytes(atlas_buf))
            atlas_image.save(
                webp_out_path,
                "WEBP",
                lossless=True,
                method=DEFAULT_WEBP_METHOD,
                quality=DEFAULT_WEBP_QUALITY,
            )
            atlas_image.close()
            related_packs = [f for f in all_json_filenames if f != json_filename]
            json_data["meta"] = {
                "image": image_filename,
                "format": "RGBA8888",
                "size": {"w": canvas_w, "h": canvas_h},
                "scale": f"{scale:.6f}",
            }
            if related_packs:
                json_data["meta"]["related_multi_packs"] = related_packs

            with open(json_out_path, "w", encoding="utf-8") as f:
                if compact_json:
                    json.dump(json_data, f, separators=(",", ":"))
                else:
                    json.dump(json_data, f, indent=2)

            page_sizes.append(f"{canvas_w}x{canvas_h}")

        # Leave the Writing Atlas 1-up counter line un-advanced so the next step overwrites it
        cleanup_stale_sheets(output_base, valid_output_paths)

        size_counts = Counter(page_sizes)
        formatted_sizes = ", ".join(
            f"{count}x({dim})" if count > 1 else f"({dim})"
            for dim, count in size_counts.items()
        )
        page_word = "page" if total_pages == 1 else "pages"
        build_type = "warm-repacked" if cache_valid else "created"
        return (
            f"--- {cfg.rel_conf_path}: Atlas {formatted_sizes} {page_word} {build_type}.",
            warning_msgs,
            0,
        )
    finally:
        if raw_pages is not None:
            raw_pages.clear()
        child_reader.close()
        for item in repack_cache.values():
            if item.get("img") is not None:
                item["img"].close()


# ==============================================================================
# JOB ORCHESTRATION, RELATIONSHIP CLASSIFICATION & 3-TIER SCHEDULER
# ==============================================================================

class BuildJob:
    def __init__(self, tier, target_configs, child_caches):
        self.tier = tier  # 1 = Leaf Child Group, 2 = Parent Group, 3 = Loner Group
        self.target_configs = target_configs
        self.child_caches = child_caches  # list of (norm_child_input_dir, norm_child_output_base)


def execute_job(job, compact_json=False, force=False, cursor=None):
    """
    Executes a complete BuildJob:
      Step 1: Preprocessor step -> produces pdata
      Steps 2..N+1: N Builder steps (1 per target atlas config)
    Returns (return_code, progress_messages, warning_messages).
    """
    pdata, effective_force, progress_msgs, warning_msgs, err = preprocess_sources(
        target_configs=job.target_configs,
        child_caches=job.child_caches,
        force=force,
        cursor=cursor,
    )
    if err is not None:
        if cursor is not None:
            cursor.print_line(f"Error: {err}")
        progress_msgs.append(f"Error: {err}")
        return 1, progress_msgs, warning_msgs

    warned_unobserved_paths = set()
    try:
        for cfg in job.target_configs:
            msg, b_warnings, code = build_single_atlas(
                cfg=cfg,
                pdata=pdata,
                compact_json=compact_json,
                force=effective_force,
                cursor=cursor,
                warned_unobserved_paths=warned_unobserved_paths,
            )
            progress_msgs.append(msg)
            warning_msgs.extend(b_warnings)
            if code != 0:
                return code, progress_msgs, warning_msgs
        return 0, progress_msgs, warning_msgs
    finally:
        pdata.close()


def is_strict_subdir(parent_norm_dir, child_norm_dir):
    if parent_norm_dir == child_norm_dir:
        return False
    try:
        return os.path.commonpath([parent_norm_dir, child_norm_dir]) == parent_norm_dir
    except ValueError:
        return False


def build_execution_plan(configs, share_enabled):
    """
    Classifies configurations into Tier 1 (Leaf Children), Tier 2 (Parents),
    and Tier 3 (Loners), bundles siblings, and emits Cousin warnings.
    Returns (jobs_list, planning_warnings).
    """
    planning_warnings = []

    if not share_enabled:
        jobs = [BuildJob(tier=3, target_configs=[cfg], child_caches=[]) for cfg in configs]
        return jobs, planning_warnings

    n = len(configs)
    parent_to_children = defaultdict(list)
    child_indices = set()
    parent_indices = set()

    for i in range(n):
        p_cfg = configs[i]
        for j in range(n):
            if i == j:
                continue
            c_cfg = configs[j]
            if (
                p_cfg.input_scale == c_cfg.input_scale
                and p_cfg.alpha_cutoff == c_cfg.alpha_cutoff
                and is_strict_subdir(p_cfg.norm_input_dir, c_cfg.norm_input_dir)
            ):
                parent_to_children[i].append(c_cfg)
                parent_indices.add(i)
                child_indices.add(j)

    # Leaf-only rule: any atlas that qualifies as a Parent has its Child status revoked
    leaf_child_indices = child_indices - parent_indices

    # Filter each parent's child list to Leaf Children only
    leaf_child_ids = {id(configs[idx]) for idx in leaf_child_indices}
    for p_idx in list(parent_to_children.keys()):
        parent_to_children[p_idx] = [
            c for c in parent_to_children[p_idx] if id(c) in leaf_child_ids
        ]

    # Cousin detection: identical input_dir but different alpha_cutoff
    by_input_dir = defaultdict(list)
    for idx, cfg in enumerate(configs):
        by_input_dir[cfg.norm_input_dir].append((idx, cfg))

    for _, group in by_input_dir.items():
        if len(group) < 2:
            continue
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                _, cfg_a = group[a]
                _, cfg_b = group[b]
                if cfg_a.alpha_cutoff != cfg_b.alpha_cutoff:
                    planning_warnings.append(
                        f"Warning: Atlases {cfg_a.display_name} and {cfg_b.display_name} "
                        f"are cousins, not siblings."
                    )

    jobs = []

    # 1. Parent status is exclusive: each parent runs alone as its own Tier 2 job
    for idx in sorted(parent_indices):
        p_cfg = configs[idx]
        seen_child_dirs = {}
        for c_cfg in parent_to_children.get(idx, []):
            if c_cfg.norm_input_dir not in seen_child_dirs:
                seen_child_dirs[c_cfg.norm_input_dir] = (
                    c_cfg.input_dir,
                    c_cfg.output_base,
                    c_cfg.prefix,
                    c_cfg.conf_path,
                )
        child_caches = list(seen_child_dirs.values())
        jobs.append(BuildJob(tier=2, target_configs=[p_cfg], child_caches=child_caches))

    # 2. Bundle non-parent atlases sharing identical (norm_input_dir, alpha_cutoff)
    sibling_buckets = defaultdict(list)
    for idx, cfg in enumerate(configs):
        if idx not in parent_indices:
            sibling_buckets[(cfg.norm_input_dir, cfg.alpha_cutoff)].append(idx)

    for _, member_indices in sibling_buckets.items():
        member_configs = [configs[idx] for idx in member_indices]
        is_child_group = any(idx in leaf_child_indices for idx in member_indices)

        if is_child_group:
            jobs.append(BuildJob(tier=1, target_configs=member_configs, child_caches=[]))
        else:
            jobs.append(BuildJob(tier=3, target_configs=member_configs, child_caches=[]))

    return jobs, planning_warnings


def run_scheduler(jobs, parallel, compact_json, force):
    """
    3-Tier Priority Scheduler:
      - Tier 1 (Leaf Children) dispatch first.
      - Tier 3 (Loners) backfill idle worker threads while Tier 1 runs.
      - Tier 2 (Parents) unlock once all Tier 1 jobs finish, taking priority over remaining Loners.
    Returns (overall_return_code, all_job_warnings).
    """
    tier1_q = deque(j for j in jobs if j.tier == 1)
    tier2_q = deque(j for j in jobs if j.tier == 2)
    tier3_q = deque(j for j in jobs if j.tier == 3)

    all_warnings = []
    overall_rc = 0
    halt_requested = False
    total_jobs = len(jobs)

    if not parallel:
        # Sequential execution: Tier 1 -> Tier 2 -> Tier 3
        ordered_jobs = list(tier1_q) + list(tier2_q) + list(tier3_q)
        cursor = ProgressCursor(enabled=True, stream=sys.stderr)
        try:
            for job_idx, job in enumerate(ordered_jobs, 1):
                atlas_list = ", ".join(cfg.display_name for cfg in job.target_configs)
                atlas_count = len(job.target_configs)
                atlas_word = "atlas" if atlas_count == 1 else "atlases"
                cursor.print_line(
                    f"Job [{job_idx}/{total_jobs}] Working on {atlas_count} {atlas_word}: {atlas_list}"
                )

                rc, _, warning_msgs = execute_job(
                    job, compact_json=compact_json, force=force, cursor=cursor
                )
                # Warnings inside execute_job are already printed inline via cursor; collect for summary.
                all_warnings.extend(warning_msgs)

                if rc != 0:
                    overall_rc = rc
                    break
        finally:
            cursor.finalize()
        return overall_rc, all_warnings

    max_workers = max(1, os.cpu_count() or 1)
    lock = threading.Lock()
    done_event = threading.Event()
    active_workers = 0
    active_tier1 = 0
    dispatched_jobs = 0
    completed_jobs = 0

    def pop_next_job():
        if halt_requested:
            return None
        if tier1_q:
            return tier1_q.popleft()
        if active_tier1 == 0 and tier2_q:
            return tier2_q.popleft()
        if tier3_q:
            return tier3_q.popleft()
        return None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        def dispatch_available():
            nonlocal active_workers, active_tier1, dispatched_jobs
            while active_workers < max_workers and not halt_requested:
                next_job = pop_next_job()
                if next_job is None:
                    break
                active_workers += 1
                dispatched_jobs += 1
                job_num = dispatched_jobs
                if next_job.tier == 1:
                    active_tier1 += 1
                atlas_list = ", ".join(cfg.display_name for cfg in next_job.target_configs)
                print(
                    f"Job [{job_num}/{total_jobs}] Dispatched (Tier {next_job.tier}): {atlas_list}",
                    file=sys.stderr,
                )
                fut = executor.submit(
                    execute_job,
                    next_job,
                    compact_json=compact_json,
                    force=force,
                    cursor=None,
                )
                fut.add_done_callback(lambda f, j=next_job, n=job_num: on_job_done(f, j, n))

            if active_workers == 0 and (halt_requested or completed_jobs == total_jobs):
                done_event.set()

        def on_job_done(fut, finished_job, job_num):
            nonlocal active_workers, active_tier1, completed_jobs, overall_rc, halt_requested
            try:
                rc, progress_msgs, warning_msgs = fut.result()
            except Exception as exc:
                rc = 1
                progress_msgs = [
                    f"--- {', '.join(c.rel_conf_path for c in finished_job.target_configs)}: "
                    f"Exception raised: {exc}"
                ]
                warning_msgs = []

            with lock:
                active_workers -= 1
                if finished_job.tier == 1:
                    active_tier1 -= 1
                completed_jobs += 1

                atlas_list = ", ".join(cfg.display_name for cfg in finished_job.target_configs)
                print(f"Job [{completed_jobs}/{total_jobs}] Completed: {atlas_list}", file=sys.stderr)
                for w in warning_msgs:
                    print(w, file=sys.stderr)
                    all_warnings.append(w)
                for p in progress_msgs:
                    print(p, file=sys.stderr)

                if rc != 0:
                    overall_rc = rc
                    halt_requested = True

                dispatch_available()

        with lock:
            if total_jobs == 0:
                done_event.set()
            else:
                dispatch_available()

        done_event.wait()

    return overall_rc, all_warnings


# ==============================================================================
# CLI ARGUMENT PARSING & ENTRYPOINT
# ==============================================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Texture Atlas Compiler Pipeline")

    parser.add_argument(
        "configs",
        nargs="*",
        help="Optional explicit .conf file path(s) to compile instead of recursive discovery.",
    )

    exec_group = parser.add_mutually_exclusive_group()
    exec_group.add_argument(
        "--parallel",
        action="store_true",
        dest="parallel",
        help=f"Allow parallel processing of multiple atlases{' (default)' if DEFAULT_PARALLEL else ''}.",
    )
    exec_group.add_argument(
        "--sequential",
        action="store_false",
        dest="parallel",
        help=f"Only perform processing in a single thread{' (default)' if not DEFAULT_PARALLEL else ''}.",
    )

    json_group = parser.add_mutually_exclusive_group()
    json_group.add_argument(
        "--pretty",
        action="store_false",
        dest="compact",
        help=f"Make the JSON description file pretty and human-readable{' (default)' if not DEFAULT_COMPACT_JSON else ''}.",
    )
    json_group.add_argument(
        "--compact",
        action="store_true",
        dest="compact",
        help=f"Make the JSON description file one single compact line{' (default)' if DEFAULT_COMPACT_JSON else ''}.",
    )

    share_group = parser.add_mutually_exclusive_group()
    share_group.add_argument(
        "--share",
        action="store_true",
        dest="share",
        help=f"Allow cross-atlas sharing of textures{' (default)' if DEFAULT_SHARE else ''}.",
    )
    share_group.add_argument(
        "--noshare",
        action="store_false",
        dest="share",
        help=f"Prohibit cross-atlas sharing of textures{' (default)' if not DEFAULT_SHARE else ''}.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Create the atlases unconditionally, even if they look up to date via timestamp comparison.",
    )

    parser.set_defaults(
        parallel=DEFAULT_PARALLEL,
        compact=DEFAULT_COMPACT_JSON,
        share=DEFAULT_SHARE,
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    all_warnings = []

    conf_paths = discover_conf_files(SCRIPT_DIR, args.configs if args.configs else None)
    if not conf_paths:
        print("No wtxpck.*.conf configuration files found.", file=sys.stderr)
        sys.exit(0)

    configs = []
    parse_errors = []
    for cpath in conf_paths:
        cfg, cfg_warnings, err = parse_conf_file(cpath, SCRIPT_DIR)
        for w in cfg_warnings:
            print(w, file=sys.stderr)
            all_warnings.append(w)
        if err is not None:
            parse_errors.append(err)
        else:
            configs.append(cfg)

    if parse_errors:
        for err in parse_errors:
            print(f"Error: {err}", file=sys.stderr)
        sys.exit("Build Failure! Configuration parsing failed.")

    jobs, planning_warnings = build_execution_plan(configs, share_enabled=args.share)
    for w in planning_warnings:
        print(w, file=sys.stderr)
        all_warnings.append(w)

    mode_str = "Parallel" if args.parallel else "Sequential"
    share_str = "Share Enabled" if args.share else "No-Share"
    print(f"=== Processing {len(configs)} Atlas(es) across {len(jobs)} Job(s) ({mode_str}, {share_str}) ===", file=sys.stderr)

    rc, job_warnings = run_scheduler(
        jobs=jobs,
        parallel=args.parallel,
        compact_json=args.compact,
        force=args.force,
    )
    all_warnings.extend(job_warnings)

    if all_warnings:
        print("\n=== Summary of Warnings ===", file=sys.stderr)
        for w in all_warnings:
            print(w, file=sys.stderr)

    if rc != 0:
        sys.exit("Build Failure! One or more atlas jobs failed.")

    print("=== Atlas Compilations Completed Successfully ===", file=sys.stderr)

