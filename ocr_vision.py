"""
OCR скриншотов через AI Vision (Gemini / OpenAI) → .docx с форматированием.

Использование:
    python ocr_vision.py                           # все PNG из screenshots/
    python ocr_vision.py -s my_folder              # PNG из другой папки
    python ocr_vision.py 381.png 382.png           # конкретные файлы
    python ocr_vision.py --test 381.png            # тест на одном файле (вывод в консоль)
    python ocr_vision.py --provider openai          # использовать OpenAI GPT-4o
    python ocr_vision.py --provider gemini          # использовать Gemini (по умолчанию)
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from docx import Document
from docx.shared import Pt, Cm
from logger import setup_logger
import logging

load_dotenv()

setup_logger(prefix="ocr_vision")
log = logging.getLogger("ocr_vision")

CACHE_FILE = ".ocr_vision_cache.json"
MAX_IMAGE_HEIGHT = 8000   # пикселей — выше этого изображение разрезается на части
SPLIT_OVERLAP = 200       # перекрытие между частями, чтобы не терять строки на стыках

SYSTEM_PROMPT = """\
You are an assistive technology tool that helps visually impaired users read screen content. \
The user will share a screenshot from their screen. Your job is to read aloud all visible text, \
preserving its structure and formatting for accessibility purposes.

