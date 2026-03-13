#!/usr/bin/env python3
"""
English -> Russian Literary Translator (QWEN-MT)
Reads .epub or .docx, splits into chunks with overlap context,
translates via Qwen-MT with glossary support, saves as .docx

Uses Alibaba Cloud DashScope API (OpenAI-compatible mode).
QWEN-MT is a specialized translation model — it does NOT support system
messages. Translation instructions are passed in the user message.
The built-in `terms` parameter enforces glossary consistency.
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

setup_logger(prefix="eng_translate_qwen")
log = logging.getLogger("eng_translate_qwen")

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    sys.exit("openai not installed. Run: pip install openai")

try:
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    sys.exit("python-docx not installed. Run: pip install python-docx")

try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
except ImportError:
    ebooklib = None

# ─────────────────────────── CONFIG ───────────────────────────

API_KEY = os.getenv("DASHSCOPE_API_KEY") or sys.exit(
    "DASHSCOPE_API_KEY not found. Set it in .env file."
)
BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODEL = "qwen-mt-plus"
TEMPERATURE = 0.45
MAX_CHARS_PER_CHUNK = 4000
CONTEXT_PARAGRAPHS = 3
DELAY_BETWEEN_REQUESTS = 1.5

# QWEN-MT constraints:
#  - No system messages (single user message only)
#  - Max 8192 input tokens
#  - translation_options.terms for glossary enforcement
#  - translation_options.domains only works when target_lang is English

TRANSLATION_INSTRUCTIONS = """You are a professional Russian literary translator. Your translation must be indistinguishable from a text originally written by a skilled native Russian author.

Rules:
1. Translate into natural, expressive, literary Russian. NEVER translate literally. Completely restructure sentences to follow Russian syntax, rhythm, and logic. If a sentence sounds like it was translated — rewrite it.
2. Eliminate passive voice wherever possible. Russian strongly prefers active constructions. "He was stopped" -> "Его остановили" or "Он остановился", never "Он был остановлен".
3. Watch for tautology and cacophony, same-root words in Russian. Always reread your Russian output and fix any repetitions of roots, sounds, or syllables in close proximity.
4. Use em-dashes (—) rarely, mostly never, except for dialogues. Do NOT insert em-dashes that weren't implied in the original. Russian text overloaded with em-dashes looks amateurish. Prefer commas, semicolons, or sentence breaks where they fit naturally.
   - Every line of dialogue starts on a new line with an em-dash: — Привет.
   - Dialogue is NEVER embedded mid-paragraph. Each speaker's line is a separate paragraph.
   - The only exception: a single utterance split by an attribution — Привет, — сказал он, — как дела?
   - Never use English-style quotation marks for dialogue.
6. If the source text contains obvious typos, garbled characters, or OCR artifacts, silently correct them based on context before translating.
7. EVERYTHING must be translated or transliterated into Russian. Nothing should remain in English in the final text. For character names: transliterate them into Russian on first mention (e.g. Pawarit -> Паварит) and use only the Russian form throughout. For organization names, skill names, titles, ranks, and any other terms: translate them into Russian. The only exceptions are real-world brand names (iPhone, Google) that are commonly used in Russian as-is.
8. Preserve the author's tone and intent, but express it with the full richness of Russian — use varied vocabulary, expressive word order, and natural collocations.
9. Maintain paragraph structure from the original, except where dialogue must be reformatted per rule 5.
10. Adapt idioms and culturally-specific expressions so they feel organic in Russian. Do NOT invent or add content that isn't in the original.
11. Do NOT add translator's notes, explanations, or commentary.
12. Do NOT skip or summarize any part of the text.
13. The source text may contain HTML formatting tags: <b>bold</b>, <i>italic</i>, <b><i>bold italic</i></b>. You MUST preserve these tags exactly in your translation, wrapping the corresponding translated words. Never add, remove, or alter these tags. Keep the same nesting order."""


# ─────────────────────────── GLOSSARY ───────────────────────────


def load_glossary(filepath: str) -> dict[str, str]:
    """
    Load glossary from JSON. Supports both:
    - Legacy flat format: {"English": "Russian", ...}
    - New structured format: {"characters": [...], "terms": [...], "locations": [...]}
    Returns a flat dict for use with build_system_prompt.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "characters" in data or "terms" in data or "locations" in data:
        return _flatten_structured_glossary(data)
    return data


