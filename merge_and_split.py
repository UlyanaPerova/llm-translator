"""
Объединение нескольких PNG в одно изображение, затем нарезка на части
с интеллектуальным поиском пустых полос и установкой DPI 300.

Использование:
    python merge_and_split.py                          # берёт все PNG из screenshots/
    python merge_and_split.py -s my_folder             # берёт PNG из my_folder/
    python merge_and_split.py image1.png image2.png    # конкретные файлы
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, PngImagePlugin
import numpy as np
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("merge_and_split")

Image.MAX_IMAGE_PIXELS = None

DPI = 300


# ── Шаг 1: объединение ──────────────────────────────────────────────

def merge_images(
    paths: list[Path],
    direction: str = "vertical",
    gap: int = 0,
    bg_color: str = "white",
    resize_mode: str = "none",
) -> Image.Image:
    images = []
    for p in paths:
        print(f"Загрузка: {p}")
        img = Image.open(p)
        img.load()
        images.append(img)

    if not images:
        print("Нет изображений для объединения.")
        sys.exit(1)

    if resize_mode == "fit_width":
        target_w = min(im.width for im in images)
        images = [
            im.resize((target_w, int(im.height * target_w / im.width)), Image.LANCZOS)
            if im.width != target_w else im
            for im in images
        ]
    elif resize_mode == "fit_height":
        target_h = min(im.height for im in images)
        images = [
            im.resize((int(im.width * target_h / im.height), target_h), Image.LANCZOS)
            if im.height != target_h else im
            for im in images
        ]

    total_gap = gap * (len(images) - 1)

    if direction == "vertical":
        total_w = max(im.width for im in images)
        total_h = sum(im.height for im in images) + total_gap
    else:
        total_w = sum(im.width for im in images) + total_gap
        total_h = max(im.height for im in images)

    print(f"Итоговый размер после склейки: {total_w} × {total_h} px")

    if bg_color == "transparent":
        canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    else:
        canvas = Image.new("RGBA", (total_w, total_h), bg_color)

    offset = 0
    for im in images:
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        if direction == "vertical":
            x = (total_w - im.width) // 2
            canvas.paste(im, (x, offset), im)
            offset += im.height + gap
        else:
            y = (total_h - im.height) // 2
            canvas.paste(im, (offset, y), im)
            offset += im.width + gap

    return canvas


# ── Шаг 2: нарезка ──────────────────────────────────────────────────

def split_image(
    img: Image.Image,
    chunk_height: int = 3000,
    min_blank_strip: int = 30,
    padding: int = 15,
    threshold: int = 250,
) -> list[Image.Image]:
    arr = np.array(img.convert("L"))
    row_brightness = arr.mean(axis=1)
    is_blank = row_brightness > threshold

    parts_coords: list[tuple[int, int]] = []
    last_cut = 0

    for target in range(chunk_height, img.height, chunk_height):
        search_start = max(target - 500, last_cut + 500)
        search_end = min(target + 500, img.height - min_blank_strip)

        best_cut = None
        best_dist = float("inf")

        for y in range(search_start, search_end):
            if all(is_blank[y : y + min_blank_strip]):
                cut_point = y + min_blank_strip // 2
                dist = abs(cut_point - target)
                if dist < best_dist:
                    best_dist = dist
                    best_cut = cut_point

        if best_cut is None:
            best_cut = target

        parts_coords.append((last_cut, best_cut))
        last_cut = best_cut

    if last_cut < img.height:
        parts_coords.append((last_cut, img.height))

    chunks: list[Image.Image] = []
    for top, bottom in parts_coords:
        safe_top = max(0, top - padding)
        safe_bottom = min(img.height, bottom + padding)
        chunk = img.crop((0, safe_top, img.width, safe_bottom))
        chunks.append(chunk)

    return chunks


# ── Шаг 3: сохранение с DPI 300 ─────────────────────────────────────

def save_with_dpi(img: Image.Image, path: Path) -> None:
    if img.mode == "RGBA":
        img = img.convert("RGB")

    pnginfo = PngImagePlugin.PngInfo()
    # pHYs: 300 DPI = 11811 пикселей на метр (300 / 0.0254)
    # Pillow записывает pHYs-чанк через параметр dpi
    img.save(path, "PNG", optimize=True, dpi=(DPI, DPI), pnginfo=pnginfo)


# ── Вспомогательные ───────────────────────────────────────────────────

def _sort_key(p: Path):
    """Числовая сортировка: 2.png < 10.png < 380.png."""
    try:
        return (0, int(p.stem))
    except ValueError:
        return (1, p.stem)


# ── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Склеить PNG-файлы → нарезать на части → DPI 300"
    )
    parser.add_argument(
        "images", nargs="*", type=Path,
        help="PNG-файлы для склейки (если не указаны — берутся из --source-dir)",
    )
    parser.add_argument(
        "-s", "--source-dir", type=Path, default=Path("screenshots"),
        help="Папка с PNG-файлами (по умолчанию: screenshots/). Используется, если файлы не указаны явно",
    )
    parser.add_argument(
        "-d", "--direction",
        choices=["vertical", "horizontal"], default="vertical",
        help="Направление склейки (по умолчанию: vertical)",
    )
    parser.add_argument(
        "--gap", type=int, default=0,
        help="Отступ между изображениями в пикселях",
    )
    parser.add_argument(
        "--bg", default="white",
        help="Цвет фона: white | black | transparent",
    )
    parser.add_argument(
        "--resize",
        choices=["none", "fit_width", "fit_height"], default="none",
        help="Подогнать размеры перед склейкой",
    )
    parser.add_argument(
        "--chunk-height", type=int, default=3000,
        help="Целевая высота одного куска (по умолчанию: 3000 px)",
    )
    parser.add_argument(
        "--min-blank", type=int, default=30,
        help="Минимум пустых строк для разреза (по умолчанию: 30)",
    )
    parser.add_argument(
        "--padding", type=int, default=15,
        help="Отступ от края разреза (по умолчанию: 15 px)",
    )
    parser.add_argument(
        "--threshold", type=int, default=250,
        help="Порог яркости для пустой строки (по умолчанию: 250)",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Имя папки для результатов (если не указано — спросит интерактивно)",
    )

    args = parser.parse_args()

    # Собираем файлы: явно указанные или из source-dir
    if args.images:
        files = args.images
        for p in files:
            if not p.exists():
                print(f"Файл не найден: {p}")
                sys.exit(1)
    else:
        src = args.source_dir
        if not src.is_dir():
            print(f"Папка не найдена: {src}")
            sys.exit(1)
        files = sorted(src.glob("*.png"), key=lambda p: _sort_key(p))
        if not files:
            print(f"В папке {src} нет PNG-файлов.")
            sys.exit(1)
        print(f"Найдено {len(files)} PNG в {src}/")

    # Определяем выходную папку
    if args.output_dir:
        folder_name = args.output_dir
    else:
        folder_name = input("Введите имя папки для результатов: ").strip()
        if not folder_name:
            print("Имя папки не может быть пустым.")
            sys.exit(1)

    out_dir = Path(folder_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Результаты будут сохранены в: {out_dir.resolve()}")

    # 1. Склейка
    print("\n── Склейка ──")
    merged = merge_images(
        files,
        direction=args.direction,
        gap=args.gap,
        bg_color=args.bg,
        resize_mode=args.resize,
    )

    # 2. Нарезка
    print("\n── Нарезка ──")
    chunks = split_image(
        merged,
        chunk_height=args.chunk_height,
        min_blank_strip=args.min_blank,
        padding=args.padding,
        threshold=args.threshold,
    )
    print(f"Нарезано {len(chunks)} частей")

    # 3. Сохранение с DPI 300
    print("\n── Сохранение (DPI {}) ──".format(DPI))
    for i, chunk in enumerate(chunks):
        out_path = out_dir / f"part_{i:03d}.png"
        save_with_dpi(chunk, out_path)
        print(f"  {out_path}  ({chunk.width}×{chunk.height})")

    print(f"\nГотово! {len(chunks)} файлов в папке «{out_dir}»")


if __name__ == "__main__":
    main()
