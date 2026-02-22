"""
Скрипт для объединения больших PNG-файлов.
Поддерживает вертикальное и горизонтальное объединение.
Обрабатывает большие файлы без ограничения PIL по размеру.

Использование:
    python merge_png.py image1.png image2.png image3.png -o result.png
    python merge_png.py *.png -o result.png --direction horizontal
    python merge_png.py *.png -o result.png --gap 20 --bg white
"""

import argparse
import sys
from pathlib import Path
from PIL import Image
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("merge_png") 

# Снимаем лимит на размер изображений (по умолчанию ~178 млн пикселей)
Image.MAX_IMAGE_PIXELS = None


def merge_images(
    paths: list[Path],
    direction: str = "vertical",
    gap: int = 0,
    bg_color: str = "white",
    resize_mode: str = "none",
) -> Image.Image:
    """
    Объединяет список изображений.

    Args:
        paths: список путей к PNG-файлам
        direction: 'vertical' или 'horizontal'
        gap: отступ между изображениями в пикселях
        bg_color: цвет фона / отступов ('white', 'black', 'transparent')
        resize_mode: 'none' — без изменения размера,
                     'fit_width' — подогнать все под ширину наименьшего,
                     'fit_height' — подогнать все под высоту наименьшего
    """
    images = []
    for p in paths:
        print(f"Загрузка: {p}")
        img = Image.open(p)
        img.load()  # принудительно загружаем в память
        images.append(img)

    if not images:
        print("Нет изображений для объединения.")
        sys.exit(1)

    # Ресайз при необходимости
    if resize_mode == "fit_width":
        target_w = min(img.width for img in images)
        resized = []
        for img in images:
            if img.width != target_w:
                ratio = target_w / img.width
                new_h = int(img.height * ratio)
                img = img.resize((target_w, new_h), Image.LANCZOS)
            resized.append(img)
        images = resized

    elif resize_mode == "fit_height":
        target_h = min(img.height for img in images)
        resized = []
        for img in images:
            if img.height != target_h:
                ratio = target_h / img.height
                new_w = int(img.width * ratio)
                img = img.resize((new_w, target_h), Image.LANCZOS)
            resized.append(img)
        images = resized

    # Вычисляем итоговый размер
    total_gap = gap * (len(images) - 1)

    if direction == "vertical":
        total_w = max(img.width for img in images)
        total_h = sum(img.height for img in images) + total_gap
    else:
        total_w = sum(img.width for img in images) + total_gap
        total_h = max(img.height for img in images)

    print(f"Итоговый размер: {total_w} × {total_h} px")

    # Создаём холст
    if bg_color == "transparent":
        canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    else:
        canvas = Image.new("RGBA", (total_w, total_h), bg_color)

    # Вставляем изображения
    offset = 0
    for img in images:
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        if direction == "vertical":
            # центрируем по горизонтали
            x = (total_w - img.width) // 2
            canvas.paste(img, (x, offset), img)
            offset += img.height + gap
        else:
            # центрируем по вертикали
            y = (total_h - img.height) // 2
            canvas.paste(img, (offset, y), img)
            offset += img.width + gap

    return canvas


def main():
    parser = argparse.ArgumentParser(
        description="Объединение больших PNG-файлов"
    )
    parser.add_argument(
        "images",
        nargs="+",
        type=Path,
        help="Пути к PNG-файлам",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("merged.png"),
        help="Путь к результату (по умолчанию: merged.png)",
    )
    parser.add_argument(
        "-d", "--direction",
        choices=["vertical", "horizontal"],
        default="vertical",
        help="Направление объединения (по умолчанию: vertical)",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=0,
        help="Отступ между изображениями в пикселях",
    )
    parser.add_argument(
        "--bg",
        default="white",
        help="Цвет фона: white, black, transparent (по умолчанию: white)",
    )
    parser.add_argument(
        "--resize",
        choices=["none", "fit_width", "fit_height"],
        default="none",
        help="Подогнать размеры: none / fit_width / fit_height",
    )

    args = parser.parse_args()

    # Проверяем файлы
    for p in args.images:
        if not p.exists():
            print(f"Файл не найден: {p}")
            sys.exit(1)

    result = merge_images(
        args.images,
        direction=args.direction,
        gap=args.gap,
        bg_color=args.bg,
        resize_mode=args.resize,
    )

    # Сохраняем
    if args.bg == "transparent":
        result.save(args.output, "PNG")
    else:
        result.convert("RGB").save(args.output, "PNG", optimize=True)

    print(f"Сохранено: {args.output}")


if __name__ == "__main__":
    main()