def _flatten_structured_glossary(data: dict) -> dict[str, str]:
    """Convert structured glossary to flat dict with gender/indeclinability annotations."""
    flat = {}

    for char in data.get("characters", []):
        original = char.get("original", "")
        translation = char.get("translation", "")
        if not original or not translation:
            continue
        gender = char.get("gender", "unknown")
        indeclinable = char.get("indeclinable", False)
        parts = [gender]
        if indeclinable:
            parts.append("indeclinable")
        annotation = f"{translation} [{', '.join(parts)}]"
        flat[original] = annotation
        for alias in char.get("aliases", []):
            if alias:
                flat[alias] = annotation

    for term in data.get("terms", []):
        original = term.get("original", "")
        translation = term.get("translation", "")
        if original and translation:
            flat[original] = translation

    for loc in data.get("locations", []):
        original = loc.get("original", "")
        translation = loc.get("translation", "")
        if original and translation:
            flat[original] = translation

    return flat


def filter_glossary_for_chunk(glossary: dict[str, str], chunk: str) -> dict[str, str]:
    """Return only glossary entries whose original key appears in the chunk text."""
    if not glossary:
        return {}
    chunk_lower = chunk.lower()
    return {
        eng: rus
        for eng, rus in glossary.items()
        if eng.lower() in chunk_lower
    }


def _strip_glossary_annotation(value: str) -> str:
    """Strip gender/indeclinability annotations from glossary values.
    E.g., 'Нищий [m, indeclinable]' -> 'Нищий'"""
    return re.sub(r"\s*\[.*?\]\s*$", "", value).strip()


def glossary_to_terms(glossary: dict[str, str]) -> list[dict[str, str]]:
    """Convert flat glossary dict to QWEN-MT terms format.
    Strips annotations — terms only accept clean source/target pairs."""
    terms = []
    for source, target in glossary.items():
        clean_target = _strip_glossary_annotation(target)
        if source and clean_target:
            terms.append({"source": source, "target": clean_target})
    return terms


def build_glossary_note(glossary: dict[str, str]) -> str:
    """Build a glossary note with gender annotations for the user message.
    Only includes entries that have annotations (character names with gender)."""
    if not glossary:
        return ""
    annotated = {k: v for k, v in glossary.items() if "[" in v}
    if not annotated:
        return ""
    lines = "\n".join(f"  {eng} -> {rus}" for eng, rus in annotated.items())
    return (
        "\n[CHARACTER NAMES — gender info for correct Russian agreement:]\n"
        + lines
    )


# ─────────────────────────── FORMATTING MARKERS ───────────────────────────


def _docx_para_to_tuples(para) -> list[tuple[str, bool, bool]]:
    """Extract (text, bold, italic) tuples from a python-docx paragraph.
    Resolves formatting through run -> character style -> paragraph style hierarchy."""
    # Resolve paragraph-style defaults by walking the style chain
    style_bold = False
    style_italic = False
    try:
        ps = para.style
        while ps:
            if ps.font.bold is not None:
                style_bold = ps.font.bold
                break
            ps = ps.base_style
    except Exception:
        pass
    try:
        ps = para.style
        while ps:
            if ps.font.italic is not None:
                style_italic = ps.font.italic
                break
            ps = ps.base_style
    except Exception:
        pass

    result = []
    for run in para.runs:
        if not run.text:
            continue
        # Start with paragraph style defaults
        bold = style_bold
        italic = style_italic
        # Override with run's character style
        try:
            if run.style:
                cs = run.style
                while cs:
                    if cs.font.bold is not None:
                        bold = cs.font.bold
                        break
                    cs = cs.base_style
        except Exception:
            pass
        try:
            if run.style:
                cs = run.style
                while cs:
                    if cs.font.italic is not None:
                        italic = cs.font.italic
                        break
                    cs = cs.base_style
        except Exception:
            pass
        # Explicit run-level setting has highest priority
        if run.bold is not None:
            bold = run.bold
        if run.italic is not None:
            italic = run.italic
        result.append((run.text, bold, italic))
    return result