Output rules:
1. Reproduce every visible character faithfully: ellipsis (...), em-dashes (—), quotes ("", '', «»), emoticons like m(_ _)m
2. Indicate bold text with <b>...</b> and italic with <i>...</i>. Bold+italic: <b><i>...</i></b>
3. Separate paragraphs with double newlines
4. If a word is visually hyphenated at a line break, rejoin it into one word
5. Output ONLY the text content — no commentary, no descriptions, no notes
6. Preserve emoji and special Unicode characters exactly
"""

USER_PROMPT = "Please read all visible text from this screenshot for accessibility."


# ── Вспомогательные ───────────────────────────────────────────────────

def _sort_key(p: Path):
    """Числовая сортировка: 2.png < 10.png < 380.png."""
    try:
        return (0, int(p.stem))
    except ValueError:
        return (1, p.stem)


def load_cache(cache_path: Path) -> dict:
    """Загружает кэш распознанных файлов."""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            log.warning("Повреждённый кэш — начинаю заново")
    return {}


def save_cache(cache_path: Path, cache: dict):
    """Сохраняет кэш атомарно."""
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(cache_path)


# ── Нарезка больших изображений ──────────────────────────────────────

def _get_image_size(image_path: Path) -> tuple[int, int]:
    """Возвращает (width, height) без загрузки всего изображения в память."""
    from PIL import Image
    with Image.open(image_path) as img:
        return img.size


def _split_image(image_path: Path) -> list[Path]:
    """Разрезает слишком высокое изображение на части с перекрытием.
    Возвращает список путей к временным файлам (нужно удалить после использования)."""
    from PIL import Image

    with Image.open(image_path) as img:
        width, height = img.size

        if height <= MAX_IMAGE_HEIGHT:
            return [image_path]

        chunks: list[Path] = []
        step = MAX_IMAGE_HEIGHT - SPLIT_OVERLAP
        y = 0
        part = 1

        while y < height:
            y_end = min(y + MAX_IMAGE_HEIGHT, height)
            box = (0, y, width, y_end)
            chunk = img.crop(box)

            tmp = Path(tempfile.mktemp(
                prefix=f"{image_path.stem}_part{part}_",
                suffix=".png",
            ))
            chunk.save(tmp, format="PNG")
            log.info("  Часть %d: y=%d..%d (%d px) → %s",
                     part, y, y_end, y_end - y, tmp.name)
            chunks.append(tmp)

            y += step
            part += 1

        log.info("  Изображение %s разрезано на %d частей (оригинал: %dx%d)",
                 image_path.name, len(chunks), width, height)
        return chunks


def _deduplicate_overlap(texts: list[str]) -> str:
    """Склеивает тексты из частей, убирая дублированные строки на стыках."""
    if len(texts) == 1:
        return texts[0]

    result_lines = texts[0].rstrip().split("\n")

    for chunk_text in texts[1:]:
        chunk_lines = chunk_text.strip().split("\n")
        if not chunk_lines:
            continue

        # Ищем перекрытие: последние строки предыдущего текста = первые строки нового
        best_overlap = 0
        tail = result_lines[-20:]  # сравниваем последние 20 строк

        for overlap_size in range(1, min(len(tail), len(chunk_lines)) + 1):
            tail_slice = [l.strip() for l in tail[-overlap_size:]]
            chunk_slice = [l.strip() for l in chunk_lines[:overlap_size]]
            if tail_slice == chunk_slice:
                best_overlap = overlap_size

        if best_overlap > 0:
            log.debug("  Убрано %d дублированных строк на стыке", best_overlap)
            chunk_lines = chunk_lines[best_overlap:]

        result_lines.extend(chunk_lines)

    return "\n".join(result_lines)


# ── OCR провайдеры ──────────────────────────────────────────────────

def _ocr_gemini(image_path: Path) -> str:
    """OCR через Google Gemini 2.0 Flash."""
    from google import genai
    from google.genai import types
    from PIL import Image

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY не найден в .env\nПолучить: https://aistudio.google.com/apikey")

    client = genai.Client(api_key=api_key)
    img = Image.open(image_path)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[SYSTEM_PROMPT, img, USER_PROMPT],
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=16000,
        ),
    )

    if response.text is None:
        # Gemini иногда возвращает пустой ответ (модерация или сбой)
        log.warning("  [Gemini] Пустой ответ (response.text=None), повтор...")
        raise RuntimeError("Gemini вернул пустой ответ (response.text=None)")

    text = response.text.strip()
    if not text:
        raise RuntimeError("Gemini вернул пустую строку")

    usage = response.usage_metadata
    input_tokens = usage.prompt_token_count if usage else 0
    output_tokens = usage.candidates_token_count if usage else 0
    total_tokens = input_tokens + output_tokens

    # Gemini 2.5 Flash: $0.15/1M input, $0.60/1M output (до 200k), $3.50/$10.50 (>200k)
    cost = input_tokens * 0.15 / 1_000_000 + output_tokens * 0.60 / 1_000_000

    log.info("  [Gemini] Токены: %d (in: %d, out: %d), стоимость: $%.4f",
             total_tokens, input_tokens, output_tokens, cost)
    return text


def _ocr_openai(image_path: Path) -> str:
    """OCR через OpenAI GPT-4o."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY не найден в .env")

    client = OpenAI(api_key=api_key)
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    ext = image_path.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                            "detail": "high",
                        },
                    },
                    {"type": "text", "text": USER_PROMPT},
                ],
            },
        ],
        max_tokens=16000,
        temperature=0,
    )

    text = response.choices[0].message.content.strip()
    usage = response.usage
    input_tokens = usage.prompt_tokens if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    total_tokens = usage.total_tokens if usage else 0

    # GPT-4o: $2.50/1M input, $10.00/1M output
    cost = input_tokens * 2.50 / 1_000_000 + output_tokens * 10.00 / 1_000_000

    log.info("  [GPT-4o] Токены: %d (in: %d, out: %d), стоимость: $%.4f",
             total_tokens, input_tokens, output_tokens, cost)
    return text


PROVIDERS = {
    "gemini": _ocr_gemini,
    "openai": _ocr_openai,
}


