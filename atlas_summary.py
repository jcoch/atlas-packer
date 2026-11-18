#!/usr/bin/env python3
import argparse
from collections import Counter, defaultdict
import json
import os
import re
import struct
import sys

IMAGE_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}
CORE_ATLASES = {"displacement", "game", "models", "models_mobile"}


class AtlasSanitizer:
    """Deterministically anonymizes atlas, page, and file names while preserving hierarchy."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self._leaf_ids = {}
        self._other_atlas_ids = {}
        self._orphan_ids = {}
        self._meta_img_ids = {}
        self.reverse_map = {}

    def _get_or_assign(self, table, key, prefix):
        if key not in table:
            table[key] = f"{prefix}_{len(table) + 1:03d}"
        return table[key]

    def sanitize_atlas_name(self, atlas_name):
        if not self.enabled or atlas_name in CORE_ATLASES:
            return atlas_name

        if atlas_name.startswith("models_mobile_"):
            subname = atlas_name[len("models_mobile_"):]
            leaf_id = self._get_or_assign(self._leaf_ids, subname, "leaf")
            sanitized = f"models_mobile_{leaf_id}"
        elif atlas_name.startswith("models_"):
            subname = atlas_name[len("models_"):]
            leaf_id = self._get_or_assign(self._leaf_ids, subname, "leaf")
            sanitized = f"models_{leaf_id}"
        else:
            sanitized = self._get_or_assign(self._other_atlas_ids, atlas_name, "atlas")

        self.reverse_map[sanitized] = atlas_name
        return sanitized

    def sanitize_page_name(self, page_name):
        if not self.enabled:
            return page_name
        match = re.match(r"^(.+)_(\d{4})$", page_name)
        if match:
            raw_atlas, suffix = match.group(1), match.group(2)
            return f"{self.sanitize_atlas_name(raw_atlas)}_{suffix}"
        return self.sanitize_atlas_name(page_name)

    def sanitize_rel_path(self, rel_path):
        if not self.enabled:
            return rel_path
        ext = os.path.splitext(rel_path)[1].lower()
        alias = self._get_or_assign(self._orphan_ids, rel_path, "orphaned_file")
        sanitized = f"{alias}{ext}"
        self.reverse_map[sanitized] = rel_path
        return sanitized

    def sanitize_meta_image(self, meta_image_name):
        if not self.enabled or not meta_image_name:
            return meta_image_name
        stem, ext = os.path.splitext(meta_image_name)
        match = re.match(r"^(.+)_(\d{4})$", stem)
        if match:
            raw_atlas, suffix = match.group(1), match.group(2)
            return f"{self.sanitize_atlas_name(raw_atlas)}_{suffix}{ext}"
        alias = self._get_or_assign(self._meta_img_ids, meta_image_name, "meta_img")
        sanitized = f"{alias}{ext}"
        self.reverse_map[sanitized] = meta_image_name
        return sanitized


def get_image_dimensions(file_path):
    """Read (width, height) directly from PNG, WebP, or JPEG file headers."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(32)
            if len(header) < 10:
                return None

            # PNG: 8-byte signature + IHDR chunk (width @ 16, height @ 20)
            if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
                w, h = struct.unpack(">II", header[16:24])
                return (w, h) if w > 0 and h > 0 else None

            # WebP: RIFF....WEBP
            if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                chunk_type = header[12:16]
                # Extended WebP (VP8X)
                if chunk_type == b"VP8X" and len(header) >= 30:
                    w = 1 + int.from_bytes(header[24:27], "little")
                    h = 1 + int.from_bytes(header[27:30], "little")
                    return (w, h) if w > 0 and h > 0 else None
                # Lossy WebP (VP8 )
                if chunk_type == b"VP8 " and len(header) >= 30:
                    if header[23:26] == b"\x9d\x01\x2a":
                        w, h = struct.unpack("<HH", header[26:30])
                        return (w & 0x3FFF, h & 0x3FFF)
                # Lossless WebP (VP8L)
                if chunk_type == b"VP8L" and len(header) >= 25:
                    if header[20] == 0x2F:
                        bits = struct.unpack("<I", header[21:25])[0]
                        w = (bits & 0x3FFF) + 1
                        h = ((bits >> 14) & 0x3FFF) + 1
                        return (w, h)

            # JPEG: Scan for SOF0..SOF3 / SOF5..SOF7 / SOF9..SOF11 / SOF13..SOF15
            if header[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker_prefix = f.read(1)
                    if not marker_prefix:
                        break
                    if marker_prefix != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker or marker in (b"\xd8", b"\xd9", b"\x01"):
                        continue
                    if b"\xd0" <= marker <= b"\xd7":
                        continue
                    seg_len_bytes = f.read(2)
                    if len(seg_len_bytes) < 2:
                        break
                    seg_len = struct.unpack(">H", seg_len_bytes)[0]
                    if marker in (
                        b"\xc0", b"\xc1", b"\xc2", b"\xc3",
                        b"\xc5", b"\xc6", b"\xc7",
                        b"\xc9", b"\xca", b"\xcb",
                        b"\xcd", b"\xce", b"\xcf",
                    ):
                        sof = f.read(5)
                        if len(sof) == 5:
                            h, w = struct.unpack(">HH", sof[1:5])
                            return (w, h) if w > 0 and h > 0 else None
                        break
                    f.seek(seg_len - 2, os.SEEK_CUR)
    except OSError:
        return None

    return None


def area_to_pot_bin(area):
    """Return N such that 2^N is the smallest power-of-two area >= area."""
    if area <= 1:
        return 0
    return (area - 1).bit_length()


def profile_atlas_efficiency(
    target_dir,
    warn_threshold=50.0,
    crit_threshold=25.0,
    sanitize=False,
    sanitize_map_path=None,
):
    sanitizer = AtlasSanitizer(enabled=sanitize)
    json_files = []
    image_files_by_stem = defaultdict(list)

    for root, _, files in os.walk(target_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            stem, ext = os.path.splitext(full_path)
            ext_lower = ext.lower()
            if ext_lower == ".json":
                json_files.append(full_path)
            elif ext_lower in IMAGE_EXTENSIONS:
                image_files_by_stem[stem].append(full_path)

    if not json_files:
        print(f"❌ No JSON atlas files found in: {target_dir}", file=sys.stderr)
        return

    pages = []
    atlases = defaultdict(
        lambda: {
            "allocated": 0,
            "used": 0,
            "sizes": Counter(),
            "has_error": False,
        }
    )

    pot_bin_counts = Counter()
    pot_bin_pixels = Counter()

    total_allocated_pixels = 0
    total_used_pixels = 0
    warn_pages = 0
    crit_pages = 0

    meta_size_mismatches = 0
    meta_image_mismatches = 0
    missing_or_bad_images = 0
    oob_frame_pages = 0
    matched_image_paths = set()

    # 1. Gather and group all page data in a single pass
    for file_path in sorted(json_files):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        frames = data.get("frames", {})
        if not frames:
            continue

        stem = os.path.splitext(file_path)[0]
        raw_page_name = os.path.basename(stem)

        # Extract atlas name by stripping the trailing _0000 suffix
        match = re.match(r"^(.+)_\d{4}$", raw_page_name)
        raw_atlas_name = match.group(1) if match else raw_page_name

        atlas_name = sanitizer.sanitize_atlas_name(raw_atlas_name)
        page_name = sanitizer.sanitize_page_name(raw_page_name)

        # Locate matching texture file on disk (same base name, image extension)
        candidate_images = sorted(image_files_by_stem.get(stem, []))
        actual_image_path = candidate_images[0] if candidate_images else None
        for cand in candidate_images:
            matched_image_paths.add(cand)

        meta = data.get("meta", {})
        meta_size = meta.get("size", {})
        meta_w = meta_size.get("w", 0)
        meta_h = meta_size.get("h", 0)
        meta_image_name = meta.get("image")

        error_flags = []
        actual_dims = None

        if not actual_image_path:
            missing_or_bad_images += 1
            error_flags.append("❌ MISSING TEXTURE FILE")
        else:
            actual_dims = get_image_dimensions(actual_image_path)
            if not actual_dims:
                missing_or_bad_images += 1
                error_flags.append("❌ UNREADABLE TEXTURE")

            actual_image_filename = os.path.basename(actual_image_path)
            if meta_image_name and meta_image_name != actual_image_filename:
                meta_image_mismatches += 1
                safe_meta_img = sanitizer.sanitize_meta_image(meta_image_name)
                error_flags.append(f"⚠️ META IMAGE: {safe_meta_img}")

        if not actual_dims:
            atlases[atlas_name]["has_error"] = True
            pages.append({
                "name": page_name,
                "dim": "MISSING",
                "efficiency": None,
                "status": "error",
                "error_flags": error_flags,
            })
            continue

        canvas_w, canvas_h = actual_dims
        canvas_area = canvas_w * canvas_h

        # Check meta.size against actual physical image dimensions
        if meta_w != canvas_w or meta_h != canvas_h:
            meta_size_mismatches += 1
            error_flags.append(f"⚠️ META SIZE: {meta_w}x{meta_h}")

        # Sum active frame pixels & check for out-of-bounds frames against actual texture
        used_pixel_area = 0
        has_oob = False
        for info in frames.values():
            frame_rect = info.get("frame", {})
            fx = frame_rect.get("x", 0)
            fy = frame_rect.get("y", 0)
            fw = frame_rect.get("w", 0)
            fh = frame_rect.get("h", 0)
            used_pixel_area += fw * fh
            if fx < 0 or fy < 0 or (fx + fw) > canvas_w or (fy + fh) > canvas_h:
                has_oob = True

        if has_oob:
            oob_frame_pages += 1
            error_flags.append("❌ FRAME OUT OF BOUNDS")

        efficiency = (used_pixel_area / canvas_area) * 100
        if efficiency < crit_threshold:
            status = "critical"
            crit_pages += 1
        elif efficiency < warn_threshold:
            status = "warning"
            warn_pages += 1
        else:
            status = "ok"

        dim_str = f"{canvas_w}x{canvas_h}"

        # Accumulate global totals
        total_allocated_pixels += canvas_area
        total_used_pixels += used_pixel_area

        pot_bin = area_to_pot_bin(canvas_area)
        pot_bin_counts[pot_bin] += 1
        pot_bin_pixels[pot_bin] += canvas_area

        # Accumulate atlas-level totals
        atlases[atlas_name]["allocated"] += canvas_area
        atlases[atlas_name]["used"] += used_pixel_area
        atlases[atlas_name]["sizes"][dim_str] += 1
        if error_flags:
            atlases[atlas_name]["has_error"] = True

        pages.append({
            "name": page_name,
            "dim": dim_str,
            "efficiency": efficiency,
            "status": status,
            "error_flags": error_flags,
        })

    if not pages:
        print(
            f"❌ No valid texture atlas JSON files found in: {target_dir}",
            file=sys.stderr,
        )
        return

    orphaned_images = []
    for paths in image_files_by_stem.values():
        for p in paths:
            if p not in matched_image_paths:
                rel_p = os.path.relpath(p, target_dir)
                orphaned_images.append(sanitizer.sanitize_rel_path(rel_p))
    orphaned_images.sort()

    # 2. Emit Section 1: Audit Report
    valid_page_count = sum(pot_bin_counts.values())
    total_wasted_pixels = total_allocated_pixels - total_used_pixels
    true_waste_ratio = (
        (total_wasted_pixels / total_allocated_pixels) * 100
        if total_allocated_pixels > 0
        else 0.0
    )

    allocated_mp = total_allocated_pixels / 1_000_000
    used_mp = total_used_pixels / 1_000_000
    wasted_mp = total_wasted_pixels / 1_000_000

    audit_lines = [
        f" Number of Atlases                : {len(atlases)}",
        f" Number of Pages                  : {valid_page_count}",
        f" Underpacked Pages ({crit_threshold:.0f}% - {warn_threshold:.0f}%)    : {warn_pages}",
        f" Severely Underpacked (< {crit_threshold:.0f}%)     : {crit_pages}",
        f" Total Allocated Memory           : {allocated_mp:.2f} MP",
        f" Total Active Texture Area        : {used_mp:.2f} MP",
        f" Total Wasted VRAM Space          : {wasted_mp:.2f} MP",
        f" True Pipeline Waste Ratio        : {true_waste_ratio:.1f}%",
        " -------------------------------------------",
        f" Meta Size Mismatches             : {meta_size_mismatches}",
        f" Meta Image Name Mismatches       : {meta_image_mismatches}",
        f" Out-of-Bounds Frame Pages        : {oob_frame_pages}",
        f" Missing / Unreadable Textures    : {missing_or_bad_images}",
        f" Orphaned Texture Files           : {len(orphaned_images)}",
    ]

    if pot_bin_counts:
        audit_lines.append(" -------------------------------------------")
        audit_lines.append(" Page Area Distribution (2^N Pixels):")
        for bin_exp in sorted(pot_bin_counts.keys(), reverse=True):
            count = pot_bin_counts[bin_exp]
            bin_mp = (2 ** bin_exp) / 1_000_000
            vram_share = (
                (pot_bin_pixels[bin_exp] / total_allocated_pixels) * 100
                if total_allocated_pixels > 0
                else 0.0
            )
            audit_lines.append(
                f"   Bin {bin_exp:<2} (<= {bin_mp:>5.2f} MP) : {count:>4} pages ({vram_share:>5.1f}% VRAM)"
            )

    if orphaned_images:
        audit_lines.append(" -------------------------------------------")
        audit_lines.append(" Orphaned Textures (No Matching JSON):")
        for rel_img in orphaned_images:
            audit_lines.append(f"   - {rel_img}")

    audit_width = max(len(line) for line in audit_lines + [" 📊 AUDIT REPORT "])

    print("=" * audit_width)
    print(" 📊 AUDIT REPORT")
    print("=" * audit_width)
    for line in audit_lines:
        print(line)

    # 3. Emit Section 2: Atlas Summaries (Sorted by Page Count DESC, then Name ASC)
    sorted_atlases = sorted(
        atlases.items(),
        key=lambda item: (-sum(item[1]["sizes"].values()), item[0]),
    )

    atlas_rows = []
    for atlas_name, info in sorted_atlases:
        if info["sizes"]:
            pages_summary = ", ".join(
                f"{count}x({dim})" for dim, count in info["sizes"].items()
            )
        else:
            pages_summary = "N/A"

        if info["allocated"] > 0:
            atlas_eff = f"{(info['used'] / info['allocated']) * 100:>6.2f}%"
        else:
            atlas_eff = "   N/A "

        meta_status = "⚠️ MISMATCH" if info["has_error"] else "OK"
        atlas_rows.append((atlas_name, pages_summary, atlas_eff, meta_status))

    max_atlas_name = max(len(r[0]) for r in atlas_rows)
    max_pages_summary = max(len(r[1]) for r in atlas_rows)
    max_meta_status = max(len(r[3]) for r in atlas_rows)

    formatted_atlas_lines = [
        f"{name:<{max_atlas_name}} | Pages: {p_sum:<{max_pages_summary}} | Utilization: {eff} | Meta: {m_stat:<{max_meta_status}}"
        for name, p_sum, eff, m_stat in atlas_rows
    ]
    atlas_width = max(
        len(line) for line in formatted_atlas_lines + [" 📦 ATLAS SUMMARIES "]
    )

    print("\n" + "=" * atlas_width)
    print(" 📦 ATLAS SUMMARIES")
    print("=" * atlas_width)
    for line in formatted_atlas_lines:
        print(line)

    # 4. Emit Section 3: Page Details (Alphabetical Reference)
    pages.sort(key=lambda p: p["name"])

    max_page_name = max(len(p["name"]) for p in pages)
    max_dim = max(len(p["dim"]) for p in pages)

    formatted_page_lines = []
    for p in pages:
        flags = []
        if p["status"] == "error":
            prefix = "❌ "
        elif p["status"] == "critical":
            prefix = "❌ "
            flags.append("[❌ SEVERELY UNDERPACKED]")
        elif p["status"] == "warning":
            prefix = "⚠️  "
            flags.append("[⚠️ UNDERPACKED]")
        else:
            prefix = "⚠️  " if p["error_flags"] else "✅ "

        for err in p["error_flags"]:
            flags.append(f"[{err}]")

        flag_str = f"  {' '.join(flags)}" if flags else ""
        eff_str = f"{p['efficiency']:>6.2f}%" if p["efficiency"] is not None else "   N/A "

        line = (
            f"{prefix}{p['name']:<{max_page_name}} | "
            f"Size: {p['dim']:<{max_dim}} | "
            f"Utilization: {eff_str}{flag_str}"
        )
        formatted_page_lines.append(line)

    page_header = (
        f" 📦 PAGE DETAILS (Warning: < {warn_threshold:.0f}%, Critical: < {crit_threshold:.0f}%)"
    )
    page_width = max(
        len(line) for line in formatted_page_lines + [page_header + " "]
    ) + 1

    print("\n" + "=" * page_width)
    print(page_header)
    print("=" * page_width)
    for line in formatted_page_lines:
        print(line)

    if sanitize and sanitize_map_path:
        with open(sanitize_map_path, "w", encoding="utf-8") as mf:
            if sanitizer.reverse_map:
                col_width = max(len(k) for k in sanitizer.reverse_map) + 4
                for sanitized_key in sorted(sanitizer.reverse_map):
                    actual_val = sanitizer.reverse_map[sanitized_key]
                    mf.write(f"{sanitized_key:<{col_width}}{actual_val}\n")
        print(f"\nSanitization lookup map saved locally to: {sanitize_map_path}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Profile texture atlas packing efficiency and metadata integrity."
    )
    parser.add_argument("target_dir", help="Path to the texture atlas directory")
    parser.add_argument(
        "--warn", type=float, default=50.0, help="Warning utilization threshold %% (default: 50.0)"
    )
    parser.add_argument(
        "--crit", type=float, default=25.0, help="Critical utilization threshold %% (default: 25.0)"
    )
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="Anonymize atlas, page, and file names in the output report",
    )
    parser.add_argument(
        "--sanitize-map",
        default=None,
        help="Optional local path to save the human-readable sanitized-to-original lookup map",
    )
    args = parser.parse_args()

    profile_atlas_efficiency(
        args.target_dir,
        warn_threshold=args.warn,
        crit_threshold=args.crit,
        sanitize=args.sanitize,
        sanitize_map_path=args.sanitize_map,
    )