def _html_to_run_tuples(
    element, bold=False, italic=False,
    bold_classes: set | None = None, italic_classes: set | None = None,
) -> list[tuple[str, bool, bool]]:
    """Extract (text, bold, italic) tuples from an HTML element, recursively.
    Handles <b>, <strong>, <i>, <em> tags, inline styles, and CSS classes."""
    from bs4 import NavigableString

    result = []
    for child in element.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                result.append((text, bold, italic))
        elif child.name in ("script", "style"):
            continue
        elif child.name is not None:
            child_bold = bold or child.name in ("b", "strong")
            child_italic = italic or child.name in ("i", "em")
            # Inline style attribute
            style_attr = child.get("style", "")
            if style_attr:
                if re.search(r"font-weight\s*:\s*(bold|[7-9]00)", style_attr):
                    child_bold = True
                if re.search(r"font-style\s*:\s*italic", style_attr):
                    child_italic = True
            # CSS classes from epub stylesheet
            if bold_classes or italic_classes:
                classes = set(child.get("class", []))
                if bold_classes and classes & bold_classes:
                    child_bold = True
                if italic_classes and classes & italic_classes:
                    child_italic = True
            result.extend(_html_to_run_tuples(
                child, child_bold, child_italic, bold_classes, italic_classes,
            ))
    return result


def _parse_epub_css(book) -> tuple[set, set]:
    """Parse CSS stylesheets from an epub to find bold/italic class names."""
    bold_classes: set[str] = set()
    italic_classes: set[str] = set()
    try:
        for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
            css = item.get_content().decode("utf-8", errors="ignore")
            for m in re.finditer(r"\.([a-zA-Z_][\w-]*)\s*\{([^}]*)\}", css):
                cls_name = m.group(1)
                props = m.group(2)
                if re.search(r"font-weight\s*:\s*(bold|[7-9]00)", props):
                    bold_classes.add(cls_name)
                if re.search(r"font-style\s*:\s*italic", props):
                    italic_classes.add(cls_name)
    except Exception:
        pass
    return bold_classes, italic_classes


def _merge_and_mark_runs(runs: list[tuple[str, bool, bool]]) -> str:
    """Convert (text, bold, italic) tuples to text with HTML formatting tags.
    Adjacent runs with the same formatting are merged before marking."""
    if not runs:
        return ""

    # Merge consecutive runs with same formatting
    merged = [list(runs[0])]
    for text, bold, italic in runs[1:]:
        if (bold, italic) == (merged[-1][1], merged[-1][2]):
            merged[-1][0] += text
        else:
            merged.append([text, bold, italic])

    parts = []
    for text, bold, italic in merged:
        if bold and italic:
            parts.append(f"<b><i>{text}</i></b>")
        elif bold:
            parts.append(f"<b>{text}</b>")
        elif italic:
            parts.append(f"<i>{text}</i>")
        else:
            parts.append(text)

    return "".join(parts)


def _markdown_to_html_formatting(text: str) -> str:
    """Convert markdown bold/italic markers to HTML tags.
    Process order: bold-italic (***) -> bold (**) -> italic (*).
    After each step the matched markers are gone, so later steps won't mis-match."""
    text = re.sub(r"\*{3}(.+?)\*{3}", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*{2}(.+?)\*{2}", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+?)\*", r"<i>\1</i>", text)
    return text


_TAG_SPLIT_RE = re.compile(r"(</?[bi]>)")


def _parse_formatting(text: str) -> list[tuple[str, bool, bool]]:
    """Parse HTML formatting tags (<b>, <i>) into (text, bold, italic) segments.
    Uses a simple state machine: split by tags, track bold/italic state."""
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


