#!/usr/bin/env python3
"""
Glossary Builder for Literary Translation
Extracts characters, terms, and locations from a novel via GPT,
builds a structured glossary JSON for use with eng_translator.py.
"""

import argparse
import json
import re
import sys
import time
import shutil
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import os
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта в sys.path
from common.logger import setup_logger
from common.api_keys import require_key
import logging

setup_logger(prefix="glossary_builder")
log = logging.getLogger("glossary_builder")

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    sys.exit("openai not installed. Run: pip install openai")

from translators.eng_translator import extract_text, split_into_chunks

# ─────────────────────────── CONFIG ───────────────────────────

API_KEY = os.getenv("OPENAI_API_KEY")  # проверяется в main() через require_key
MODEL = "gpt-5.1"
TEMPERATURE_EXTRACT = 0.3
TEMPERATURE_CONSOLIDATE = 0.2
MAX_CHARS_PER_CHUNK = 6000
DELAY_BETWEEN_REQUESTS = 1.5
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 5
MIN_OCCURRENCES = 2  # сущности, встречающиеся реже, в глоссарий не попадают

# ─────────────────────────── PROMPTS ───────────────────────────

EXTRACTION_SYSTEM_PROMPT = """You are a literary text analysis assistant specializing in fantasy/fiction works.
Analyze the following text excerpt and extract all named entities into three categories.

Return ONLY valid JSON with this exact structure:
{
  "characters": [
    {
      "original": "Name as it appears in text",
      "suggested_translation": "Russian transliteration",
      "gender": "m|f|unknown",
      "indeclinable": true|false,
      "aliases": [],
      "notes": "brief context (role, description)"
    }
  ],
  "terms": [
    {
      "original": "Term as it appears",
      "suggested_translation": "Russian translation",
      "category": "magic_system|rank|title|organization|item|creature|other",
      "notes": "brief context"
    }
  ],
  "locations": [
    {
      "original": "Place Name",
      "suggested_translation": "Russian transliteration or translation",
      "notes": "brief context"
    }
  ]
}

Guidelines:
- For gender: infer from pronouns used in the text (he/him -> m, she/her -> f, they/unknown -> unknown).
- For indeclinable: apply Russian phonetic rules. Female names ending in a consonant are usually indeclinable. Male names ending in a consonant ARE declinable. Names ending in -а/-я are declinable regardless of gender. When unsure, set false.
- For terms: only extract terms specific to this fictional world (magic systems, made-up ranks, special items, etc.), NOT common English words.
- For suggested_translation: transliterate proper names phonetically. For descriptive terms (e.g. "Shadow Guard"), provide a meaningful Russian translation.
- If no entities of a category are found, return an empty list for that category.
- Do NOT invent entities. Only extract what is explicitly present in the text."""

CONSOLIDATION_PROMPT_CHARACTERS = """You are a literary glossary editor. You will receive a raw list of CHARACTER entries
extracted from multiple chunks of a novel. Your task is to:

1. DEDUPLICATE: Merge entries that refer to the same character (same name, different casing,
   or one is an alias/nickname of another). Combine their notes and aliases.
2. RESOLVE CONFLICTS: If gender was "unknown" in some chunks but identified in others,
   use the identified gender. If translations differ, pick the most consistent one and
   put alternatives into the "alternatives" field.
3. STANDARDIZE: Ensure all Russian transliterations follow consistent rules.
4. MERGE ALIASES: If "Paw" and "Pawarit" refer to the same character, keep one entry
   with aliases.

Return ONLY a JSON object with a single key "characters" containing the consolidated list:
{
  "characters": [
    {
      "original": "Name",
      "translation": "Russian transliteration",
      "gender": "m|f|unknown",
      "indeclinable": true|false,
      "aliases": [],
      "alternatives": [],
      "notes": "combined context"
    }
  ]
}

Sort entries alphabetically by "original". Do NOT omit any characters."""

CONSOLIDATION_PROMPT_TERMS = """You are a literary glossary editor. You will receive a raw list of TERM entries
(magic systems, ranks, titles, organizations, items, creatures) extracted from multiple chunks of a novel.

Your task is to:
1. DEDUPLICATE: Merge entries that refer to the same term. Combine notes.
2. RESOLVE CONFLICTS: If translations differ, pick the best one and put alternatives into "alternatives".
3. STANDARDIZE: Ensure consistent Russian translations.

Return ONLY a JSON object with a single key "terms" containing the consolidated list:
{
  "terms": [
    {
      "original": "Term",
      "translation": "Russian translation",
      "category": "category",
      "alternatives": [],
      "notes": "combined context"
    }
  ]
}

Sort entries alphabetically by "original". Do NOT omit any terms."""

CONSOLIDATION_PROMPT_LOCATIONS = """You are a literary glossary editor. You will receive a raw list of LOCATION entries
extracted from multiple chunks of a novel.

Your task is to:
1. DEDUPLICATE: Merge entries that refer to the same place. Combine notes.
2. RESOLVE CONFLICTS: If translations differ, pick the best one and put alternatives into "alternatives".
3. STANDARDIZE: Ensure consistent Russian transliterations/translations.

Return ONLY a JSON object with a single key "locations" containing the consolidated list:
{
  "locations": [
    {
      "original": "Place",
      "translation": "Russian translation",
      "alternatives": [],
      "notes": "combined context"
    }
  ]
}

Sort entries alphabetically by "original". Do NOT omit any locations."""

# ─────────────────────────── API CALL WITH RETRY ───────────────────────────


