from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageOps
from skimage import measure

from .runtime import ensure_windows_cairo_runtime


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
SVG_EXTS = {".svg"}
EPS = 1e-9
SMALL_COMPONENT_AREA_RATIO = 0.0002
TINY_PATH_AREA_RATIO = 0.0002

CSV_COLUMNS = [
    "row_type",
    "sample_name",
    "binary_file",
    "svg_file",
    "foreground_area_ratio",
    "component_count",
    "small_component_count",
    "small_component_ratio",
    "svg_binary_precision",
    "ssim_binary_svg",
    "num_paths",
    "num_path_commands",
    "tiny_path_count",
    "tiny_path_ratio",
    "notes",
]

TEXT_COLUMNS = {"row_type", "sample_name", "binary_file", "svg_file", "notes"}
PATH_TOKEN_RE = re.compile(r"[AaCcHhLlMmQqSsTtVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
COMMANDS = set("AaCcHhLlMmQqSsTtVvZz")
COMMAND_ARITY = {
    "M": 2,
    "L": 2,
    "H": 1,
    "V": 1,
    "C": 6,
    "S": 4,
    "Q": 4,
    "T": 2,
    "A": 7,
    "Z": 0,
}


@dataclass
class PathStats:
    command_count: int = 0
    tiny_path_count: int = 0
    subpath_count: int = 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate paired binary images and SVG files by filename stem.")
    parser.add_argument("--binary-root", required=True, type=Path, help="Folder containing binary result images.")
    parser.add_argument("--svg-root", required=True, type=Path, help="Folder containing SVG result files.")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output folder.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows, unmatched_binary, unmatched_svg = evaluate_folder_pair(args.binary_root, args.svg_root)
    write_unmatched_report(args.out_dir, unmatched_binary, unmatched_svg)
    if not rows:
        print("ERROR: no matched samples found. Files are matched by filename stem.", file=sys.stderr)
        return 2

    average = average_row(rows)
    average["row_type"] = "AVERAGE"
    average["sample_name"] = "AVERAGE"
    csv_path = args.out_dir / "evaluation.csv"
    write_csv(csv_path, [average, *rows])

    print(f"Wrote: {csv_path.resolve()}")
    print(f"Matched samples: {len(rows)}")
    if unmatched_binary or unmatched_svg:
        print(f"See unmatched file list: {(args.out_dir / 'unmatched_files.txt').resolve()}")
    return 0


def evaluate_folder_pair(binary_root: Path, svg_root: Path) -> tuple[list[dict[str, object]], list[Path], list[Path]]:
    binary_root = binary_root.resolve()
    svg_root = svg_root.resolve()
    if not binary_root.is_dir():
        raise FileNotFoundError(f"binary root is not a folder: {binary_root}")
    if not svg_root.is_dir():
        raise FileNotFoundError(f"svg root is not a folder: {svg_root}")

    binary_map = build_unique_stem_map(iter_files(binary_root, IMAGE_EXTS), "binary")
    svg_map = build_unique_stem_map(iter_files(svg_root, SVG_EXTS), "svg")
    matched_names = sorted(set(binary_map) & set(svg_map))
    unmatched_binary = [binary_map[name] for name in sorted(set(binary_map) - set(svg_map))]
    unmatched_svg = [svg_map[name] for name in sorted(set(svg_map) - set(binary_map))]

    rows = []
    total = len(matched_names)
    print_progress(0, total)
    for completed, sample_name in enumerate(matched_names, start=1):
        rows.append(evaluate_one(sample_name, binary_map[sample_name], svg_map[sample_name]))
        print_progress(completed, total)
    return rows, unmatched_binary, unmatched_svg


def iter_files(root: Path, exts: set[str]) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in exts)


def build_unique_stem_map(paths: Iterable[Path], label: str) -> dict[str, Path]:
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in paths:
        by_stem[path.stem].append(path)
    duplicates = {stem: found for stem, found in by_stem.items() if len(found) > 1}
    if duplicates:
        lines = [f"duplicate {label} sample names found; filenames must be unique by stem:"]
        for stem, found in sorted(duplicates.items()):
            lines.append(f"  {stem}:")
            lines.extend(f"    {path}" for path in found)
        raise ValueError("\n".join(lines))
    return {stem: found[0] for stem, found in by_stem.items()}


def print_progress(completed: int, total: int, width: int = 30) -> None:
    if total <= 0:
        return
    ratio = min(1.0, max(0.0, completed / float(total)))
    filled = int(round(width * ratio))
    bar = "#" * filled + "-" * (width - filled)
    message = f"\rProgress [{bar}] {completed}/{total} completed ({ratio * 100:5.1f}%)"
    print(message, end="" if completed < total else "\n", flush=True)


