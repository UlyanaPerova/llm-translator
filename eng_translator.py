#!/usr/bin/env python3
"""
English → Russian Literary Translator
Reads .epub or .docx, splits into chunks with overlap context,
translates via GPT-5.2 with glossary support, saves as .docx
"""

import argparse
import json
import sys
import time
import re
from pathlib import Path
from dotenv import load_dotenv
import os
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("eng_translate") 

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    sys.exit("openai не установлен. Запусти: pip install openai")

try:
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    sys.exit("python-docx не установлен. Запусти: pip install python-docx")

try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
except ImportError:
    ebooklib = None

# ─────────────────────────── CONFIG ───────────────────────────

API_KEY = os.getenv("OPENAI_API_KEY") or sys.exit("OPENAI_API_KEY не найден в окружении. Установи его в .env файле.")
MODEL = "gpt-5.2"
TEMPERATURE = 0.55
MAX_CHARS_PER_CHUNK = 4000
CONTEXT_PARAGRAPHS = 3
DELAY_BETWEEN_REQUESTS = 1.5
REASONING_EFFORT = None  # None, "low", "medium", "high", "xhigh"

SYSTEM_PROMPT_BASE = """You are a professional Russian literary translator. Your translation must be indistinguishable from a text originally written by a skilled native Russian author.

Rules:
1. Translate into natural, expressive, literary Russian. NEVER translate literally. Completely restructure sentences to follow Russian syntax, rhythm, and logic. If a sentence sounds like it was translated — rewrite it.
2. Eliminate passive voice wherever possible. Russian strongly prefers active constructions. "He was stopped" → "Его остановили" or "Он остановился", never "Он был остановлен".
3. Watch for tautology and cacophony, same-root words in Russian. Always reread your Russian output and fix any repetitions of roots, sounds, or syllables in close proximity.
4. Use em-dashes (—) rarely, mostly never, except for dialogues. Do NOT insert em-dashes that weren't implied in the original. Russian text overloaded with em-dashes looks amateurish. Prefer commas, colons, semicolons, or sentence breaks where they fit naturally. 
   - Every line of dialogue starts on a new line with an em-dash: — Привет.
   - Dialogue is NEVER embedded mid-paragraph. Each speaker's line is a separate paragraph.
   - The only exception: a single utterance split by an attribution — Привет, — сказал он, — как дела?
   - Never use English-style quotation marks for dialogue.
6. If the source text contains obvious typos, garbled characters, or OCR artifacts, silently correct them based on context before translating.
7. For character names: transliterate them into Russian on first mention (e.g. Pawarit → Паварит) and use only the Russian form throughout. For brand names, titles of works, and organization names: keep in English unless they have an established Russian equivalent.
8. Preserve the author's tone and intent, but express it with the full richness of Russian — use varied vocabulary, expressive word order, and natural collocations.
9. Maintain paragraph structure from the original, except where dialogue must be reformatted per rule 5.
10. Adapt idioms and culturally-specific expressions so they feel organic in Russian. Do NOT invent or add content that isn't in the original.
11. Do NOT add translator's notes, explanations, or commentary.
12. Do NOT skip or summarize any part of the text.

IMPORTANT: If you receive context from a previous translation chunk (marked as [CONTEXT FROM PREVIOUS CHUNK]), use it ONLY to maintain consistency in tone, style, character names, and narrative flow. Do NOT re-translate the context — translate ONLY the new text that follows after the context block."""


# ─────────────────────────── GLOSSARY ───────────────────────────


def build_system_prompt(glossary: dict[str, str]) -> str:
    """Build system prompt, appending glossary if provided."""
    if not glossary:
        return SYSTEM_PROMPT_BASE

    glossary_lines = "\n".join(f"  {eng} → {rus}" for eng, rus in glossary.items())
    return (
        SYSTEM_PROMPT_BASE
        + "\n\nMANDATORY GLOSSARY — always use these exact translations (decline normally in Russian according to grammatical context):\n"
        + glossary_lines
    )


# ─────────────────────────── TEXT EXTRACTION ───────────────────────────


def extract_from_docx(filepath: str) -> str:
    """Extract text from .docx preserving paragraph breaks."""
    doc = Document(filepath)
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def extract_from_epub(filepath: str) -> str:
    """Extract text from .epub preserving paragraph breaks."""
    if ebooklib is None:
        sys.exit("Для .epub нужны библиотеки: pip install ebooklib beautifulsoup4 lxml")

    book = epub.read_epub(filepath, options={"ignore_ncx": True})
    full_text = []

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()

        for p in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "div"]):
            text = p.get_text(strip=True)
            if text:
                full_text.append(text)

    return "\n\n".join(full_text)