def call_gpt_json(
    client: OpenAI,
    messages: list[dict],
    temperature: float = TEMPERATURE_EXTRACT,
    max_retries: int = MAX_RETRIES,
    max_output_tokens: int = 16000,
) -> tuple[dict | None, int]:
    """
    Call GPT expecting a JSON response. Retries with exponential backoff.
    Returns (parsed_dict, tokens_used) or (None, 0) on failure.
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_completion_tokens=max_output_tokens,
            )
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length":
                log.warning(
                    "GPT output truncated (hit token limit, attempt %d/%d)",
                    attempt,
                    max_retries,
                )
                if attempt < max_retries:
                    # retry with higher limit
                    max_output_tokens = min(max_output_tokens * 2, 64000)
                    log.info("Retrying with max_output_tokens=%d", max_output_tokens)
                    continue
                else:
                    log.error("Output still truncated after %d attempts", max_retries)
                    return None, 0

            content = response.choices[0].message.content.strip()
            result = json.loads(content)
            tokens = response.usage.total_tokens if response.usage else 0
            log.debug("GPT response: %d tokens, finish_reason=%s", tokens, finish_reason)
            return result, tokens

        except json.JSONDecodeError as e:
            log.warning(
                "Invalid JSON from GPT (attempt %d/%d): %s", attempt, max_retries, e
            )

        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "rate_limit" in error_str.lower():
                delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
                log.warning(
                    "Rate limited (attempt %d/%d), waiting %ds...",
                    attempt,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            elif any(code in error_str for code in ("500", "502", "503")):
                delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
                log.warning(
                    "Server error (attempt %d/%d), waiting %ds...",
                    attempt,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            else:
                log.error("API error (attempt %d/%d): %s", attempt, max_retries, e)

        if attempt < max_retries:
            delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
            log.info("Retrying in %ds...", delay)
            time.sleep(delay)

    log.error("All %d attempts failed", max_retries)
    return None, 0


# ─────────────────────────── PHASE 1: EXTRACTION ───────────────────────────


def extract_entities_from_chunk(
    client: OpenAI, chunk: str, chunk_num: int, total: int
) -> dict | None:
    """Extract characters, terms, locations from a single chunk."""
    log.info(
        "Extracting entities from chunk %d/%d (%d chars)...",
        chunk_num,
        total,
        len(chunk),
    )
    print(f"  Chunk {chunk_num}/{total} ({len(chunk)} chars)...", end=" ", flush=True)

    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Analyze this text excerpt (chunk {chunk_num}/{total}):\n\n{chunk}",
        },
    ]

    result, tokens = call_gpt_json(client, messages, temperature=TEMPERATURE_EXTRACT)
    if result is None:
        print("FAIL")
        log.error("Failed to extract from chunk %d/%d", chunk_num, total)
        return None

    for key in ("characters", "terms", "locations"):
        if key not in result:
            result[key] = []

    chars = len(result["characters"])
    terms = len(result["terms"])
    locs = len(result["locations"])
    print(f"OK ({chars}ch, {terms}t, {locs}l, {tokens} tok)")
    log.info(
        "Chunk %d/%d: %d characters, %d terms, %d locations (%d tokens)",
        chunk_num,
        total,
        chars,
        terms,
        locs,
        tokens,
    )
    return result


# ─────────────────────────── AGGREGATION ───────────────────────────


def aggregate_raw_results(results: list[dict]) -> dict:
    """Combine all per-chunk results, doing basic deduplication."""
    seen_chars: dict[str, dict] = {}
    seen_terms: dict[str, dict] = {}
    seen_locs: dict[str, dict] = {}

    for result in results:
        for char in result.get("characters", []):
            key = char.get("original", "").strip().lower()
            if not key:
                continue
            if key in seen_chars:
                existing = seen_chars[key]
                existing["chunk_appearances"] = existing.get("chunk_appearances", 1) + 1
                # prefer identified gender over unknown
                if (
                    existing.get("gender") == "unknown"
                    and char.get("gender") != "unknown"
                ):
                    existing["gender"] = char["gender"]
                # merge aliases
                existing_aliases = set(existing.get("aliases", []))
                new_aliases = set(char.get("aliases", []))
                existing["aliases"] = list(existing_aliases | new_aliases)
                # track alternative translations
                new_trans = char.get("suggested_translation", "")
                existing_trans = existing.get("suggested_translation", "")
                if new_trans and new_trans != existing_trans:
                    alts = existing.setdefault("alternatives", [])
                    if new_trans not in alts and new_trans != existing_trans:
                        alts.append(new_trans)
                # append notes
                if char.get("notes") and char["notes"] not in existing.get(
                    "notes", ""
                ):
                    existing["notes"] = f"{existing.get('notes', '')}; {char['notes']}"
            else:
                char.setdefault("alternatives", [])
                char.setdefault("chunk_appearances", 1)
                seen_chars[key] = char

        for term in result.get("terms", []):
            key = term.get("original", "").strip().lower()
            if not key:
                continue
            if key in seen_terms:
                existing = seen_terms[key]
                existing["chunk_appearances"] = existing.get("chunk_appearances", 1) + 1
                new_trans = term.get("suggested_translation", "")
                existing_trans = existing.get("suggested_translation", "")
                if new_trans and new_trans != existing_trans:
                    alts = existing.setdefault("alternatives", [])
                    if new_trans not in alts:
                        alts.append(new_trans)
                if term.get("notes") and term["notes"] not in existing.get(
                    "notes", ""
                ):
                    existing["notes"] = f"{existing.get('notes', '')}; {term['notes']}"
            else:
                term.setdefault("alternatives", [])
                term.setdefault("chunk_appearances", 1)
                seen_terms[key] = term

        for loc in result.get("locations", []):
            key = loc.get("original", "").strip().lower()
            if not key:
                continue
            if key in seen_locs:
                existing = seen_locs[key]
                existing["chunk_appearances"] = existing.get("chunk_appearances", 1) + 1
                new_trans = loc.get("suggested_translation", "")
                existing_trans = existing.get("suggested_translation", "")
                if new_trans and new_trans != existing_trans:
                    alts = existing.setdefault("alternatives", [])
                    if new_trans not in alts:
                        alts.append(new_trans)
                if loc.get("notes") and loc["notes"] not in existing.get("notes", ""):
                    existing["notes"] = f"{existing.get('notes', '')}; {loc['notes']}"
            else:
                loc.setdefault("alternatives", [])
                loc.setdefault("chunk_appearances", 1)
                seen_locs[key] = loc

    # Sort alphabetically by original
    return {
        "characters": sorted(seen_chars.values(), key=lambda x: x.get("original", "").lower()),
        "terms": sorted(seen_terms.values(), key=lambda x: x.get("original", "").lower()),
        "locations": sorted(seen_locs.values(), key=lambda x: x.get("original", "").lower()),
    }


# ─────────────────────────── FREQUENCY (OCCURRENCE COUNTING) ───────────────────────────


def _entity_regex(name: str) -> re.Pattern:
    """Word-boundary, case-insensitive pattern for an entity name."""
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)


def _entry_names(entry: dict) -> list[str]:
    """All searchable names of an entry: original + aliases."""
    names = [entry.get("original", "")] + list(entry.get("aliases", []) or [])
    return [n.strip() for n in names if n and len(n.strip()) >= 2]


def annotate_occurrences(glossary: dict, source_text: str | None) -> None:
    """Set entry["occurrences"] for every entry.

    If source_text is available — exact whole-word count of original + aliases.
    Otherwise falls back to chunk_appearances (how many chunks mentioned the entity).
    """
    for category in ALL_CATEGORIES:
        for entry in glossary.get(category, []):
            if source_text is not None:
                # Только имена собственные — генерик-алиасы раздувают счётчик
                total = sum(
                    len(_entity_regex(name).findall(source_text))
                    for name in _proper_entry_names(entry)
                )
                entry["occurrences"] = total
            else:
                entry["occurrences"] = entry.get("chunk_appearances", entry.get("occurrences", 0))
            entry.pop("chunk_appearances", None)


def filter_by_occurrences(glossary: dict, min_occurrences: int) -> list[tuple[str, str, int]]:
    """Drop entries seen fewer than min_occurrences times. Returns the dropped list."""
    dropped = []
    for category in ALL_CATEGORIES:
        kept = []
        for entry in glossary.get(category, []):
            if entry.get("occurrences", 0) >= min_occurrences:
                kept.append(entry)
            else:
                dropped.append((category, entry.get("original", "?"), entry.get("occurrences", 0)))
        glossary[category] = kept
    return dropped


def sort_by_frequency(glossary: dict) -> None:
    """Sort every category by occurrences (descending), then alphabetically."""
    for category in ALL_CATEGORIES:
        glossary.get(category, []).sort(
            key=lambda e: (-e.get("occurrences", 0), e.get("original", "").lower())
        )


def build_occurrence_map(glossary: dict) -> dict[str, int]:
    """Map lowercase name (original or alias) -> occurrences, for restoring counts
    after GPT consolidation (which may drop the field)."""
    occ_map: dict[str, int] = {}
    for category in ALL_CATEGORIES:
        for entry in glossary.get(category, []):
            count = entry.get("occurrences", 0)
            for name in _entry_names(entry):
                key = name.lower()
                occ_map[key] = max(occ_map.get(key, 0), count)
    return occ_map


def restore_occurrences(glossary: dict, occ_map: dict[str, int]) -> None:
    """Restore occurrences after consolidation using the pre-consolidation map."""
    for category in ALL_CATEGORIES:
        for entry in glossary.get(category, []):
            if entry.get("occurrences"):
                continue
            counts = [occ_map.get(name.lower(), 0) for name in _entry_names(entry)]
            entry["occurrences"] = max(counts) if counts else 0


# ─────────────────────────── PHASE 2: CONSOLIDATION ───────────────────────────


CONSOLIDATION_BATCH_SIZE = 50


def _consolidate_batch(
    client: OpenAI, category: str, entries: list[dict], system_prompt: str,
    batch_label: str = "",
) -> tuple[list[dict] | None, int]:
    """Consolidate a single batch of entries. Returns (entries, tokens) or (None, 0)."""
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(entries, ensure_ascii=False, indent=2),
        },
    ]

    result, tokens = call_gpt_json(
        client, messages, temperature=TEMPERATURE_CONSOLIDATE
    )

    if result is None:
        return None, 0

    consolidated = result.get(category, [])
    if not consolidated and entries:
        log.warning(
            "GPT returned empty %s%s but had %d entries",
            category,
            batch_label,
            len(entries),
        )
        return None, tokens

    # Validate: warn if GPT dropped entries
    if len(consolidated) < len(entries) * 0.5:
        log.warning(
            "GPT %s%s: input %d entries, output %d (lost >50%%, likely truncated)",
            category,
            batch_label,
            len(entries),
            len(consolidated),
        )

    return consolidated, tokens


def _consolidate_category(
    client: OpenAI, category: str, entries: list[dict], system_prompt: str
) -> list[dict]:
    """Consolidate a single category via GPT. Splits into batches if needed."""
    if not entries:
        return []

    log.info("Consolidating %d %s entries...", len(entries), category)

    # Small enough — single call
    if len(entries) <= CONSOLIDATION_BATCH_SIZE:
        print(f"  {category}: {len(entries)} entries...", end=" ", flush=True)
        consolidated, tokens = _consolidate_batch(client, category, entries, system_prompt)
        if consolidated is None:
            print("FAIL (keeping local)")
            log.warning("Consolidation failed for %s, using local aggregation", category)
            return None
        consolidated = sorted(consolidated, key=lambda x: x.get("original", "").lower())
        print(f"OK ({len(consolidated)}, {tokens} tok)")
        log.info("Consolidated %s: %d entries (%d tokens)", category, len(consolidated), tokens)
        return consolidated

    # Large category — split into batches
    batches = [
        entries[i : i + CONSOLIDATION_BATCH_SIZE]
        for i in range(0, len(entries), CONSOLIDATION_BATCH_SIZE)
    ]
    print(f"  {category}: {len(entries)} entries in {len(batches)} batches...")

    all_consolidated = []
    total_tokens = 0
    failed_batches = 0

    for batch_idx, batch in enumerate(batches, 1):
        batch_label = f" batch {batch_idx}/{len(batches)}"
        print(f"    batch {batch_idx}/{len(batches)} ({len(batch)} entries)...", end=" ", flush=True)

        consolidated, tokens = _consolidate_batch(
            client, category, batch, system_prompt, batch_label
        )
        total_tokens += tokens

        if consolidated is not None:
            all_consolidated.extend(consolidated)
            print(f"OK ({len(consolidated)}, {tokens} tok)")
        else:
            # Fallback: keep original batch entries with suggested_translation renamed
            for entry in batch:
                if "suggested_translation" in entry:
                    entry["translation"] = entry.pop("suggested_translation")
            all_consolidated.extend(batch)
            failed_batches += 1
            print(f"FAIL (keeping {len(batch)} local)")

        if batch_idx < len(batches):
            time.sleep(DELAY_BETWEEN_REQUESTS)

    if failed_batches > 0:
        log.warning(
            "%d/%d batches failed for %s, used local fallback",
            failed_batches,
            len(batches),
            category,
        )

    all_consolidated = sorted(
        all_consolidated, key=lambda x: x.get("original", "").lower()
    )
    print(f"  {category} total: {len(all_consolidated)} entries ({total_tokens} tok)")
    log.info(
        "Consolidated %s: %d entries in %d batches (%d tokens, %d failed)",
        category,
        len(all_consolidated),
        len(batches),
        total_tokens,
        failed_batches,
    )
    return all_consolidated


def consolidate_glossary(client: OpenAI, aggregated: dict) -> dict:
    """Consolidate each category separately via GPT."""
    entity_count = (
        len(aggregated["characters"])
        + len(aggregated["terms"])
        + len(aggregated["locations"])
    )
    log.info("Consolidating %d total entities via GPT (per-category)...", entity_count)
    print(f"\nConsolidating {entity_count} entities via GPT (per-category):")

    # Finalize a copy of aggregated as fallback
    fallback = _finalize_aggregated_copy(aggregated)

    categories = [
        ("characters", CONSOLIDATION_PROMPT_CHARACTERS),
        ("terms", CONSOLIDATION_PROMPT_TERMS),
        ("locations", CONSOLIDATION_PROMPT_LOCATIONS),
    ]

    result = {}
    for cat_key, prompt in categories:
        entries = aggregated.get(cat_key, [])
        if not entries:
            result[cat_key] = []
            continue

        consolidated = _consolidate_category(client, cat_key, entries, prompt)
        if consolidated is not None:
            result[cat_key] = consolidated
        else:
            # fallback to locally aggregated + finalized
            result[cat_key] = fallback.get(cat_key, [])
            log.info("Using local fallback for %s (%d entries)", cat_key, len(result[cat_key]))

        time.sleep(DELAY_BETWEEN_REQUESTS)

    chars = len(result.get("characters", []))
    terms = len(result.get("terms", []))
    locs = len(result.get("locations", []))
    print(f"  Total: {chars}ch, {terms}t, {locs}l")
    log.info(
        "Consolidation complete: %d characters, %d terms, %d locations",
        chars,
        terms,
        locs,
    )
    return result


def _finalize_aggregated(aggregated: dict) -> dict:
    """Rename suggested_translation -> translation in locally aggregated data (mutates)."""
    for category in ALL_CATEGORIES:
        for entry in aggregated.get(category, []):
            if "suggested_translation" in entry:
                entry["translation"] = entry.pop("suggested_translation")
    return aggregated


def _finalize_aggregated_copy(aggregated: dict) -> dict:
    """Rename suggested_translation -> translation on a deep copy (does not mutate original)."""
    import copy
    data = copy.deepcopy(aggregated)
    return _finalize_aggregated(data)


# ─────────────────────────── GENDER FROM PRONOUN STATISTICS ───────────────────────────

# Род по статистике he/she рядом с именем — детерминированно, по всей книге,
# ноль токенов. Надёжнее, чем догадка LLM по одному чанку.

MALE_PRONOUN_RE = re.compile(r"\b(?:he|him|his|himself)\b", re.IGNORECASE)
FEMALE_PRONOUN_RE = re.compile(r"\b(?:she|her|hers|herself)\b", re.IGNORECASE)
PRONOUN_WINDOW = 150  # символов после упоминания имени
MIN_PRONOUN_SIGNAL = 3  # минимум мужских местоимений для вывода «мужчина»
FEMALE_MIN_SIGNAL = 2  # женскому сигналу хватает меньшего порога (см. ниже)
DOMINANCE_RATIO = 2.0  # во сколько раз мужской род должен перевешивать

# ПРАВИЛО ПОЛЬЗОВАТЕЛЯ: женский род имеет приоритет. К персонажам неизвестного
# пола в тексте по умолчанию обращаются «по-мужски», поэтому мужские сигналы
# ненадёжны. Женские обращения почти не бывают ошибкой: есть женский сигнал —
# персонаж женский, даже если мужских упоминаний в сумме больше.

# Категории, содержащие записи (в т.ч. новая generics — генерики вроде
# «daughter», «the goddess», отделённые от настоящих имён)
ALL_CATEGORIES = ("characters", "terms", "locations", "generics")
CHARACTER_LIKE = ("characters", "generics")  # у этих записей есть род

# Генерики, которые часто попадают в алиасы («saint», «ghost», «bishop»...).
# Для подсчёта рода и частот они ЯД: встречаются по всей книге рядом с кем угодно.
GENERIC_NAME_WORDS = {
    "the", "a", "an", "saint", "saintess", "bishop", "priest", "priestess",
    "professor", "doctor", "teacher", "master", "mister", "miss", "mrs", "lady",
    "lord", "king", "queen", "prince", "princess", "knight", "hunter", "artist",
    "painter", "ghost", "collector", "attendant", "attendants", "protagonist",
    "boy", "girl", "man", "woman", "it", "brother", "sister", "father", "mother",
    "uncle", "aunt", "captain", "general", "chairman", "director", "president",
    "god", "goddess", "deity", "demon", "devil", "angel", "spirit", "monster",
    "beast", "child", "kid", "elder", "adult", "leader", "chief", "guard",
}


def _is_proper_name(name: str) -> bool:
    """True, если имя похоже на имя собственное, а не на генерик
    («Bishop Aram» — да, «fallen saint», «Saintess», «it» — нет)."""
    words = [w.strip(".,'\"“”‘’") for w in name.split()]
    words = [w for w in words if w]
    capitalized = [w for w in words if w[0].isupper()]
    if not capitalized:
        return False
    return any(w.lower() not in GENERIC_NAME_WORDS for w in capitalized)


def _proper_entry_names(entry: dict) -> list[str]:
    """Имена записи, пригодные для подсчёта частот и рода (без генериков).
    Если ничего не осталось — только original (fallback)."""
    names = [n for n in _entry_names(entry) if _is_proper_name(n)]
    if names:
        return names
    original = entry.get("original", "").strip()
    return [original] if len(original) >= 2 else []


# Род из notes: экстракция пишет туда контекст («Girl from the slums...») —
# это осмысленное суждение модели, кросс-проверяем им статистику местоимений.
# Только существительные — местоимения (she/her/he/his) в notes слишком часто
# относятся к ДРУГИМ персонажам («gives her the key») и дают ложный род
_NOTES_FEMALE_RE = re.compile(
    r"\b(?:girl|woman|female|lady|heroine|priestess|saintess|goddess|daughter|sister|mother|aunt|queen|princess"
    r"|дев(?:ушка|очка|а)|женщина|героиня|жрица|святая|богиня|дочь|сестра|мать|королева|принцесса)\b",
    re.IGNORECASE)
_NOTES_MALE_RE = re.compile(
    r"\b(?:boy|man|male|hero|priest|monk|son|brother|father|uncle|king|prince"
    r"|юноша|мужчина|парень|герой|жрец|святой|бог|сын|брат|отец|король|принц)\b",
    re.IGNORECASE)


def gender_from_notes(entry: dict) -> str | None:
    notes = entry.get("notes") or ""
    if not notes:
        return None
    f_hits = len(_NOTES_FEMALE_RE.findall(notes))
    m_hits = len(_NOTES_MALE_RE.findall(notes))
    # Приоритет женского: хоть один женский сигнал в notes — персонаж женский
    if f_hits >= 1:
        return "f"
    if m_hits >= 1:
        return "m"
    return None


def infer_gender_from_pronouns(
    text: str, names: list[str], other_names: list[str] | None = None
) -> str | None:
    """Considers pronouns in a window after each mention of the name across the
    whole text. The window is cut at the first mention of ANOTHER character —
    their pronouns must not pollute the signal. Returns 'm'/'f' when confident."""
    other_rx = None
    if other_names:
        alts = [re.escape(n) for n in other_names if len(n) >= 2]
        if alts:
            other_rx = re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)

    male = 0
    female = 0
    for name in names:
        if len(name) < 2:
            continue
        for match in _entity_regex(name).finditer(text):
            window = text[match.end():match.end() + PRONOUN_WINDOW]
            if other_rx:
                cut = other_rx.search(window)
                if cut:
                    window = window[:cut.start()]
            male += len(MALE_PRONOUN_RE.findall(window))
            female += len(FEMALE_PRONOUN_RE.findall(window))
    # Женский сигнал побеждает независимо от количества мужских (см. правило выше)
    if female >= FEMALE_MIN_SIGNAL:
        return "f"
    if male >= MIN_PRONOUN_SIGNAL and male >= female * DOMINANCE_RATIO:
        return "m"
    return None


def apply_pronoun_genders(glossary: dict, text: str | None) -> int:
    """Set/override character gender: pronoun statistics (только по именам
    собственным — генерик-алиасы отравляют сигнал) + кросс-проверка по notes.
    При конфликте побеждают notes: это контекстное суждение модели, а статистика
    может быть отравлена. Returns changed count."""
    characters = [(cat, e) for cat in CHARACTER_LIKE for e in glossary.get(cat, [])]
    all_names = [(i, n) for i, (_, e) in enumerate(characters) for n in _proper_entry_names(e)]
    changed = 0
    for i, (cat, entry) in enumerate(characters):
        stats_gender = None
        # Статистика местоимений — только для настоящих имён: рядом с генериком
        # («humans», «child») местоимения относятся к кому угодно
        if text is not None and cat == "characters":
            other_names = [n for j, n in all_names if j != i]
            stats_gender = infer_gender_from_pronouns(
                text, _proper_entry_names(entry), other_names
            )
        notes_gender = gender_from_notes(entry)

        # Приоритет женского рода: любой женский сигнал побеждает
        if "f" in (stats_gender, notes_gender):
            final = "f"
        elif stats_gender and notes_gender and stats_gender != notes_gender:
            final = notes_gender
        else:
            final = stats_gender or notes_gender

        if final and final != entry.get("gender"):
            log.info(
                "Gender: %s %s -> %s",
                entry.get("original"), entry.get("gender"), final,
            )
            entry["gender"] = final
            changed += 1
    return changed


def split_generic_characters(glossary: dict) -> int:
    """Разделяет персонажей на настоящие имена и генерики («daughter»,
    «the goddess», «сын»...). Генерики уходят в отдельную секцию `generics` —
    для перевода они работают так же, но не путаются под ногами при чтении
    и не участвуют в матрице ты/вы. Returns число перемещённых."""
    proper = []
    generics = list(glossary.get("generics", []))
    existing_generic_keys = {e.get("original", "").casefold() for e in generics}
    moved = 0
    for entry in glossary.get("characters", []):
        if _is_proper_name(entry.get("original", "")):
            proper.append(entry)
        elif entry.get("original", "").casefold() not in existing_generic_keys:
            generics.append(entry)
            moved += 1
        else:
            moved += 1  # дубль уже существующего генерика — просто выбрасываем
    glossary["characters"] = proper
    glossary["generics"] = generics
    return moved


def tidy_aliases(glossary: dict) -> int:
    """Чистка алиасов и alternatives: обрамляющие кавычки, дубли (без учёта
    регистра), у персонажей — выброс генерик-алиасов («saint», «it», «the ghost»),
    которые ломают частоты, род и заставляют переводить генерик как имя.
    Returns число выброшенных генерик-алиасов."""
    removed = 0
    for category in ALL_CATEGORIES:
        for entry in glossary.get(category, []):
            original_key = entry.get("original", "").casefold()
            seen = {original_key}
            clean = []
            for alias in entry.get("aliases") or []:
                alias = alias.strip().strip("'\"“”‘’").strip()
                if not alias or alias.casefold() in seen:
                    continue
                if category == "characters" and not _is_proper_name(alias):
                    log.info(
                        "Dropped generic alias '%s' from %s",
                        alias, entry.get("original"),
                    )
                    removed += 1
                    continue
                seen.add(alias.casefold())
                clean.append(alias)
            entry["aliases"] = sorted(clean)

            seen_alt = {(entry.get("translation") or "").casefold()}
            alts = []
            for alt in entry.get("alternatives") or []:
                alt = alt.strip().strip("'\"“”‘’").strip()
                if alt and alt.casefold() not in seen_alt:
                    seen_alt.add(alt.casefold())
                    alts.append(alt)
            entry["alternatives"] = alts
    return removed


# ─────────────────────────── DECLENSION BY RUSSIAN MORPHOLOGY ───────────────────────────

# Склоняемость и пример родительного падежа — по правилам русской морфологии.
# В глоссарии храним готовый образец («Паварит, р.п. Паварита»): модель надёжнее
# подражает примеру, чем применяет абстрактное правило.

_RU_VOWELS = "аеёиоуыэюя"


def infer_declension(name_ru: str, gender: str) -> tuple[bool, str | None]:
    """Return (indeclinable, genitive_example) for a Russian transliterated name.
    Rules:
      - ends in -а: declines (Анна — Анны; после г/к/х/ж/ч/ш/щ — и)
      - ends in -я: declines (Ася — Аси)
      - ends in consonant: male declines (Паварит — Паварита), female does not (Элис)
      - ends in -ь: male declines (Игорь — Игоря), female — не трогаем (позволяем LLM)
      - ends in other vowel (о/е/и/у/ю/э/ы/ё): indeclinable (Гирё, Хару)
    """
    word = name_ru.strip().split()[0] if name_ru.strip() else ""
    if len(word) < 2 or not re.match(r"^[А-ЯЁа-яё]+$", word):
        return False, None  # не кириллица или слишком короткое — не решаем

    last = word[-1].lower()
    if last == "а":
        prev = word[-2].lower()
        ending = "и" if prev in "гкхжчшщ" else "ы"
        return False, word[:-1] + ending
    if last == "я":
        return False, word[:-1] + "и"
    if last == "ь":
        if gender == "m":
            return False, word[:-1] + "я"
        return True, None
    if last not in _RU_VOWELS:  # согласная
        if gender == "f":
            return True, None
        return False, word + "а"
    return True, None  # о, е, ё, и, у, ы, э, ю


def apply_declension_rules(glossary: dict) -> int:
    """Set indeclinable for characters by morphology rules.
    ПРАВИЛО ПОЛЬЗОВАТЕЛЯ: автоматика НЕ генерирует genitive — только вручную
    вписанные образцы склонения. Существующие genitive не трогаем никогда.
    Returns changed count."""
    changed = 0
    for entry in glossary.get("characters", []):
        translation = entry.get("translation") or entry.get("suggested_translation") or ""
        if not translation:
            continue
        indeclinable, _ = infer_declension(translation, entry.get("gender", "unknown"))
        if entry.get("indeclinable") != indeclinable:
            entry["indeclinable"] = indeclinable
            changed += 1
    return changed


# ─────────────────────────── PHASE 3: CROSS-BATCH DEDUP ───────────────────────────

# Консолидация идёт батчами по 50 записей — дубли, попавшие в разные батчи,
# модель слить не могла (не видела их вместе). Финальная проходка отправляет
# ТОЛЬКО списки имён (без заметок) одной дешёвой командой на категорию.

DEDUP_SYSTEM_PROMPT = """You are a literary glossary editor. You receive a numbered list of entity names
(with their aliases after "/") from ONE category of a novel glossary.
Some entries may refer to the SAME entity: nickname vs full name ("Paw" / "Pawarit"),
title vs name ("Doctor Yu" / "Yu An"), spelling variants.

