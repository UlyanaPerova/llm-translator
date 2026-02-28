#!/usr/bin/env python3
"""
Glossary Builder for Literary Translation
Extracts characters, terms, and locations from a novel via GPT,
builds a structured glossary JSON for use with eng_translator.py.
"""

import argparse
import json
import sys
import time
import shutil
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import os
from logger import setup_logger
import logging

setup_logger(prefix="glossary_builder")
log = logging.getLogger("glossary_builder")

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    sys.exit("openai not installed. Run: pip install openai")

from eng_translator import extract_text, split_into_chunks

# ─────────────────────────── CONFIG ───────────────────────────

API_KEY = os.getenv("OPENAI_API_KEY") or sys.exit(
    "OPENAI_API_KEY not found. Set it in .env"
)
MODEL = "gpt-5.2"
TEMPERATURE_EXTRACT = 0.3
TEMPERATURE_CONSOLIDATE = 0.2
MAX_CHARS_PER_CHUNK = 6000
DELAY_BETWEEN_REQUESTS = 1.5
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 5

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

CONSOLIDATION_SYSTEM_PROMPT = """You are a literary glossary editor. You will receive a raw aggregated glossary extracted
from multiple chunks of a novel. Your task is to:

1. DEDUPLICATE: Merge entries that refer to the same entity (same name, different casing,
   or one is an alias of another). Combine their notes and aliases.
2. RESOLVE CONFLICTS: If gender was "unknown" in some chunks but identified in others,
   use the identified gender. If translations differ, pick the most consistent one and
   put alternatives into the "alternatives" field.
3. STANDARDIZE: Ensure all Russian transliterations follow consistent rules.
4. MERGE ALIASES: If "Paw" and "Pawarit" refer to the same character, keep one entry
   with aliases.

Return the consolidated glossary as JSON with this structure:
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
  ],
  "terms": [
    {
      "original": "Term",
      "translation": "Russian translation",
      "category": "category",
      "alternatives": [],
      "notes": "combined context"
    }
  ],
  "locations": [
    {
      "original": "Place",
      "translation": "Russian translation",
      "alternatives": [],
      "notes": "combined context"
    }
  ]
}

Sort all entries alphabetically by "original" within each category."""

# ─────────────────────────── API CALL WITH RETRY ───────────────────────────


def call_gpt_json(
    client: OpenAI,
    messages: list[dict],
    temperature: float = TEMPERATURE_EXTRACT,
    max_retries: int = MAX_RETRIES,
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
            )
            content = response.choices[0].message.content.strip()
            result = json.loads(content)
            tokens = response.usage.total_tokens if response.usage else 0
            log.debug("GPT response: %d tokens", tokens)
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
                seen_chars[key] = char

        for term in result.get("terms", []):
            key = term.get("original", "").strip().lower()
            if not key:
                continue
            if key in seen_terms:
                existing = seen_terms[key]
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
                seen_terms[key] = term

        for loc in result.get("locations", []):
            key = loc.get("original", "").strip().lower()
            if not key:
                continue
            if key in seen_locs:
                existing = seen_locs[key]
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
                seen_locs[key] = loc

    # Sort alphabetically by original
    return {
        "characters": sorted(seen_chars.values(), key=lambda x: x.get("original", "").lower()),
        "terms": sorted(seen_terms.values(), key=lambda x: x.get("original", "").lower()),
        "locations": sorted(seen_locs.values(), key=lambda x: x.get("original", "").lower()),
    }


# ─────────────────────────── PHASE 2: CONSOLIDATION ───────────────────────────


def consolidate_glossary(client: OpenAI, aggregated: dict) -> dict:
    """Send aggregated raw results to GPT for final consolidation."""
    entity_count = (
        len(aggregated["characters"])
        + len(aggregated["terms"])
        + len(aggregated["locations"])
    )
    log.info("Consolidating %d total entities via GPT...", entity_count)
    print(f"\nConsolidating {entity_count} entities via GPT...", end=" ", flush=True)

    messages = [
        {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(aggregated, ensure_ascii=False, indent=2),
        },
    ]

    result, tokens = call_gpt_json(
        client, messages, temperature=TEMPERATURE_CONSOLIDATE
    )
    if result is None:
        print("FAIL (using local aggregation)")
        log.warning("Consolidation failed, using locally aggregated results")
        return _finalize_aggregated(aggregated)

    for key in ("characters", "terms", "locations"):
        if key not in result:
            result[key] = []
        result[key] = sorted(
            result[key], key=lambda x: x.get("original", "").lower()
        )

    chars = len(result["characters"])
    terms = len(result["terms"])
    locs = len(result["locations"])
    print(f"OK ({chars}ch, {terms}t, {locs}l, {tokens} tok)")
    log.info(
        "Consolidated: %d characters, %d terms, %d locations (%d tokens)",
        chars,
        terms,
        locs,
        tokens,
    )
    return result