def evaluate_one(sample_name: str, binary_path: Path, svg_path: Path) -> dict[str, object]:
    notes: list[str] = []
    binary_mask = load_dark_foreground_mask(binary_path)
    row: dict[str, object] = {
        "row_type": "SAMPLE",
        "sample_name": sample_name,
        "binary_file": str(binary_path),
        "svg_file": str(svg_path),
    }
    row.update(binary_shape_metrics(binary_mask))

    try:
        svg_mask = render_svg_dark_foreground_mask(svg_path, binary_mask.shape)
        row["svg_binary_precision"] = svg_binary_precision(binary_mask, svg_mask)
        row["ssim_binary_svg"] = global_binary_ssim(binary_mask, svg_mask)
    except Exception as exc:
        notes.append(f"svg_render_failed:{type(exc).__name__}:{exc}")
        row["svg_binary_precision"] = math.nan
        row["ssim_binary_svg"] = math.nan

    try:
        row.update(svg_complexity_metrics(svg_path, binary_mask.shape))
    except Exception as exc:
        notes.append(f"svg_parse_failed:{type(exc).__name__}:{exc}")
        row["num_paths"] = math.nan
        row["num_path_commands"] = math.nan
        row["tiny_path_count"] = math.nan
        row["tiny_path_ratio"] = math.nan

    row["notes"] = "; ".join(notes)
    for column in CSV_COLUMNS:
        row.setdefault(column, "")
    return row


