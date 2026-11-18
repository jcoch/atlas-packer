#!/usr/bin/env python3
import argparse
from collections import defaultdict
import glob
import json
import os
import re
import sys

CORE_ATLASES = {"displacement", "game", "models", "models_mobile"}


class CompareSanitizer:
    """Deterministically anonymizes atlas, page, and texture names while preserving hierarchy."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self._leaf_ids = {}
        self._other_atlas_ids = {}
        self._tex_ids = {}
        self.reverse_map = {}

    def _get_or_assign(self, table, key, prefix, pad=3):
        if key not in table:
            table[key] = f"{prefix}_{len(table) + 1:0{pad}d}"
        return table[key]

    def sanitize_atlas_name(self, atlas_name):
        if not self.enabled or atlas_name in CORE_ATLASES:
            return atlas_name

        if atlas_name.startswith("models_mobile_"):
            subname = atlas_name[len("models_mobile_"):]
            leaf_id = self._get_or_assign(self._leaf_ids, subname, "leaf", pad=3)
            sanitized = f"models_mobile_{leaf_id}"
        elif atlas_name.startswith("models_"):
            subname = atlas_name[len("models_"):]
            leaf_id = self._get_or_assign(self._leaf_ids, subname, "leaf", pad=3)
            sanitized = f"models_{leaf_id}"
        else:
            sanitized = self._get_or_assign(self._other_atlas_ids, atlas_name, "atlas", pad=3)

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

    def sanitize_texture_name(self, tex_name):
        if not self.enabled:
            return tex_name
        sanitized = self._get_or_assign(self._tex_ids, tex_name, "sprite", pad=4)
        self.reverse_map[sanitized] = tex_name
        return sanitized


def load_atlas_tree(target_dir):
    """Scans a directory tree and maps each atlas_name to a dict of {texture_name: (w, h)}."""
    search_path = os.path.join(target_dir, "**", "*.json")
    json_files = glob.glob(search_path, recursive=True)

    atlases = defaultdict(dict)
    duplicates = defaultdict(list)

    for file_path in sorted(json_files):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        frames = data.get("frames", {})
        meta_size = data.get("meta", {}).get("size", {})

        if not frames or (meta_size.get("w", 0) * meta_size.get("h", 0)) == 0:
            continue

        page_name = os.path.splitext(os.path.basename(file_path))[0]
        match = re.match(r"^(.+)_\d{4}$", page_name)
        atlas_name = match.group(1) if match else page_name

        for tex_name, info in frames.items():
            frame_data = info.get("frame", {})
            size = (frame_data.get("w", 0), frame_data.get("h", 0))

            if tex_name in atlases[atlas_name]:
                duplicates[atlas_name].append((tex_name, page_name))
            else:
                atlases[atlas_name][tex_name] = size

    return dict(atlases), dict(duplicates)


def compare_atlas_trees(dir_a, dir_b, sanitize=False, sanitize_map_path=None):
    if not os.path.isdir(dir_a):
        print(f"❌ Directory not found: {dir_a}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(dir_b):
        print(f"❌ Directory not found: {dir_b}", file=sys.stderr)
        sys.exit(1)

    sanitizer = CompareSanitizer(enabled=sanitize)

    atlases_a, dupes_a = load_atlas_tree(dir_a)
    atlases_b, dupes_b = load_atlas_tree(dir_b)

    if not atlases_a and not atlases_b:
        print("❌ No valid atlas JSON files found in either directory.", file=sys.stderr)
        sys.exit(1)

    names_a = set(atlases_a.keys())
    names_b = set(atlases_b.keys())

    only_in_a = sorted(names_a - names_b)
    only_in_b = sorted(names_b - names_a)
    common_atlases = sorted(names_a & names_b)

    has_errors = bool(only_in_a or only_in_b or dupes_a or dupes_b)
    atlas_results = []

    for atlas in common_atlases:
        tex_a = atlases_a[atlas]
        tex_b = atlases_b[atlas]

        keys_a = set(tex_a.keys())
        keys_b = set(tex_b.keys())

        missing_in_b = sorted(keys_a - keys_b)
        missing_in_a = sorted(keys_b - keys_a)
        common_keys = sorted(keys_a & keys_b)

        size_mismatches = []
        for key in common_keys:
            if tex_a[key] != tex_b[key]:
                size_mismatches.append((key, tex_a[key], tex_b[key]))

        is_match = not (missing_in_b or missing_in_a or size_mismatches)
        if not is_match:
            has_errors = True

        safe_atlas = sanitizer.sanitize_atlas_name(atlas)
        safe_missing_b = [sanitizer.sanitize_texture_name(k) for k in missing_in_b]
        safe_missing_a = [sanitizer.sanitize_texture_name(k) for k in missing_in_a]
        safe_mismatches = [
            (sanitizer.sanitize_texture_name(k), dim_a, dim_b)
            for k, dim_a, dim_b in size_mismatches
        ]

        atlas_results.append({
            "name": safe_atlas,
            "count_a": len(tex_a),
            "count_b": len(tex_b),
            "is_match": is_match,
            "missing_in_b": safe_missing_b,
            "missing_in_a": safe_missing_a,
            "size_mismatches": safe_mismatches,
        })

    # --- Output Report ---
    header = " 🔍 ATLAS SET COMPARISON REPORT"
    width = max(len(header) + 1, 72)
    print("=" * width)
    print(header)
    print("=" * width)
    disp_a = "<dir_A>" if sanitize else os.path.abspath(dir_a)
    disp_b = "<dir_B>" if sanitize else os.path.abspath(dir_b)
    print(f" Pipeline A : {disp_a}")
    print(f" Pipeline B : {disp_b}")
    print("-" * width)
    print(f" Atlases in A : {len(names_a)}")
    print(f" Atlases in B : {len(names_b)}")
    print(f" Common       : {len(common_atlases)}")

    if only_in_a or only_in_b:
        print("\n⚠️  ATLAS NAME MISMATCHES:")
        for name in sorted(sanitizer.sanitize_atlas_name(n) for n in only_in_a):
            print(f"   - Missing in B: {name}")
        for name in sorted(sanitizer.sanitize_atlas_name(n) for n in only_in_b):
            print(f"   - Missing in A: {name}")

    if dupes_a or dupes_b:
        print("\n⚠️  DUPLICATE TEXTURE ENTRIES DETECTED:")
        for atlas, items in sorted(dupes_a.items()):
            safe_atlas = sanitizer.sanitize_atlas_name(atlas)
            for tex, page in items:
                safe_tex = sanitizer.sanitize_texture_name(tex)
                safe_page = sanitizer.sanitize_page_name(page)
                print(f"   - [Pipeline A] {safe_atlas}: '{safe_tex}' duplicated on {safe_page}")
        for atlas, items in sorted(dupes_b.items()):
            safe_atlas = sanitizer.sanitize_atlas_name(atlas)
            for tex, page in items:
                safe_tex = sanitizer.sanitize_texture_name(tex)
                safe_page = sanitizer.sanitize_page_name(page)
                print(f"   - [Pipeline B] {safe_atlas}: '{safe_tex}' duplicated on {safe_page}")

    if atlas_results:
        atlas_results.sort(key=lambda r: r["name"])
        max_name_len = max(len(r["name"]) for r in atlas_results)
        print("\n" + "=" * width)
        print(" 📦 ATLAS TEXTURE VERIFICATION")
        print("=" * width)

        for r in atlas_results:
            status = "✅ MATCH   " if r["is_match"] else "❌ MISMATCH"
            print(
                f"{status} | {r['name']:<{max_name_len}} | "
                f"Textures: {r['count_a']} (A) vs {r['count_b']} (B)"
            )

            if not r["is_match"]:
                for key in r["missing_in_b"]:
                    print(f"             ↳ Missing in B : {key}")
                for key in r["missing_in_a"]:
                    print(f"             ↳ Missing in A : {key}")
                for key, dim_a, dim_b in r["size_mismatches"]:
                    print(
                        f"             ↳ Size mismatch: {key} "
                        f"({dim_a[0]}x{dim_a[1]} in A vs {dim_b[0]}x{dim_b[1]} in B)"
                    )

    if sanitize and sanitize_map_path:
        with open(sanitize_map_path, "w", encoding="utf-8") as mf:
            if sanitizer.reverse_map:
                col_width = max(len(k) for k in sanitizer.reverse_map) + 4
                for sanitized_key in sorted(sanitizer.reverse_map):
                    actual_val = sanitizer.reverse_map[sanitized_key]
                    mf.write(f"{sanitized_key:<{col_width}}{actual_val}\n")
        print(f"\nSanitization lookup map saved locally to: {sanitize_map_path}", file=sys.stderr)

    print("\n" + "=" * width)
    if has_errors:
        print("❌ RESULT: Differences detected between the two atlas sets.")
        sys.exit(1)
    else:
        print("🎉 RESULT: Both pipelines contain the exact same atlases and textures!")
        sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify that two directory trees contain identical atlas sets and sprite dimensions."
    )
    parser.add_argument("dir_a", help="Path to first atlas directory (Pipeline A)")
    parser.add_argument("dir_b", help="Path to second atlas directory (Pipeline B)")
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="Anonymize directory paths, atlas names, page names, and sprite names in the report",
    )
    parser.add_argument(
        "--sanitize-map",
        default=None,
        help="Optional local path to save the human-readable sanitized-to-original lookup map",
    )
    args = parser.parse_args()

    compare_atlas_trees(
        args.dir_a,
        args.dir_b,
        sanitize=args.sanitize,
        sanitize_map_path=args.sanitize_map,
    )