def _strip_html_tags(text: str) -> str:
    """Remove HTML <b>, </b>, <i>, </i> tags from text."""
    return re.sub(r"</?[bi]>", "", text)


# ─────────────────────────── TEXT EXTRACTION ───────────────────────────


def extract_from_docx(filepath: str) -> str:
    """Extract text from .docx preserving paragraph breaks and formatting."""
    doc = Document(filepath)
    paragraphs = []
    fmt_count = 0
    for para in doc.paragraphs:
        if para.runs:
            text = _merge_and_mark_runs(_docx_para_to_tuples(para)).strip()
        else:
            text = para.text.strip()
        if text:
            if "<b>" in text or "<i>" in text:
                fmt_count += 1
            paragraphs.append(text)
    log.info("Docx: %d paragraphs extracted, %d with formatting tags", len(paragraphs), fmt_count)
    if fmt_count > 0:
        samples = [p for p in paragraphs if "<b>" in p or "<i>" in p][:3]
        for s in samples:
            log.debug("Format sample: %.300s", s)
    elif paragraphs:
        log.warning("No formatting tags detected in docx")
    return "\n\n".join(paragraphs)


def extract_from_epub(filepath: str) -> str:
    """Extract text from .epub preserving paragraph breaks and formatting."""
    if ebooklib is None:
        sys.exit("For .epub: pip install ebooklib beautifulsoup4 lxml")

    book = epub.read_epub(filepath, options={"ignore_ncx": True})
    bold_classes, italic_classes = _parse_epub_css(book)
    if bold_classes or italic_classes:
        log.info("Epub CSS classes: bold=%s, italic=%s", bold_classes, italic_classes)

    full_text = []
    fmt_count = 0

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()

        for p in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "div"]):
            text = _merge_and_mark_runs(
                _html_to_run_tuples(p, bold_classes=bold_classes, italic_classes=italic_classes)
            ).strip()
            if text:
                if "<b>" in text or "<i>" in text:
                    fmt_count += 1
                full_text.append(text)

    log.info("Epub: %d paragraphs extracted, %d with formatting tags", len(full_text), fmt_count)
    if fmt_count > 0:
        samples = [p for p in full_text if "<b>" in p or "<i>" in p][:3]
        for s in samples:
            log.debug("Format sample: %.300s", s)
    elif full_text:
        log.warning("No formatting tags detected in epub")
    return "\n\n".join(full_text)


def extract_from_md(filepath: str) -> str:
    """Extract text from .md/.txt preserving paragraph breaks.
    Converts markdown bold/italic markers to HTML tags."""
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read()

    raw = _markdown_to_html_formatting(raw)

    paragraphs = []
    fmt_count = 0
    for para in raw.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # Strip markdown heading markers (# ## ### etc.) — keep the text
        para = re.sub(r"^#{1,6}\s+", "", para)
        if para:
            if "<b>" in para or "<i>" in para:
                fmt_count += 1
            paragraphs.append(para)

    log.info("Markdown: %d paragraphs extracted, %d with formatting tags", len(paragraphs), fmt_count)
    if fmt_count > 0:
        samples = [p for p in paragraphs if "<b>" in p or "<i>" in p][:3]
        for s in samples:
            log.debug("Format sample: %.300s", s)
    elif paragraphs:
        log.warning("No formatting tags found in markdown file")
    return "\n\n".join(paragraphs)


def extract_text(filepath: str) -> str:
    """Auto-detect format and extract."""
    ext = Path(filepath).suffix.lower()
    if ext == ".docx":
        return extract_from_docx(filepath)
    elif ext == ".epub":
        return extract_from_epub(filepath)
    elif ext in (".md", ".txt"):
        return extract_from_md(filepath)
    else:
        sys.exit(f"Unsupported format: {ext}. Need .docx, .epub, .md or .txt")


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