def load_dark_foreground_mask(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        gray = np.asarray(image.convert("L"), dtype=np.uint8)
    return gray < 128


def render_svg_dark_foreground_mask(path: Path, shape: tuple[int, int]) -> np.ndarray:
    ensure_windows_cairo_runtime()
    import cairosvg

    height, width = shape
    png_bytes = cairosvg.svg2png(url=str(path), output_width=width, output_height=height, background_color="white")
    with Image.open(io.BytesIO(png_bytes)) as image:
        rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    gray = np.asarray(background.convert("L"), dtype=np.uint8)
    return gray < 128


def binary_shape_metrics(mask: np.ndarray) -> dict[str, object]:
    mask = mask.astype(bool)
    total_area = int(mask.size)
    foreground_area = int(mask.sum())
    labeled = measure.label(mask, connectivity=2)
    regions = measure.regionprops(labeled)
    min_area = max(4, int(round(SMALL_COMPONENT_AREA_RATIO * max(1, total_area))))
    small_regions = [region for region in regions if int(region.area) < min_area]
    small_area = int(sum(int(region.area) for region in small_regions))
    return {
        "foreground_area_ratio": float(foreground_area / (total_area + EPS)),
        "component_count": int(len(regions)),
        "small_component_count": int(len(small_regions)),
        "small_component_ratio": float(small_area / (foreground_area + EPS)),
    }


def svg_binary_precision(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference = reference.astype(bool)
    candidate = candidate.astype(bool)
    tp = float((reference & candidate).sum())
    fp = float((~reference & candidate).sum())
    return float(tp / (tp + fp + EPS))


def global_binary_ssim(a: np.ndarray, b: np.ndarray) -> float:
    x = a.astype(np.float64)
    y = b.astype(np.float64)
    mux = float(x.mean())
    muy = float(y.mean())
    vx = float(((x - mux) ** 2).mean())
    vy = float(((y - muy) ** 2).mean())
    cov = float(((x - mux) * (y - muy)).mean())
    c1 = 0.01**2
    c2 = 0.03**2
    return float(((2 * mux * muy + c1) * (2 * cov + c2)) / ((mux * mux + muy * muy + c1) * (vx + vy + c2) + EPS))


def svg_complexity_metrics(path: Path, shape: tuple[int, int]) -> dict[str, object]:
    root = ET.parse(path).getroot()
    path_elements = [el for el in root.iter() if local_name(el.tag) == "path"]
    image_area = max(1, shape[0] * shape[1])
    tiny_area_px = max(1.0, TINY_PATH_AREA_RATIO * image_area)
    stats = PathStats()
    for element in path_elements:
        path_stats = parse_path_d_stats(element.attrib.get("d", ""), tiny_area_px)
        stats.command_count += path_stats.command_count
        stats.tiny_path_count += path_stats.tiny_path_count
        stats.subpath_count += path_stats.subpath_count
    denominator = max(1, stats.subpath_count if stats.subpath_count else len(path_elements))
    return {
        "num_paths": int(len(path_elements)),
        "num_path_commands": int(stats.command_count),
        "tiny_path_count": int(stats.tiny_path_count),
        "tiny_path_ratio": float(stats.tiny_path_count / denominator),
    }


def parse_path_d_stats(d: str, tiny_area_px: float) -> PathStats:
    tokens = PATH_TOKEN_RE.findall(d)
    stats = PathStats()
    if not tokens:
        return stats

    subpath_coords: list[tuple[float, float]] = []

    def finish_subpath() -> None:
        if not subpath_coords:
            return
        stats.subpath_count += 1
        xs = [xy[0] for xy in subpath_coords]
        ys = [xy[1] for xy in subpath_coords]
        bbox_area = max(0.0, max(xs) - min(xs)) * max(0.0, max(ys) - min(ys))
        if bbox_area <= tiny_area_px:
            stats.tiny_path_count += 1
        subpath_coords.clear()

    i = 0
    cmd = ""
    current = (0.0, 0.0)
    subpath_start = (0.0, 0.0)
    previous_cmd = ""

    while i < len(tokens):
        token = tokens[i]
        if token in COMMANDS:
            cmd = token
            i += 1
        elif not cmd:
            break

        upper = cmd.upper()
        if upper == "Z":
            stats.command_count += 1
            finish_subpath()
            current = subpath_start
            previous_cmd = cmd
            continue

        arity = COMMAND_ARITY[upper]
        first_moveto = upper == "M"
        consumed_group = False
        while i < len(tokens) and tokens[i] not in COMMANDS:
            if count_numeric_tokens(tokens, i) < arity:
                i = len(tokens)
                break
            nums = [float(tokens[i + j]) for j in range(arity)]
            i += arity
            consumed_group = True
            effective_cmd = cmd
            if first_moveto:
                first_moveto = False
            elif upper == "M":
                effective_cmd = "l" if cmd.islower() else "L"
            if effective_cmd.upper() == "M":
                finish_subpath()
            current, subpath_start, coords = apply_path_group(effective_cmd, nums, current, subpath_start)
            subpath_coords.extend(coords)
            stats.command_count += 1
            previous_cmd = effective_cmd

        if not consumed_group and i < len(tokens) and tokens[i] not in COMMANDS:
            i += 1
        if previous_cmd and previous_cmd.upper() == "M":
            cmd = "l" if previous_cmd.islower() else "L"

    finish_subpath()
    return stats


def count_numeric_tokens(tokens: list[str], start: int) -> int:
    count = 0
    for token in tokens[start:]:
        if token in COMMANDS:
            break
        count += 1
    return count


def apply_path_group(
    cmd: str,
    nums: list[float],
    current: tuple[float, float],
    subpath_start: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float], list[tuple[float, float]]]:
    upper = cmd.upper()
    relative = cmd.islower()
    cx, cy = current
    coords: list[tuple[float, float]] = []

    def point(x: float, y: float) -> tuple[float, float]:
        return (cx + x, cy + y) if relative else (x, y)

    if upper == "M":
        end = point(nums[0], nums[1])
        coords.append(end)
        return end, end, coords
    if upper == "L":
        end = point(nums[0], nums[1])
        coords.append(end)
        return end, subpath_start, coords
    if upper == "H":
        end = (cx + nums[0] if relative else nums[0], cy)
        coords.append(end)
        return end, subpath_start, coords
    if upper == "V":
        end = (cx, cy + nums[0] if relative else nums[0])
        coords.append(end)
        return end, subpath_start, coords
    if upper == "C":
        p1 = point(nums[0], nums[1])
        p2 = point(nums[2], nums[3])
        end = point(nums[4], nums[5])
        coords.extend([p1, p2, end])
        return end, subpath_start, coords
    if upper == "S":
        p2 = point(nums[0], nums[1])
        end = point(nums[2], nums[3])
        coords.extend([p2, end])
        return end, subpath_start, coords
    if upper == "Q":
        p1 = point(nums[0], nums[1])
        end = point(nums[2], nums[3])
        coords.extend([p1, end])
        return end, subpath_start, coords
    if upper == "T":
        end = point(nums[0], nums[1])
        coords.append(end)
        return end, subpath_start, coords
    if upper == "A":
        end = point(nums[5], nums[6])
        coords.append(end)
        return end, subpath_start, coords
    return current, subpath_start, coords


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def average_row(rows: list[dict[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {column: "" for column in CSV_COLUMNS}
    for column in CSV_COLUMNS:
        if column in TEXT_COLUMNS:
            continue
        values = []
        for row in rows:
            value = row.get(column, "")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                values.append(float(value))
        if values:
            out[column] = float(sum(values) / len(values))
    return out


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: csv_value(row.get(column, "")) for column in CSV_COLUMNS})


def csv_value(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.8g}"
    return value


def write_unmatched_report(out_dir: Path, unmatched_binary: list[Path], unmatched_svg: list[Path]) -> None:
    report = out_dir / "unmatched_files.txt"
    lines = ["Binary files without matched SVG:"]
    lines.extend(str(path) for path in unmatched_binary) if unmatched_binary else lines.append("(none)")
    lines.append("")
    lines.append("SVG files without matched binary image:")
    lines.extend(str(path) for path in unmatched_svg) if unmatched_svg else lines.append("(none)")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