def extract_text(filepath: str) -> str:
    """Auto-detect format and extract."""
    ext = Path(filepath).suffix.lower()
    if ext == ".docx":
        return extract_from_docx(filepath)
    elif ext == ".epub":
        return extract_from_epub(filepath)
    else:
        sys.exit(f"Неподдерживаемый формат: {ext}. Нужен .docx или .epub")


# ─────────────────────────── CHUNKING ───────────────────────────


def split_into_chunks(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """
    Split text into chunks by paragraphs, respecting max_chars limit.
    Never splits mid-paragraph.
    """
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = []
    current_length = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        if para_len > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            chunks.append(para)
            continue

        if current_length + para_len + 2 > max_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = []
            current_length = 0

        current_chunk.append(para)
        current_length += para_len + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


# ─────────────────────────── CONTEXT HELPER ───────────────────────────


def get_tail_paragraphs(text: str, n: int = CONTEXT_PARAGRAPHS) -> str:
    """Extract last N paragraphs from translated text for context overlap."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    tail = paragraphs[-n:] if len(paragraphs) >= n else paragraphs
    return "\n\n".join(tail)


def build_user_message(chunk: str, previous_translation: str | None) -> str:
    """Build the user message with optional context from previous chunk."""
    if previous_translation:
        context = get_tail_paragraphs(previous_translation)
        return (
            f"[CONTEXT FROM PREVIOUS CHUNK — do NOT re-translate this, use only for continuity:]\n"
            f"{context}\n\n"
            f"[NEW TEXT TO TRANSLATE:]\n"
            f"{chunk}"
        )
    return chunk


# ─────────────────────────── TRANSLATION ───────────────────────────


def translate_chunk(
    client: OpenAI,
    chunk: str,
    chunk_num: int,
    total: int,
    previous_translation: str | None = None,
    reasoning_effort: str | None = None,
    glossary: dict[str, str] | None = None,
) -> str:
    """Translate a single chunk via GPT-5.2."""
    has_context = previous_translation is not None
    ctx_label = " +ctx" if has_context else ""
    print(
        f"  📝 Перевожу чанк {chunk_num}/{total} ({len(chunk)} символов{ctx_label})...",
        end=" ",
        flush=True,
    )

    user_message = build_user_message(chunk, previous_translation)

    kwargs = dict(
        model=MODEL,
        messages=[
            {"role": "system", "content": build_system_prompt(glossary or {})},
            {"role": "user", "content": user_message},
        ],
    )

    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["temperature"] = TEMPERATURE

    def _call():
        response = client.chat.completions.create(**kwargs)
        result = response.choices[0].message.content.strip()
        tokens_used = response.usage.total_tokens if response.usage else "?"
        return result, tokens_used

    try:
        result, tokens_used = _call()
        print(f"✅ (токенов: {tokens_used})")
        return result

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        print(f"  🔄 Повторная попытка через 10 секунд...")
        time.sleep(10)
        try:
            result, tokens_used = _call()
            print(f"  ✅ Повторная попытка успешна! (токенов: {tokens_used})")
            return result
        except Exception as e2:
            print(f"  ❌ Повторная ошибка: {e2}")
            return f"[ОШИБКА ПЕРЕВОДА ЧАНКА {chunk_num}: {e2}]"


# ─────────────────────────── SAVE DOCX ───────────────────────────


def save_to_docx(translated_chunks: list[str], output_path: str):
    """Save translated text to a formatted .docx file."""
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

    # Set default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Times New Roman"
    font.size = Pt(12)

    # Set margins
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    for chunk_text in translated_chunks:
        paragraphs = chunk_text.split("\n\n")
        for para_text in paragraphs:
            para_text = para_text.strip()
            if not para_text:
                continue
            sub_paragraphs = para_text.split("\n")
            for sub in sub_paragraphs:
                sub = sub.strip()
                if sub:
                    p = doc.add_paragraph(sub)
                    p.paragraph_format.space_after = Pt(6)
                    p.paragraph_format.first_line_indent = Cm(1.25)

    doc.save(output_path)
    print(f"\n💾 Сохранено: {output_path}")


# ─────────────────────────── MAIN ───────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="English → Russian Literary Translator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python3 eng_translator.py book.docx
  python3 eng_translator.py book.epub -o перевод.docx
  python3 eng_translator.py book.docx --reasoning medium
  python3 eng_translator.py book.docx --chunk-size 5000 --context 5
  python3 eng_translator.py book.docx --glossary glossary.json
        """,
    )
    parser.add_argument("input", help="Путь к .epub или .docx файлу")
    parser.add_argument(
        "-o", "--output", help="Путь к выходному .docx (по умолчанию: input_translated.docx)"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=MAX_CHARS_PER_CHUNK,
        help=f"Макс. символов на чанк (по умолчанию: {MAX_CHARS_PER_CHUNK})",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=CONTEXT_PARAGRAPHS,
        help=f"Кол-во абзацев из предыдущего перевода для контекста (по умолчанию: {CONTEXT_PARAGRAPHS}, 0 = отключить)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_REQUESTS,
        help=f"Пауза между запросами в секундах (по умолчанию: {DELAY_BETWEEN_REQUESTS})",
    )
    parser.add_argument(
        "--reasoning",
        type=str,
        default=REASONING_EFFORT,
        choices=["none", "low", "medium", "high", "xhigh"],
        help="Уровень reasoning (по умолчанию: отключён)",
    )
    parser.add_argument(
        "--glossary",
        type=str,
        default=None,
        help="Путь к JSON-файлу со словарём (по умолчанию: отключён)",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Файл не найден: {args.input}")

    # Output path
    if args.output:
        output_path = args.output
    else:
        stem = Path(args.input).stem
        output_path = f"{stem}_translated.docx"

    # Glossary
    glossary = {}
    if args.glossary:
        if not os.path.isfile(args.glossary):
            sys.exit(f"Словарь не найден: {args.glossary}")
        with open(args.glossary, "r", encoding="utf-8") as f:
            glossary = json.load(f)
        print(f"📚 Словарь загружен: {len(glossary)} терминов")

    # Reasoning effort
    reasoning = args.reasoning if args.reasoning and args.reasoning != "none" else None

    # Extract
    print(f"📖 Читаю файл: {args.input}")
    text = extract_text(args.input)
    print(f"   Извлечено {len(text)} символов")

    if not text.strip():
        sys.exit("Файл пуст или не удалось извлечь текст.")

    # Chunk
    chunks = split_into_chunks(text, max_chars=args.chunk_size)
    print(f"✂️  Разбито на {len(chunks)} чанков (макс. {args.chunk_size} символов)")
    print(f"📎 Контекст: {args.context} абзацев из предыдущего перевода")
    if reasoning:
        print(f"🧠 Reasoning: {reasoning}")
    print()

    # Estimate cost (rough)
    estimated_input_tokens = len(text) * 0.8
    if args.context > 0:
        estimated_input_tokens *= 1.15
    estimated_output_tokens = estimated_input_tokens * 1.5
    estimated_cost = (estimated_input_tokens * 2.50 + estimated_output_tokens * 10) / 1_000_000
    if reasoning:
        multiplier = {"low": 1.3, "medium": 1.8, "high": 2.5, "xhigh": 4.0}[reasoning]
        estimated_cost *= multiplier

    print(f"💰 Примерная стоимость: ${estimated_cost:.3f}")
    print(f"   (input ~{estimated_input_tokens:.0f} tokens, output ~{estimated_output_tokens:.0f} tokens)")
    if reasoning:
        multiplier = {"low": 1.3, "medium": 1.8, "high": 2.5, "xhigh": 4.0}[reasoning]
        print(f"   (с учётом reasoning-наценки ×{multiplier})")
    print()

    confirm = input("Продолжить? [Y/n]: ").strip().lower()
    if confirm == "n":
        sys.exit("Отменено.")

    # Translate
    client = OpenAI(api_key=API_KEY)
    translated = []

    for i, chunk in enumerate(chunks, 1):
        prev = None
        if args.context > 0 and translated:
            prev = translated[-1]

        result = translate_chunk(
            client,
            chunk,
            i,
            len(chunks),
            previous_translation=prev,
            reasoning_effort=reasoning,
            glossary=glossary,
        )
        translated.append(result)
        if i < len(chunks):
            time.sleep(args.delay)

    # Save
    save_to_docx(translated, output_path)

    # Summary
    total_chars_in = sum(len(c) for c in chunks)
    total_chars_out = sum(len(c) for c in translated)
    print(f"\n📊 Итого:")
    print(f"   Исходный текст: {total_chars_in:,} символов")
    print(f"   Перевод:        {total_chars_out:,} символов")
    print(f"   Чанков:         {len(chunks)}")
    print(f"   Контекст:       {args.context} абзацев между чанками")
    if glossary:
        print(f"   Словарь:        {len(glossary)} терминов")
    if reasoning:
        print(f"   Reasoning:      {reasoning}")
    print(f"   Файл:           {output_path}")


if __name__ == "__main__":
    main()