def _finalize_aggregated(aggregated: dict) -> dict:
    """Rename suggested_translation -> translation in locally aggregated data."""
    for category in ("characters", "terms", "locations"):
        for entry in aggregated.get(category, []):
            if "suggested_translation" in entry:
                entry["translation"] = entry.pop("suggested_translation")
    return aggregated


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
    for category in ("characters", "terms", "locations"):
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
        "terms": glossary.get("terms", []),
        "locations": glossary.get("locations", []),
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


# ─────────────────────────── COST ESTIMATION ───────────────────────────


def estimate_cost(text: str, chunk_count: int) -> tuple[float, float, float]:
    """Estimate API cost for glossary extraction."""
    # Phase 1: each chunk through extraction
    input_tokens_p1 = len(text) * 0.8 + (500 * chunk_count)
    output_tokens_p1 = chunk_count * 300

    # Phase 2: consolidation
    input_tokens_p2 = output_tokens_p1 * 0.5
    output_tokens_p2 = input_tokens_p2 * 0.8

    total_input = input_tokens_p1 + input_tokens_p2
    total_output = output_tokens_p1 + output_tokens_p2

    # GPT-5.2 pricing: $2.50/1M input, $10/1M output
    cost = (total_input * 2.50 + total_output * 10) / 1_000_000
    return cost, total_input, total_output


# ─────────────────────────── MAIN ───────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Build a structured glossary from a novel for literary translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 glossary_builder.py novel.epub
  python3 glossary_builder.py novel.docx -o glossary.json
  python3 glossary_builder.py novel.epub --chunk-size 8000
  python3 glossary_builder.py novel.docx --merge existing_glossary.json
        """,
    )
    parser.add_argument("input", help="Path to .epub or .docx file")
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
        "--no-consolidate",
        action="store_true",
        help="Skip GPT consolidation phase, use local deduplication only",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"File not found: {args.input}")

    output_path = args.output or f"{Path(args.input).stem}_glossary.json"

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
        print(f"  Phase 2: 1 consolidation call")
    print()

    confirm = input("Continue? [Y/n]: ").strip().lower()
    if confirm == "n":
        sys.exit("Cancelled.")

    # Phase 1: Extract from each chunk
    client = OpenAI(api_key=API_KEY)
    raw_results = []
    total_tokens = 0

    print(f"\n--- Phase 1: Extraction ---")
    for i, chunk in enumerate(chunks, 1):
        result = extract_entities_from_chunk(client, chunk, i, len(chunks))
        if result:
            raw_results.append(result)
        if i < len(chunks):
            time.sleep(args.delay)

    if not raw_results:
        sys.exit("No entities extracted from any chunk.")

    succeeded = len(raw_results)
    failed = len(chunks) - succeeded
    if failed > 0:
        log.warning("%d/%d chunks failed extraction", failed, len(chunks))
        print(f"\nWarning: {failed}/{len(chunks)} chunks failed")

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

    # Phase 2: GPT Consolidation
    if not args.no_consolidate:
        print(f"\n--- Phase 2: Consolidation ---")
        glossary = consolidate_glossary(client, aggregated)
    else:
        glossary = _finalize_aggregated(aggregated)

    # Merge with existing glossary
    if args.merge:
        glossary = merge_with_existing(glossary, args.merge)

    # Backup and save
    backup_if_exists(output_path)
    save_glossary(glossary, output_path, args.input, len(chunks))

    # Summary
    chars = len(glossary.get("characters", []))
    terms = len(glossary.get("terms", []))
    locs = len(glossary.get("locations", []))
    print(f"\nSummary:")
    print(f"  Characters: {chars}")
    print(f"  Terms:      {terms}")
    print(f"  Locations:  {locs}")
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