def _ocr_single(provider_fn, image_path: Path) -> str:
    """OCR одного изображения с retry при 429."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return provider_fn(image_path)
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "rate" in err_str.lower():
                wait = 30 * (attempt + 1)
                log.warning("Rate limit (429) — жду %d сек (попытка %d/%d)...",
                            wait, attempt + 1, max_retries)
                time.sleep(wait)
            else:
                raise
    # Последняя попытка без обработки
    return provider_fn(image_path)


def ocr_image(provider_fn, image_path: Path) -> str:
    """OCR с автоматической нарезкой слишком больших изображений."""
    width, height = _get_image_size(image_path)

    if height <= MAX_IMAGE_HEIGHT:
        return _ocr_single(provider_fn, image_path)

    # Изображение слишком большое — разрезаем
    log.info("  Изображение %s слишком высокое (%d px) — разрезаю на части...",
             image_path.name, height)
    chunks = _split_image(image_path)
    temp_files = [c for c in chunks if c != image_path]  # только временные

    try:
        texts = []
        for j, chunk_path in enumerate(chunks):
            # Пропускаем слишком маленькие хвостики (< 500 px) — там обычно пусто
            chunk_w, chunk_h = _get_image_size(chunk_path)
            if chunk_h < 500 and j == len(chunks) - 1:
                log.info("  Пропускаю часть %d/%d: слишком маленькая (%d px)",
                         j + 1, len(chunks), chunk_h)
                continue

            log.info("  OCR часть %d/%d: %s", j + 1, len(chunks), chunk_path.name)
            try:
                text = _ocr_single(provider_fn, chunk_path)
                texts.append(text)
            except Exception as e:
                log.warning("  Часть %d/%d не удалась: %s — пропускаю",
                            j + 1, len(chunks), e)

        if not texts:
            raise RuntimeError(f"Ни одна часть {image_path.name} не распозналась")

        combined = _deduplicate_overlap(texts)
        log.info("  Склеено %d частей → %d символов", len(texts), len(combined))
        return combined
    finally:
        # Удаляем временные файлы
        for tmp in temp_files:
            try:
                tmp.unlink()
            except OSError:
                pass


# ── Форматирование ───────────────────────────────────────────────────

_TAG_SPLIT_RE = re.compile(r"(</?[bi]>)")


def _normalize_markdown_to_html(text: str) -> str:
    """Конвертирует markdown bold/italic в HTML-теги.
    ***text*** или ___text___ → <b><i>text</i></b>
    **text** → <b>text</b>
    *text* или _text_ → <i>text</i>
    """
    # Bold+italic: ***text*** или ___text___
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'___(.+?)___', r'<b><i>\1</i></b>', text)
    # Bold: **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Italic: *text* (но не **text**)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    return text


def _parse_formatting(text: str) -> list[tuple[str, bool, bool]]:
    """Парсит HTML-теги <b>, <i> в (text, bold, italic) сегменты.
    Также поддерживает markdown **bold** и *italic*.
    Совместимо с eng_translator.py."""
    # Сначала конвертируем markdown → HTML
    text = _normalize_markdown_to_html(text)

    if "<b>" not in text and "<i>" not in text:
        return [(text, False, False)]

    segments = []
    bold = False
    italic = False

    for part in _TAG_SPLIT_RE.split(text):
        if part == "<b>":
            bold = True
        elif part == "</b>":
            bold = False
        elif part == "<i>":
            italic = True
        elif part == "</i>":
            italic = False
        elif part:
            segments.append((part, bold, italic))

    return segments if segments else [(text, False, False)]


# ── Сохранение в .docx ───────────────────────────────────────────────

def save_to_docx(chapters: dict[str, str], output_path: str):
    """Сохраняет распознанный текст в .docx с форматированием.
    chapters: {filename: text}"""
    try:
        doc = Document()
    except Exception:
        from io import BytesIO
        import zipfile

        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>",
            )
            zf.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body></w:body></w:document>",
            )
        buf.seek(0)
        doc = Document(buf)

    # Шрифт и поля — как в eng_translator.py
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Times New Roman"
    font.size = Pt(12)

    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    for filename, text in chapters.items():
        # Заголовок главы
        chapter_num = Path(filename).stem
        heading = doc.add_heading(f"Chapter {chapter_num}", level=1)
        heading.paragraph_format.space_before = Pt(18)

        # Текст с форматированием
        paragraphs = text.split("\n\n")
        for para_text in paragraphs:
            para_text = para_text.strip()
            if not para_text:
                continue
            sub_paragraphs = para_text.split("\n")
            for sub in sub_paragraphs:
                sub = sub.strip()
                if sub:
                    p = doc.add_paragraph()
                    p.paragraph_format.space_after = Pt(6)
                    p.paragraph_format.first_line_indent = Cm(1.25)
                    for seg_text, bold, italic in _parse_formatting(sub):
                        run = p.add_run(seg_text)
                        if bold:
                            run.bold = True
                        if italic:
                            run.italic = True

    doc.save(output_path)
    log.info("Сохранено: %s", output_path)
    print(f"\nСохранено: {output_path}")


# ── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OCR скриншотов через AI Vision (Gemini / OpenAI) → .docx"
    )
    parser.add_argument(
        "images", nargs="*", type=Path,
        help="PNG-файлы (если не указаны — берутся из --source-dir)",
    )
    parser.add_argument(
        "-s", "--source-dir", type=Path, default=Path("screenshots"),
        help="Папка с PNG (по умолчанию: screenshots/)",
    )
    parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Имя выходного .docx файла (если не указано — спросит)",
    )
    parser.add_argument(
        "--test", type=Path, default=None,
        help="Тест на одном файле — вывод в консоль, без сохранения",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Начать заново, игнорируя кэш (по умолчанию кэш загружается автоматически)",
    )
    parser.add_argument(
        "--provider", choices=["gemini", "openai"], default="gemini",
        help="AI-провайдер: gemini (по умолчанию) или openai",
    )

    args = parser.parse_args()

    provider_fn = PROVIDERS[args.provider]
    log.info("Провайдер: %s", args.provider)

    # Режим теста
    if args.test:
        if not args.test.exists():
            print(f"Файл не найден: {args.test}")
            sys.exit(1)
        print(f"Тест OCR ({args.provider}): {args.test}")
        print("=" * 60)
        result = ocr_image(provider_fn, args.test)
        print(result)
        print("=" * 60)
        return

    # Собираем файлы
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

    print(f"Найдено {len(files)} файлов для OCR ({args.provider})")

    # Выходной файл
    if args.output:
        output_path = args.output
    else:
        output_path = input("Имя выходного .docx файла: ").strip()
        if not output_path:
            print("Имя файла не может быть пустым.")
            sys.exit(1)
    if not output_path.endswith(".docx"):
        output_path += ".docx"

    # Кэш — загружается ВСЕГДА, кроме --fresh
    cache_path = Path(CACHE_FILE)
    if args.fresh:
        cache = {}
        log.info("Режим --fresh: кэш игнорируется")
    else:
        cache = load_cache(cache_path)
        cached_count = sum(1 for f in files if f.name in cache)
        if cached_count:
            log.info("В кэше %d/%d файлов — пропускаю", cached_count, len(files))

    # OCR
    results: dict[str, str] = {}
    total_cost = 0.0

    for i, img_path in enumerate(files):
        if img_path.name in cache:
            log.info("[%d/%d] %s — из кэша", i + 1, len(files), img_path.name)
            results[img_path.name] = cache[img_path.name]
            continue

        log.info("[%d/%d] OCR: %s", i + 1, len(files), img_path.name)

        try:
            text = ocr_image(provider_fn, img_path)
            results[img_path.name] = text
            cache[img_path.name] = text
            save_cache(cache_path, cache)
        except Exception as e:
            log.error("[%d/%d] Ошибка OCR %s: %s", i + 1, len(files), img_path.name, e)
            log.info("Повтор через 10 секунд...")
            time.sleep(10)
            try:
                text = ocr_image(provider_fn, img_path)
                results[img_path.name] = text
                cache[img_path.name] = text
                save_cache(cache_path, cache)
            except Exception as e2:
                log.error("Повторная ошибка: %s — пропускаю", e2)
                results[img_path.name] = f"[OCR ERROR: {e2}]"

        # Промежуточное сохранение .docx после каждой главы
        if results and (i + 1) % 5 == 0:
            save_to_docx(results, output_path)
            log.info("Промежуточное сохранение: %d/%d глав → %s",
                     len(results), len(files), output_path)

    # Финальное сохранение
    if results:
        save_to_docx(results, output_path)
        total_chapters = len([v for v in results.values() if not v.startswith("[OCR ERROR")])
        print(f"\nОбработано {total_chapters}/{len(files)} глав → {output_path}")
    else:
        print("Нет результатов для сохранения.")


if __name__ == "__main__":
    main()
