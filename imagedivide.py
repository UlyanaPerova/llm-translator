from PIL import Image
import numpy as np
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("imagedivide") 

Image.MAX_IMAGE_PIXELS = None

img = Image.open("c_5_36_result.png")
arr = np.array(img.convert("L"))

row_brightness = arr.mean(axis=1)
threshold = 250
is_blank = row_brightness > threshold

chunk_height = 3000
min_blank_strip = 30   # минимум 30 подряд пустых строк (была 5)
padding = 15            # отступ от края пустой полосы

parts = []
last_cut = 0

for target in range(chunk_height, img.height, chunk_height):
    search_start = max(target - 500, last_cut + 500)
    search_end = min(target + 500, img.height - min_blank_strip)

    best_cut = None
    best_dist = float("inf")

    for y in range(search_start, search_end):
        if all(is_blank[y:y + min_blank_strip]):
            # Режем по середине пустой полосы
            cut_point = y + min_blank_strip // 2
            dist = abs(cut_point - target)
            if dist < best_dist:
                best_dist = dist
                best_cut = cut_point

    if best_cut is None:
        best_cut = target

    parts.append((last_cut, best_cut))
    last_cut = best_cut

if last_cut < img.height:
    parts.append((last_cut, img.height))

for i, (top, bottom) in enumerate(parts):
    # Добавляем padding — расширяем каждый кусок чтобы захватить немного соседнего пространства
    safe_top = max(0, top - padding)
    safe_bottom = min(img.height, bottom + padding)
    chunk = img.crop((0, safe_top, img.width, safe_bottom))
    chunk.save(f"part_{i:03d}.png")
    print(f"part_{i:03d}.png: строки {safe_top}-{safe_bottom}")

print(f"\nНарезано {len(parts)} частей")