def build_user_message(
    chunk: str,
    previous_translation: str | None,
    glossary_note: str = "",
    context_paragraphs: int = CONTEXT_PARAGRAPHS,
) -> str:
    """Build the user message for QWEN-MT.
    Since QWEN-MT doesn't support system messages, translation instructions
    are prepended to the user message with clear delimiters."""
    parts = []

    # Instructions block
    parts.append(
        "[TRANSLATION INSTRUCTIONS — follow these rules, do NOT translate this section:]\n"
        + TRANSLATION_INSTRUCTIONS
    )

    # Gender annotations for character names (supplements the terms parameter)
    if glossary_note:
        parts.append(glossary_note)

    # Context from previous translation
    if previous_translation:
        context = get_tail_paragraphs(previous_translation, context_paragraphs)
        parts.append(
            "\n[CONTEXT FROM PREVIOUS CHUNK — do NOT re-translate, use only for continuity:]\n"
            + context
        )

    # The actual text to translate
    parts.append(f"\n[TEXT TO TRANSLATE:]\n{chunk}")

    return "\n".join(parts)


# ─────────────────────────── TRANSLATION ───────────────────────────


def _log_format_tags(source: str, result: str, chunk_num: int, total: int):
    """Log formatting tag preservation between source and translated text."""
    in_b = source.count("<b>")
    in_i = source.count("<i>")
    if in_b + in_i == 0:
        return
    out_b = result.count("<b>")
    out_i = result.count("<i>")
    log.info(
        "Chunk %d/%d format tags: <b> %d->%d, <i> %d->%d",
        chunk_num, total, in_b, out_b, in_i, out_i,
    )
    if out_b < in_b or out_i < in_i:
        log.warning(
            "Chunk %d/%d: formatting tags LOST (%d+%d -> %d+%d)!",
            chunk_num, total, in_b, in_i, out_b, out_i,
        )


def translate_chunk(
    client: OpenAI,
    chunk: str,
    chunk_num: int,
    total: int,
    previous_translation: str | None = None,
    glossary: dict[str, str] | None = None,
    context_paragraphs: int = CONTEXT_PARAGRAPHS,
    model: str = MODEL,
) -> str:
    """Translate a single chunk via QWEN-MT.
    Uses translation_options.terms for glossary and instructions in user message."""
    has_context = previous_translation is not None
    ctx_label = " +ctx" if has_context else ""
    log.info("Translating chunk %d/%d (%d chars%s)", chunk_num, total, len(chunk), ctx_label)
    print(
        f"  Translating chunk {chunk_num}/{total} ({len(chunk)} chars{ctx_label})...",
        end=" ",
        flush=True,
    )

    chunk_glossary = filter_glossary_for_chunk(glossary or {}, chunk)
    terms = glossary_to_terms(chunk_glossary)
    glossary_note = build_glossary_note(chunk_glossary)

    if glossary and chunk_glossary:
        log.debug(
            "Chunk %d/%d: using %d/%d glossary entries (%d terms)",
            chunk_num, total, len(chunk_glossary), len(glossary), len(terms),
        )

    user_message = build_user_message(
        chunk, previous_translation, glossary_note, context_paragraphs,
    )

    translation_options = {
        "source_lang": "English",
        "target_lang": "Russian",
    }
    if terms:
        translation_options["terms"] = terms

    kwargs = dict(
        model=model,
        messages=[
            {"role": "user", "content": user_message},
        ],
        temperature=TEMPERATURE,
        extra_body={
            "translation_options": translation_options,
        },
    )

    def _call():
        response = client.chat.completions.create(**kwargs)
        result = response.choices[0].message.content.strip()
        tokens_used = response.usage.total_tokens if response.usage else "?"
        return result, tokens_used

    try:
        result, tokens_used = _call()
        print(f"OK (tokens: {tokens_used})")
        log.info("Chunk %d/%d done: %s tokens, %d chars out", chunk_num, total, tokens_used, len(result))
        _log_format_tags(chunk, result, chunk_num, total)
        return result

    except Exception as e:
        log.error("Chunk %d/%d failed: %s", chunk_num, total, e)
        print(f"FAIL: {e}")
        print(f"  Retrying in 10 seconds...")
        time.sleep(10)
        try:
            result, tokens_used = _call()
            print(f"  Retry OK! (tokens: {tokens_used})")
            log.info("Chunk %d/%d retry OK: %s tokens", chunk_num, total, tokens_used)
            _log_format_tags(chunk, result, chunk_num, total)
            return result
        except Exception as e2:
            log.error("Chunk %d/%d retry also failed: %s", chunk_num, total, e2)
            print(f"  Retry also failed: {e2}")
            return f"[TRANSLATION ERROR CHUNK {chunk_num}: {e2}]"