Return ONLY JSON: {"groups": [[1, 5], [2, 7, 9]]}
Each group lists numbers of entries that are the same entity. Only groups of 2+.
No duplicates found -> {"groups": []}.

Be CONSERVATIVE: group only when confident. Do NOT group different characters
from the same family, or similar-sounding but distinct names."""


def _merge_entry_into(primary: dict, duplicate: dict) -> None:
    """Merge duplicate entry into primary: names -> aliases, translations -> alternatives."""
    primary_original = primary.get("original", "").lower()
    aliases = {a for a in (primary.get("aliases") or []) if a}
    for name in [duplicate.get("original", "")] + list(duplicate.get("aliases") or []):
        if name and name.lower() != primary_original and name not in aliases:
            aliases.add(name)
    primary["aliases"] = sorted(aliases)

    alternatives = {a for a in (primary.get("alternatives") or []) if a}
    dup_translation = duplicate.get("translation", "")
    if dup_translation and dup_translation != primary.get("translation"):
        alternatives.add(dup_translation)
    alternatives.update(a for a in (duplicate.get("alternatives") or []) if a)
    primary["alternatives"] = sorted(
        a for a in alternatives if a != primary.get("translation")
    )

    if duplicate.get("notes") and duplicate["notes"] not in (primary.get("notes") or ""):
        primary["notes"] = f"{primary.get('notes', '')}; {duplicate['notes']}".strip("; ")

    if primary.get("gender") in (None, "unknown") and duplicate.get("gender") not in (None, "unknown"):
        primary["gender"] = duplicate["gender"]
        primary.setdefault("indeclinable", duplicate.get("indeclinable", False))


def dedup_category(client: OpenAI, category: str, entries: list[dict]) -> tuple[list[dict], int]:
    """Find and merge duplicate entries across the whole category.
    Sends only name lists to GPT (cheap). Returns (entries, merged_count)."""
    if len(entries) < 2:
        return entries, 0

    lines = []
    for i, entry in enumerate(entries, 1):
        names = [entry.get("original", "")] + list(entry.get("aliases") or [])
        lines.append(f"{i}. " + " / ".join(n for n in names if n))

    messages = [
        {"role": "system", "content": DEDUP_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
    result, tokens = call_gpt_json(client, messages, temperature=0.1)
    if result is None:
        log.warning("Dedup pass failed for %s, keeping entries as-is", category)
        return entries, 0

    merged_away: set[int] = set()
    for group in result.get("groups", []):
        if not isinstance(group, list):
            continue
        idxs = [
            i - 1 for i in group
            if isinstance(i, int) and 1 <= i <= len(entries) and (i - 1) not in merged_away
        ]
        if len(idxs) < 2:
            continue
        primary_idx = max(idxs, key=lambda i: entries[i].get("occurrences", 0))
        for i in idxs:
            if i == primary_idx:
                continue
            log.info(
                "Dedup %s: '%s' merged into '%s'",
                category,
                entries[i].get("original"),
                entries[primary_idx].get("original"),
            )
            _merge_entry_into(entries[primary_idx], entries[i])
            merged_away.add(i)

    if merged_away:
        entries = [e for i, e in enumerate(entries) if i not in merged_away]
    return entries, len(merged_away)


def dedup_glossary(client: OpenAI, glossary: dict) -> int:
    """Run the cross-batch dedup pass on every category. Returns merged count."""
    total_merged = 0
    for category in ALL_CATEGORIES:
        entries = glossary.get(category, [])
        if len(entries) < 2:
            continue
        print(f"  {category}: {len(entries)} entries...", end=" ", flush=True)
        deduped, merged = dedup_category(client, category, entries)
        glossary[category] = deduped
        total_merged += merged
        print(f"OK (слито дублей: {merged})")
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return total_merged


# ─────────────────────────── PHASE 4: ТЫ/ВЫ MATRIX ───────────────────────────

# Английское "you" не говорит, на «ты» персонажи или на «вы». Один вызов модели
# по диалоговым цитатам строит матрицу обращений между главными персонажами.
# Переводчик подставляет её в промпт, когда оба персонажа в чанке.

RELATIONS_TOP_CHARACTERS = 10
RELATIONS_MAX_EXCERPTS = 40
RELATIONS_MAX_CHARS = 15000

RELATIONS_SYSTEM_PROMPT = """You are a Russian literary translation consultant. Russian distinguishes
informal ты and formal вы where English only has "you". You receive:
1. A list of main characters of a novel.
2. Dialogue excerpts from the ENGLISH source involving these characters.