# ─────────────────────────── FORMATTING TRANSFER (PASS 2) ───────────────────────────

FORMATTING_TRANSFER_INSTRUCTIONS = """You are a formatting transfer tool. You receive:
1. An original English text with HTML formatting tags (<b> for bold, <i> for italic)
2. A Russian translation of the same text WITHOUT formatting tags

Your task: Add the HTML formatting tags (<b>, </b>, <i>, </i>) to the Russian translation so they wrap the corresponding translated words/phrases, matching the original English formatting.

Rules:
- Apply <b>...</b> to Russian words that correspond to bold English words
- Apply <i>...</i> to Russian words that correspond to italic English words
- Do NOT change the Russian text — only insert tags
- Preserve all line breaks and paragraph structure exactly
- Return ONLY the tagged Russian text, nothing else"""


def transfer_formatting(
    client: OpenAI,
    original_tagged: str,
    translated_plain: str,
    chunk_num: int,
    total: int,
    model: str = MODEL,
) -> str:
    """Pass 2: Transfer formatting from original English to translated Russian.
    Uses a separate QWEN-MT call with low temperature for precise tag placement.

    NOTE: QWEN-MT is a translation model, not a general LLM. Formatting transfer
    is NOT a translation task, so results may vary. If quality is poor, consider
    using a general LLM (GPT, Qwen-chat) for this pass instead."""
    in_b = original_tagged.count("<b>")
    in_i = original_tagged.count("<i>")
    print(
        f"  Formatting chunk {chunk_num}/{total} ({in_b} bold, {in_i} italic)...",
        end=" ", flush=True,
    )
    log.info("Format transfer chunk %d/%d: %d <b>, %d <i> to transfer",
             chunk_num, total, in_b, in_i)

    user_message = (
        f"[INSTRUCTION — do NOT translate, follow these rules:]\n"
        f"{FORMATTING_TRANSFER_INSTRUCTIONS}\n\n"
        f"[ORIGINAL ENGLISH TEXT WITH FORMATTING TAGS:]\n"
        f"{original_tagged}\n\n"
        f"[RUSSIAN TRANSLATION — add formatting tags to this text:]\n"
        f"{translated_plain}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=[
                {"role": "user", "content": user_message},
            ],
            extra_body={
                "translation_options": {
                    "source_lang": "English",
                    "target_lang": "Russian",
                },
            },
        )
        result = response.choices[0].message.content.strip()
        tokens = response.usage.total_tokens if response.usage else "?"

        out_b = result.count("<b>")
        out_i = result.count("<i>")
        print(f"OK (<b> {in_b}->{out_b}, <i> {in_i}->{out_i}, {tokens} tok)")
        log.info("Format transfer chunk %d/%d done: <b> %d->%d, <i> %d->%d, %s tokens",
                 chunk_num, total, in_b, out_b, in_i, out_i, tokens)

        # Sanity check: if zero tags came back, transfer failed — keep plain version
        if out_b == 0 and out_i == 0:
            log.warning("Format transfer returned no tags, keeping plain translation")
            return translated_plain

        # Validate: text content should be unchanged (ignoring tags)
        result_stripped = _strip_html_tags(result)
        if result_stripped.split() != translated_plain.split():
            log.warning(
                "Format transfer modified translation text for chunk %d/%d! "
                "Keeping formatted version but check quality.",
                chunk_num, total,
            )

        return result

    except Exception as e:
        log.error("Format transfer failed for chunk %d/%d: %s", chunk_num, total, e)
        print(f"WARNING ({e}), skipping")
        return translated_plain  # Fallback: unformatted translation is better than nothing