For each PAIR of characters that talk to each other in the excerpts, decide how
they would address each other in a natural Russian translation, based on their
relationship (age, status, intimacy, hostility, formality of speech).

Return ONLY JSON:
{"relations": [
  {"a": "Name1", "b": "Name2", "a_to_b": "ты"|"вы", "b_to_a": "ты"|"вы",
   "note": "brief reason; mention if the address changes mid-story and when"}
]}

Rules: close friends/family/enemies in combat -> ты; strangers, subordinates to
superiors, formal settings -> вы; asymmetry is common (boss ты, subordinate вы).
Only include pairs actually present in the excerpts. If unsure, prefer вы."""


def collect_dialogue_excerpts(text: str, names: list[str]) -> list[str]:
    """Paragraphs that look like dialogue and mention at least two main characters."""
    name_regexes = {n: _entity_regex(n) for n in names if len(n) >= 2}
    excerpts = []
    total_chars = 0
    for para in text.split("\n\n"):
        if len(excerpts) >= RELATIONS_MAX_EXCERPTS or total_chars >= RELATIONS_MAX_CHARS:
            break
        if '"' not in para and "“" not in para and "'" not in para:
            continue
        mentioned = [n for n, rx in name_regexes.items() if rx.search(para)]
        if len(mentioned) >= 2:
            excerpt = para[:800]
            excerpts.append(excerpt)
            total_chars += len(excerpt)
    return excerpts


def build_relations(client: OpenAI, text: str, characters: list[dict]) -> list[dict]:
    """Build the ты/вы matrix for top characters. Returns list of relation dicts."""
    top = sorted(characters, key=lambda e: -e.get("occurrences", 0))[:RELATIONS_TOP_CHARACTERS]
    names = [e.get("original", "") for e in top if e.get("original")]
    if len(names) < 2:
        return []

    excerpts = collect_dialogue_excerpts(text, names)
    if not excerpts:
        log.info("No dialogue excerpts found for relations matrix")
        return []

    char_list = "\n".join(
        f"- {e.get('original')} ({e.get('translation', '?')}, {e.get('gender', '?')}): "
        f"{(e.get('notes') or '')[:150]}"
        for e in top
    )
    user_message = (
        f"CHARACTERS:\n{char_list}\n\nDIALOGUE EXCERPTS:\n\n" + "\n---\n".join(excerpts)
    )
    messages = [
        {"role": "system", "content": RELATIONS_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    result, tokens = call_gpt_json(client, messages, temperature=0.2)
    if result is None:
        log.warning("Relations matrix build failed")
        return []

    valid_names = {n.lower() for n in names}
    relations = []
    for rel in result.get("relations", []):
        if not isinstance(rel, dict):
            continue
        a, b = rel.get("a", ""), rel.get("b", "")
        if a.lower() in valid_names and b.lower() in valid_names and a.lower() != b.lower():
            if rel.get("a_to_b") in ("ты", "вы") and rel.get("b_to_a") in ("ты", "вы"):
                relations.append({
                    "a": a, "b": b,
                    "a_to_b": rel["a_to_b"], "b_to_a": rel["b_to_a"],
                    "note": (rel.get("note") or "")[:300],
                })
    log.info("Relations matrix: %d pairs (%d tokens)", len(relations), tokens)
    return relations


# ─────────────────────────── MERGE WITH EXISTING ───────────────────────────


def merge_with_existing(new_glossary: dict, existing_path: str) -> dict:
    """Merge new glossary entries with an existing glossary file.
    Existing entries take priority (already user-approved)."""
    if not os.path.isfile(existing_path):
        log.warning("Existing glossary not found: %s, skipping merge", existing_path)
        return new_glossary

    with open(existing_path, "r", encoding="utf-8") as f:
        existing = json.load(f)

    added = 0
    for category in ALL_CATEGORIES:
        existing_keys = {
            entry.get("original", "").lower()
            for entry in existing.get(category, [])
        }
        for entry in new_glossary.get(category, []):
            if entry.get("original", "").lower() not in existing_keys:
                existing.setdefault(category, []).append(entry)
                added += 1
                log.info(
                    "New %s entry from merge: %s", category, entry.get("original")
                )

        existing[category] = sorted(
            existing.get(category, []),
            key=lambda x: x.get("original", "").lower(),
        )

    # relations: существующие пары в приоритете, новые доливаются
    existing_pairs = {
        frozenset((r.get("a", "").lower(), r.get("b", "").lower()))
        for r in existing.get("relations", [])
    }
    for rel in new_glossary.get("relations", []):
        pair = frozenset((rel.get("a", "").lower(), rel.get("b", "").lower()))
        if pair not in existing_pairs:
            existing.setdefault("relations", []).append(rel)
            added += 1

    log.info("Merged: %d new entries added", added)
    print(f"Merged with {existing_path}: {added} new entries added")
    return existing


# ─────────────────────────── BACKUP ───────────────────────────


def backup_if_exists(filepath: str) -> str | None:
    """If filepath exists, create a timestamped backup. Returns backup path or None."""
    path = Path(filepath)
    if not path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.stem}_backup_{timestamp}{path.suffix}")
    shutil.copy2(str(path), str(backup_path))
    log.info("Backed up existing glossary to %s", backup_path)
    print(f"Backup: {backup_path}")
    return str(backup_path)


# ─────────────────────────── SAVE ───────────────────────────


def save_glossary(
    glossary: dict, output_path: str, source_file: str, chunk_count: int
):
    """Save the glossary to JSON with metadata."""
    now = datetime.now().isoformat(timespec="seconds")

    output = {
        "meta": {
            "source_file": Path(source_file).name,
            "created_at": now,
            "updated_at": now,
            "model": MODEL,
            "chunk_count": chunk_count,
            "version": 1,
        },
        "characters": glossary.get("characters", []),
        "generics": glossary.get("generics", []),
        "terms": glossary.get("terms", []),
        "locations": glossary.get("locations", []),
        "relations": glossary.get("relations", []),
    }

    # preserve created_at and increment version if updating
    existing_path = Path(output_path)
    if existing_path.exists():
        try:
            with open(existing_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            output["meta"]["created_at"] = existing.get("meta", {}).get(
                "created_at", now
            )
            output["meta"]["version"] = (
                existing.get("meta", {}).get("version", 0) + 1
            )
        except (json.JSONDecodeError, KeyError):
            pass

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total = (
        len(output["characters"]) + len(output["terms"]) + len(output["locations"])
    )
    log.info("Saved glossary with %d entries to %s", total, output_path)
    print(f"\nSaved: {output_path} ({total} entries)")


# ─────────────────────────── RAW RESULTS CACHE ───────────────────────────


def save_raw_results(raw_results: list[dict], cache_path: str):
    """Save Phase 1 raw results to disk so they can be reused."""
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(raw_results, f, ensure_ascii=False, indent=2)
    log.info("Raw results cached to %s (%d chunks)", cache_path, len(raw_results))
    print(f"Raw results cached: {cache_path} ({len(raw_results)} chunks)")


def load_raw_results(cache_path: str) -> list[dict]:
    """Load Phase 1 raw results from cache."""
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    log.info("Loaded raw results from %s (%d chunks)", cache_path, len(data))
    print(f"Loaded raw results: {cache_path} ({len(data)} chunks)")
    return data


# ─────────────────────────── COST ESTIMATION ───────────────────────────


def estimate_cost(text: str, chunk_count: int) -> tuple[float, float, float]:
    """Estimate API cost for glossary extraction."""
    # Phase 1: each chunk through extraction
    input_tokens_p1 = len(text) * 0.8 + (500 * chunk_count)
    output_tokens_p1 = chunk_count * 300

    # Phase 2: consolidation (3 separate calls — one per category)
    input_tokens_p2 = output_tokens_p1 * 0.5 + (500 * 3)  # system prompts
    output_tokens_p2 = input_tokens_p2 * 0.8

    total_input = input_tokens_p1 + input_tokens_p2
    total_output = output_tokens_p1 + output_tokens_p2

    # GPT-5.2 pricing: $2.50/1M input, $10/1M output
    cost = (total_input * 2.50 + total_output * 10) / 1_000_000
    return cost, total_input, total_output


# ─────────────────────────── MAIN ───────────────────────────


def main():
    global MODEL, API_KEY

    parser = argparse.ArgumentParser(
        description="Build a structured glossary from a novel for literary translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 glossary_builder.py novel.epub
  python3 glossary_builder.py novel.docx -o glossary.json
  python3 glossary_builder.py novel.epub --chunk-size 8000
  python3 glossary_builder.py novel.docx --merge existing_glossary.json

  # Resume from cached Phase 1 results (skip extraction):
  python3 glossary_builder.py --from-raw novel_raw.json -o glossary.json
        """,
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Path to .epub or .docx file (not needed with --from-raw)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output JSON path (default: <input_stem>_glossary.json)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=MAX_CHARS_PER_CHUNK,
        help=f"Max characters per chunk (default: {MAX_CHARS_PER_CHUNK})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_REQUESTS,
        help=f"Delay between API requests in seconds (default: {DELAY_BETWEEN_REQUESTS})",
    )
    parser.add_argument(
        "--merge",
        type=str,
        default=None,
        help="Path to existing glossary JSON to merge with (existing entries take priority)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=MODEL,
        help=(
            f"Модель OpenAI (default: {MODEL}). Извлечение имён — простая задача: "
            "gpt-5-mini справляется и стоит в разы дешевле"
        ),
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=MIN_OCCURRENCES,
        help=(
            f"Минимум появлений сущности в тексте, чтобы попасть в глоссарий "
            f"(default: {MIN_OCCURRENCES}; 0 или 1 — отключить фильтр)"
        ),
    )
    parser.add_argument(
        "--no-consolidate",
        action="store_true",
        help="Skip GPT consolidation phase, use local deduplication only",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Пропустить финальную проходку слияния дублей между батчами (Phase 3)",
    )
    parser.add_argument(
        "--no-relations",
        action="store_true",
        help="Пропустить построение матрицы ты/вы (Phase 4)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        metavar="N",
        help="Потоков извлечения в Phase 1 (default: 4; 1 = последовательно с паузами)",
    )
    parser.add_argument(
        "--refine",
        type=str,
        metavar="GLOSSARY.json",
        help="Перечистить готовый глоссарий без API-вызовов: алиасы, род "
             "(местоимения+notes), склонение, частоты. Опционально укажи input "
             "(исходный текст) для точного пересчёта. Файл правится на месте с бэкапом.",
    )
    parser.add_argument(
        "--protect",
        type=str,
        metavar="USER_EDITS.json",
        help="Глоссарий с ручными правками пользователя: совпадающие по original "
             "записи получают его translation/gender/indeclinable/aliases вербатим, "
             "автоматика их не трогает",
    )
    parser.add_argument(
        "--from-raw",
        type=str,
        default=None,
        help="Load cached Phase 1 raw results from file (skip extraction, go straight to aggregation + consolidation)",
    )

    args = parser.parse_args()
    API_KEY = require_key("openai")

    MODEL = args.model

    # ── Mode: refine — перечистка готового глоссария без API-вызовов ──
    if args.refine:
        if not os.path.isfile(args.refine):
            sys.exit(f"Глоссарий не найден: {args.refine}")
        with open(args.refine, "r", encoding="utf-8") as f:
            glossary = json.load(f)

        text = None
        if args.input:
            if not os.path.isfile(args.input):
                sys.exit(f"Файл не найден: {args.input}")
            print(f"Читаю исходник для пересчёта: {args.input}")
            text = extract_text(args.input)

        before_genders = {
            e.get("original"): e.get("gender")
            for e in glossary.get("characters", [])
        }

        removed = tidy_aliases(glossary)
        print(f"Алиасы: выброшено генериков — {removed}")

        changed = apply_pronoun_genders(glossary, text)
        flips = [
            (name, before_genders[name], e.get("gender"))
            for e in glossary.get("characters", [])
            for name in [e.get("original")]
            if before_genders.get(name) != e.get("gender")
        ]
        print(f"Род исправлен: {changed} персонажей")
        for name, old, new in flips:
            print(f"  {name}: {old} -> {new}")

        decl = apply_declension_rules(glossary)
        print(f"Склонение обновлено: {decl} персонажей")

        moved = split_generic_characters(glossary)
        if moved:
            print(f"Генерики отделены от персонажей: {moved} записей → секция generics")

        # Правки пользователя — истина в последней инстанции, автоматика их не трогает
        if args.protect and os.path.isfile(args.protect):
            with open(args.protect, "r", encoding="utf-8") as f:
                protected = json.load(f)
            protect_map = {}
            for cat in ALL_CATEGORIES:
                for e in protected.get(cat, []):
                    key = e.get("original", "").casefold()
                    if key:
                        protect_map[key] = e
            restored = 0
            for cat in ALL_CATEGORIES:
                for e in glossary.get(cat, []):
                    src = protect_map.get(e.get("original", "").casefold())
                    if src is None:
                        continue
                    for field in ("translation", "gender", "indeclinable", "genitive", "aliases"):
                        if field in src and e.get(field) != src[field]:
                            e[field] = src[field]
                            restored += 1
            print(f"Защита правок пользователя: восстановлено полей — {restored}")

        if text is not None:
            annotate_occurrences(glossary, text)
            print("Частоты пересчитаны по именам собственным")
        sort_by_frequency(glossary)

        meta = glossary.get("meta", {})
        backup_if_exists(args.refine)
        save_glossary(
            glossary, args.refine,
            meta.get("source_file", args.input or args.refine),
            meta.get("chunk_count", 0),
        )
        return

    # ── Mode: resume from cached raw results ──
    if args.from_raw:
        if not os.path.isfile(args.from_raw):
            sys.exit(f"Raw cache not found: {args.from_raw}")

        output_path = args.output
        if not output_path:
            stem = Path(args.from_raw).stem.replace("_raw", "")
            output_path = f"{stem}_glossary.json"

        raw_results = load_raw_results(args.from_raw)
        source_file = args.from_raw
        chunk_count = len(raw_results)
        text = None  # без input: частотность по чанкам, род только по notes
        if args.input:
            if not os.path.isfile(args.input):
                sys.exit(f"Файл не найден: {args.input}")
            print(f"Читаю исходник (частоты, род, ты/вы): {args.input}")
            text = extract_text(args.input)
            source_file = args.input

    # ── Mode: full extraction from input file ──
    else:
        if not args.input:
            sys.exit("Specify input file or use --from-raw to resume from cache.")
        if not os.path.isfile(args.input):
            sys.exit(f"File not found: {args.input}")

        output_path = args.output or f"{Path(args.input).stem}_glossary.json"
        raw_cache_path = f"{Path(args.input).stem}_raw.json"
        source_file = args.input

        # Extract text
        log.info("Reading file: %s", args.input)
        print(f"Reading: {args.input}")
        text = extract_text(args.input)
        log.info("Extracted %d characters", len(text))
        print(f"Extracted {len(text):,} characters")

        if not text.strip():
            sys.exit("File is empty or text extraction failed.")

        # Chunk
        chunks = split_into_chunks(text, max_chars=args.chunk_size)
        log.info("Split into %d chunks (max %d chars)", len(chunks), args.chunk_size)
        print(f"Split into {len(chunks)} chunks (max {args.chunk_size} chars)")

        # Cost estimate
        cost, input_tokens, output_tokens = estimate_cost(text, len(chunks))
        print(f"\nEstimated cost: ${cost:.3f}")
        print(f"  input ~{input_tokens:.0f} tokens, output ~{output_tokens:.0f} tokens")
        print(f"  Phase 1: {len(chunks)} extraction calls")
        if not args.no_consolidate:
            print(f"  Phase 2: up to 3 consolidation calls (one per category)")
        print()

        confirm = input("Continue? [Y/n]: ").strip().lower()
        if confirm == "n":
            sys.exit("Cancelled.")

        # Phase 1: Extract from each chunk
        client = OpenAI(api_key=API_KEY)

        # Инкрементальный кэш: прогресс сохраняется по ходу, обрыв не теряет деньги
        partial_path = f"{Path(args.input).stem}_raw_partial.json"
        done: dict[int, dict] = {}
        if os.path.isfile(partial_path):
            try:
                with open(partial_path, "r", encoding="utf-8") as f:
                    done = {int(k): v for k, v in json.load(f).items()}
                print(f"Phase 1: возобновление — {len(done)} чанков уже извлечено")
                log.info("Phase 1 resume: %d chunks from partial cache", len(done))
            except (json.JSONDecodeError, OSError):
                done = {}

        def _save_partial():
            tmp = partial_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in done.items()}, f, ensure_ascii=False)
            os.replace(tmp, partial_path)

        todo = [i for i in range(1, len(chunks) + 1) if i not in done]

        print(f"\n--- Phase 1: Extraction ---")
        if args.parallel > 1:
            # Извлечение независимо по чанкам — гоним параллельно
            import concurrent.futures

            print(f"({args.parallel} потоков)")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futures = {
                    pool.submit(extract_entities_from_chunk, client, chunks[i - 1], i, len(chunks)): i
                    for i in todo
                }
                completed = 0
                for fut in concurrent.futures.as_completed(futures):
                    i = futures[fut]
                    result = fut.result()
                    if result:
                        done[i] = result
                    completed += 1
                    if completed % 25 == 0:
                        _save_partial()
        else:
            for n, i in enumerate(todo):
                result = extract_entities_from_chunk(client, chunks[i - 1], i, len(chunks))
                if result:
                    done[i] = result
                if n % 10 == 9:
                    _save_partial()
                if i < len(chunks):
                    time.sleep(args.delay)

        _save_partial()
        raw_results = [done[i] for i in sorted(done)]

        if not raw_results:
            sys.exit("No entities extracted from any chunk.")

        # Cache raw results immediately
        save_raw_results(raw_results, raw_cache_path)
        if os.path.isfile(partial_path):
            os.remove(partial_path)  # полный кэш сохранён — частичный больше не нужен

        succeeded = len(raw_results)
        failed = len(chunks) - succeeded
        chunk_count = len(chunks)
        if failed > 0:
            log.warning("%d/%d chunks failed extraction", failed, chunk_count)
            print(f"\nWarning: {failed}/{chunk_count} chunks failed")

    # ── From here: same flow for both modes ──

    # Aggregate locally
    aggregated = aggregate_raw_results(raw_results)
    total_raw = (
        len(aggregated["characters"])
        + len(aggregated["terms"])
        + len(aggregated["locations"])
    )
    print(
        f"\nExtracted {total_raw} unique entities "
        f"({len(aggregated['characters'])}ch, {len(aggregated['terms'])}t, "
        f"{len(aggregated['locations'])}l)"
    )

    # Frequency: count occurrences and drop rare entities BEFORE consolidation
    # (saves tokens and keeps the glossary focused on recurring names)
    annotate_occurrences(aggregated, text)
    if text is None:
        print("(частотность посчитана по числу чанков — точный подсчёт недоступен в --from-raw)")
    else:
        # Род по статистике местоимений всей книги — надёжнее LLM-догадки по чанку
        gender_changed = apply_pronoun_genders(aggregated, text)
        if gender_changed:
            print(f"Род уточнён по статистике местоимений: {gender_changed} персонажей")
    if args.min_occurrences > 1:
        dropped = filter_by_occurrences(aggregated, args.min_occurrences)
        if dropped:
            print(f"Отброшено {len(dropped)} редких сущностей (< {args.min_occurrences} появлений):")
            for category, name, count in dropped[:20]:
                print(f"  - [{category}] {name} ({count})")
            if len(dropped) > 20:
                print(f"  ... и ещё {len(dropped) - 20}")
            log.info("Dropped %d rare entities (min_occurrences=%d)", len(dropped), args.min_occurrences)
    # Ранняя чистка алиасов — ДО консолидации и дедупа: генерик-алиасы
    # («saint», «it») провоцируют ложные слияния разных персонажей
    early_removed = tidy_aliases(aggregated)
    if early_removed:
        print(f"Алиасы (до консолидации): выброшено генериков — {early_removed}")

    occ_map = build_occurrence_map(aggregated)

    # Phase 2: GPT Consolidation
    client = OpenAI(api_key=API_KEY)
    if not args.no_consolidate:
        print(f"\n--- Phase 2: Consolidation ---")
        glossary = consolidate_glossary(client, aggregated)
    else:
        glossary = _finalize_aggregated(aggregated)

    # Phase 3: cross-batch dedup — консолидация видит только свой батч из 50 записей,
    # поэтому дубли из разных батчей ("Paw" и "Pawarit") сливаются здесь.
    # Также спасает режим --no-consolidate, где алиасы вообще никто не сливал.
    if not args.no_dedup:
        print(f"\n--- Phase 3: Cross-batch dedup ---")
        merged_count = dedup_glossary(client, glossary)
        if merged_count:
            print(f"  Итого слито дублей: {merged_count}")
        log.info("Cross-batch dedup: %d entries merged", merged_count)

    # Чистка алиасов: кавычки, дубли, генерики (до пересчёта частот!)
    removed_aliases = tidy_aliases(glossary)
    if removed_aliases:
        print(f"Алиасы: выброшено генериков — {removed_aliases}")

    # Restore/recount occurrences after consolidation (GPT drops unknown fields),
    # then re-filter (aliases may have merged) and sort by frequency
    if text is not None:
        annotate_occurrences(glossary, text)
    else:
        restore_occurrences(glossary, occ_map)
    if args.min_occurrences > 1:
        filter_by_occurrences(glossary, args.min_occurrences)
    sort_by_frequency(glossary)

    # Пост-обработка кодом: род по местоимениям (повторно — консолидация могла
    # перезаписать) и склонение по правилам морфологии с примером род. падежа
    if text is not None:
        apply_pronoun_genders(glossary, text)
    decl_changed = apply_declension_rules(glossary)
    if decl_changed:
        print(f"Склоняемость проставлена по правилам морфологии: {decl_changed} персонажей")

    # Генерики («daughter», «the goddess») — в отдельную секцию, отдельно от имён
    moved = split_generic_characters(glossary)
    if moved:
        print(f"Генерики отделены от персонажей: {moved} записей → секция generics")

    # Phase 4: матрица ты/вы (только при наличии исходного текста)
    if not args.no_relations and text is not None:
        print(f"\n--- Phase 4: Матрица ты/вы ---")
        relations = build_relations(client, text, glossary.get("characters", []))
        glossary["relations"] = relations
        if relations:
            for rel in relations:
                print(f"  {rel['a']} → {rel['b']}: «{rel['a_to_b']}», "
                      f"{rel['b']} → {rel['a']}: «{rel['b_to_a']}»")
        else:
            print("  Пар не найдено")

    # Merge with existing glossary (existing entries keep their saved occurrences)
    if args.merge:
        glossary = merge_with_existing(glossary, args.merge)
        sort_by_frequency(glossary)

    # Backup and save
    backup_if_exists(output_path)
    save_glossary(glossary, output_path, source_file, chunk_count)

    # Summary
    chars = len(glossary.get("characters", []))
    terms = len(glossary.get("terms", []))
    locs = len(glossary.get("locations", []))
    print(f"\nSummary:")
    print(f"  Characters: {chars}")
    print(f"  Terms:      {terms}")
    print(f"  Locations:  {locs}")

    all_entries = (
        glossary.get("characters", []) + glossary.get("terms", []) + glossary.get("locations", [])
    )
    top = sorted(all_entries, key=lambda e: -e.get("occurrences", 0))[:10]
    if top and top[0].get("occurrences", 0) > 0:
        print(f"\n  Топ по частоте:")
        for entry in top:
            print(f"    {entry.get('occurrences', 0):>4}× {entry.get('original', '?')} → {entry.get('translation', '?')}")
    print(f"  Output:     {output_path}")
    if args.merge:
        print(f"  Merged:     {args.merge}")
    print(f"\nOpen in VSCode to review and edit: code {output_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        log.info("Interrupted by user (Ctrl+C)")
        sys.exit(1)