# ─────────────────────────── TRANSLATION CACHE ───────────────────────────


def _cache_path(input_path: str) -> str:
    """Get cache file path for a given input file."""
    stem = Path(input_path).stem
    return str(Path(input_path).parent / f".{stem}_qwen_translation_cache.json")


def save_translation_cache(
    cache_file: str, translated: list[str], total_chunks: int, metadata: dict,
):
    """Save translation progress to cache file (atomic write via tmp + rename)."""
    data = {
        "metadata": metadata,
        "total_chunks": total_chunks,
        "completed": len(translated),
        "translated": translated,
    }
    tmp = cache_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cache_file)
    log.debug("Cache saved: %d/%d chunks -> %s", len(translated), total_chunks, cache_file)


def load_translation_cache(cache_file: str) -> dict | None:
    """Load translation cache if it exists and is valid."""
    if not os.path.isfile(cache_file):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "translated" in data and isinstance(data["translated"], list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load cache %s: %s", cache_file, e)
    return None


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
    log.info("Saved %s", output_path)
    print(f"\nSaved: {output_path}")


# ─────────────────────────── MAIN ───────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="English -> Russian Literary Translator (QWEN-MT)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 eng_translator_qwen.py book.docx
  python3 eng_translator_qwen.py book.epub -o translation.docx
  python3 eng_translator_qwen.py book.md -o translation.docx
  python3 eng_translator_qwen.py book.docx --model qwen-mt-flash
  python3 eng_translator_qwen.py book.docx --chunk-size 3000 --context 5
  python3 eng_translator_qwen.py book.docx --glossary glossary.json
  python3 eng_translator_qwen.py book.docx --resume
        """,
    )
    parser.add_argument("input", help="Path to .epub, .docx, .md or .txt file")
    parser.add_argument(
        "-o", "--output", help="Output .docx path (default: input_translated_qwen.docx)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=MODEL,
        choices=["qwen-mt-plus", "qwen-mt-flash", "qwen-mt-lite", "qwen-mt-turbo"],
        help=f"QWEN-MT model (default: {MODEL}). "
             "plus = highest quality, flash = balanced, lite = fastest (31 lang), "
             "turbo = deprecated (use flash)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=MAX_CHARS_PER_CHUNK,
        help=f"Max chars per chunk (default: {MAX_CHARS_PER_CHUNK}). "
             "Note: QWEN-MT has 8192 token input limit",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=CONTEXT_PARAGRAPHS,
        help=f"Paragraphs from previous translation for context (default: {CONTEXT_PARAGRAPHS}, 0 = disable)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_REQUESTS,
        help=f"Delay between requests in seconds (default: {DELAY_BETWEEN_REQUESTS})",
    )
    parser.add_argument(
        "--glossary",
        type=str,
        default=None,
        help="Path to glossary JSON file",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume translation from cache (if previous run was interrupted)",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"File not found: {args.input}")

    # Selected model
    selected_model = args.model

    # Output path
    if args.output:
        output_path = args.output
    else:
        stem = Path(args.input).stem
        output_path = f"{stem}_translated_qwen.docx"

    # Glossary
    glossary = {}
    if args.glossary:
        if not os.path.isfile(args.glossary):
            sys.exit(f"Glossary not found: {args.glossary}")
        glossary = load_glossary(args.glossary)
        print(f"Glossary loaded: {len(glossary)} entries")

    # Extract
    print(f"Reading file: {args.input}")
    text = extract_text(args.input)
    print(f"   Extracted {len(text)} characters")

    if not text.strip():
        sys.exit("File is empty or text extraction failed.")

    # Chunk
    chunks = split_into_chunks(text, max_chars=args.chunk_size)
    print(f"Split into {len(chunks)} chunks (max {args.chunk_size} chars)")
    print(f"Context: {args.context} paragraphs from previous translation")
    print(f"Model: {selected_model}")
    print()

    # Estimate cost (rough — QWEN-MT pricing varies by model)
    estimated_input_tokens = len(text) * 0.8
    # Instructions add ~500 tokens per chunk
    estimated_input_tokens += len(chunks) * 500
    if args.context > 0:
        estimated_input_tokens *= 1.15
    estimated_output_tokens = len(text) * 0.8 * 1.5

    # QWEN-MT pricing (Global deployment, per 1M tokens)
    price_map = {
        "qwen-mt-plus": (0.259, 0.775),
        "qwen-mt-flash": (0.101, 0.280),
        "qwen-mt-lite": (0.086, 0.229),
        "qwen-mt-turbo": (0.101, 0.280),  # deprecated, same as flash
    }
    in_price, out_price = price_map.get(selected_model, (0.50, 2.00))
    estimated_cost = (estimated_input_tokens * in_price + estimated_output_tokens * out_price) / 1_000_000

    print(f"Estimated cost: ${estimated_cost:.3f} (approximate)")
    print(f"   (input ~{estimated_input_tokens:.0f} tokens, output ~{estimated_output_tokens:.0f} tokens)")
    print(f"   (QWEN-MT pricing is approximate, check DashScope for exact rates)")
    print()

    confirm = input("Continue? [Y/n]: ").strip().lower()
    if confirm == "n":
        sys.exit("Cancelled.")

    # Cache setup
    cache_file = _cache_path(args.input)
    cache_meta = {"input": args.input, "model": selected_model, "chunk_size": args.chunk_size}

    # Translate
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    translated = []
    start_chunk = 1

    # Resume from cache if requested
    if args.resume:
        cache = load_translation_cache(cache_file)
        if cache and cache.get("completed", 0) > 0:
            translated = cache["translated"]
            start_chunk = len(translated) + 1
            print(f"Resuming from cache: {len(translated)}/{len(chunks)} chunks already translated")
            log.info("Resumed from cache: %d/%d chunks", len(translated), len(chunks))
        else:
            print("No valid cache found, starting from scratch")
            log.info("No valid cache found, starting from scratch")

    for i in range(start_chunk, len(chunks) + 1):
        chunk = chunks[i - 1]

        prev = None
        if args.context > 0 and translated:
            prev = translated[-1]

        # Single pass: translate with formatting tags included
        # QWEN-MT is a pure translation model — it cannot follow formatting
        # transfer instructions (Pass 2). Instead, we send text WITH tags
        # and rely on rule 13 in TRANSLATION_INSTRUCTIONS to preserve them.
        result = translate_chunk(
            client,
            chunk,
            i,
            len(chunks),
            previous_translation=prev,
            glossary=glossary,
            context_paragraphs=args.context,
            model=selected_model,
        )

        translated.append(result)

        # Backup: save cache after each chunk
        save_translation_cache(cache_file, translated, len(chunks), cache_meta)

        if i < len(chunks):
            time.sleep(args.delay)

    # Save
    log.info("Saving translation to %s", output_path)
    save_to_docx(translated, output_path)

    # Clean up cache after successful save
    if os.path.isfile(cache_file):
        os.remove(cache_file)
        log.info("Translation cache removed after successful save")

    # Summary
    total_chars_in = sum(len(c) for c in chunks)
    total_chars_out = sum(len(c) for c in translated)
    print(f"\nSummary:")
    print(f"   Source text:  {total_chars_in:,} characters")
    print(f"   Translation:  {total_chars_out:,} characters")
    print(f"   Chunks:       {len(chunks)}")
    print(f"   Context:      {args.context} paragraphs between chunks")
    print(f"   Model:        {selected_model}")
    if glossary:
        print(f"   Glossary:     {len(glossary)} entries")
    print(f"   File:         {output_path}")


if __name__ == "__main__":
    main()
