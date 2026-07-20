#!/usr/bin/env python3
"""
English → Russian Literary Translator (Google Gemini)
Reads .epub, .docx, .md or .txt, splits into small chunks with overlap context,
translates via Gemini with glossary support, saves as .docx.

Отличия от eng_translator.py (GPT-версии):
 - работает через Google Gemini API (GEMINI_API_KEY в .env)
 - чанки меньше по умолчанию (3000 символов) — модель не «забывает» правила
 - правила пунктуации дублируются в конце каждого запроса (REMINDER-блок)
 - после каждого чанка — автоматическая проверка русской пунктуации
   (английские кавычки, дефис вместо тире в диалогах, непереведённые слова)
   с корректирующим запросом к модели и детерминированным дофиксом
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

setup_logger(prefix="eng_translate_gemini")
log = logging.getLogger("eng_translate_gemini")

load_dotenv()

try:
    from google import genai
    from google.genai import types
except ImportError:
    sys.exit("google-genai не установлен. Запусти: pip install google-genai")

# Переиспользуем извлечение текста, чанкование, глоссарий и сохранение .docx
from eng_translator import (
    extract_text,
    split_into_chunks,
    get_tail_paragraphs,
    load_glossary,
    filter_glossary_for_chunk,
    save_to_docx,
    _strip_html_tags,
)

# ─────────────────────────── CONFIG ───────────────────────────

API_KEY = os.getenv("GEMINI_API_KEY")  # проверяется в main()
MODEL = "gemini-2.5-flash"  # в ~4 раза дешевле pro; для максимального качества: --model gemini-2.5-pro
CORRECTION_MODEL = "gemini-2.5-flash"  # правка пунктуации — простая задача, всегда дешёвая модель
TEMPERATURE = 0.55  # чуть выше — модели нужна свобода перестраивать фразы, а не калькировать
MAX_CHARS_PER_CHUNK = 3000  # меньше, чем у GPT-версии: длинные чанки → модель забывает правила
CONTEXT_PARAGRAPHS = 3
DELAY_BETWEEN_REQUESTS = 2.0
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 10

# Цены $/1M токенов (input, output)
PRICING = {
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
}

# Thinking-токены билятся по цене ВЫВОДА — для перевода они не нужны.
# flash: полностью отключаем (0); pro: минимум (128, полностью выключить нельзя).
# Переопределить: --thinking-budget N (-1 = динамический режим по умолчанию модели).
THINKING_BUDGET_OVERRIDE: int | None = None


def _resolve_thinking_budget(model: str) -> int | None:
    if THINKING_BUDGET_OVERRIDE is not None:
        return None if THINKING_BUDGET_OVERRIDE == -1 else THINKING_BUDGET_OVERRIDE
    return 0 if "flash" in model else 128


# ─────────────────────────── CLIENT POOL (несколько API-ключей) ───────────────────────────

# Ключи в .env: GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...
# Round-robin по ключам обходит per-key rate limits — потоки не душат друг друга.


class DailyQuotaExceeded(RuntimeError):
    """Дневная квота модели исчерпана — ретраи бессмысленны, надо останавливаться."""


class ClientPool:
    """Пул ключей с учётом исчерпанных дневных квот: квота per-model per-key,
    поэтому ключ, умерший для pro, продолжает работать для flash."""

    def __init__(self, clients: list):
        import threading
        self._clients = clients
        self._lock = threading.Lock()
        self._i = 0
        self._exhausted: set[tuple[int, str]] = set()  # (id(client), model)

    def get(self, model: str = ""):
        with self._lock:
            alive = [c for c in self._clients if (id(c), model) not in self._exhausted]
            if not alive:
                raise DailyQuotaExceeded(
                    f"Все {len(self._clients)} ключ(а) исчерпали дневную квоту модели {model}"
                )
            client = alive[self._i % len(alive)]
            self._i += 1
            return client

    def mark_exhausted(self, client, model: str) -> int:
        """Пометить ключ исчерпанным для модели. Возвращает число живых ключей."""
        with self._lock:
            self._exhausted.add((id(client), model))
            return len([
                c for c in self._clients if (id(c), model) not in self._exhausted
            ])

    def __len__(self):
        return len(self._clients)


def make_client_pool() -> ClientPool:
    keys: list[str] = []
    for name, value in sorted(os.environ.items()):
        if re.match(r"^GEMINI_API_KEY(_\d+)?$", name) and value and value not in keys:
            keys.append(value)
    return ClientPool([genai.Client(api_key=k) for k in keys])


# Фактический расход токенов по моделям (заполняется по ходу работы)
USAGE: dict[str, dict[str, int]] = {}


def _track_usage(model: str, usage_metadata) -> None:
    if usage_metadata is None:
        return
    u = USAGE.setdefault(model, {"in": 0, "out": 0})
    u["in"] += usage_metadata.prompt_token_count or 0
    u["out"] += (usage_metadata.candidates_token_count or 0) + (
        getattr(usage_metadata, "thoughts_token_count", 0) or 0
    )


def actual_cost() -> float:
    total = 0.0
    for model, u in USAGE.items():
        base = model.removesuffix(" (batch)")
        in_price, out_price = PRICING.get(base, PRICING[MODEL])
        if model.endswith(" (batch)"):  # Batch API — половина цены
            in_price, out_price = in_price / 2, out_price / 2
        total += (u["in"] * in_price + u["out"] * out_price) / 1_000_000
    return total

SYSTEM_PROMPT_BASE = """You are a professional Russian literary translator. Your translation must be indistinguishable from a text originally written by a skilled native Russian author.

Rules:
1. Translate into natural, expressive, literary Russian. NEVER translate literally. Completely restructure sentences to follow Russian syntax, rhythm, and logic. If a sentence sounds like it was translated — rewrite it.
2. Eliminate passive voice wherever possible. Russian strongly prefers active constructions. "He was stopped" → "Его остановили" or "Он остановился", never "Он был остановлен".
3. Watch for tautology and cacophony, same-root words in Russian. Always reread your Russian output and fix any repetitions of roots, sounds, or syllables in close proximity.
4. RUSSIAN PUNCTUATION ONLY — this is critical:
   - Every line of dialogue starts on a new line with an em-dash and a space: — Привет.
   - Dialogue is NEVER embedded mid-paragraph and NEVER wrapped in quotation marks. Each speaker's line is a separate paragraph.
   - A single utterance split by attribution: — Привет, — сказал он, — как дела?
   - Quotations, titles, thoughts inside narration use Russian guillemets «ёлочки», nested quotes use „лапки". NEVER use English-style quotation marks "..." or “...” anywhere.
   - Use an em-dash (—), never a hyphen (-) or en-dash (–), for dialogue and syntactic dashes.
5. Use em-dashes rarely outside dialogue. Do NOT insert em-dashes that weren't implied in the original — overloaded text looks amateurish. Prefer commas, semicolons, or sentence breaks.
6. If the source text contains obvious typos, garbled characters, or OCR artifacts, silently correct them based on context before translating.
7. EVERYTHING must be translated or transliterated into Russian. Nothing should remain in English in the final text — including words in [square brackets]. Translate ALL of the following into Russian:
   - Character names → transliterate (e.g. Pawarit → Паварит, Hydra → Гидра, Giryeo → Гирё)
   - Monster/creature names → translate (e.g. [Hydra] → [Гидра], Black Dragon → Чёрный Дракон)
   - Skill/ability names → translate (e.g. Telekinesis → Телекинез, Faith → Вера)
   - Item/equipment names → translate (e.g. Dragon Heart → Сердце Дракона)
   - Location names → translate (e.g. Castle of Sloth → Замок Лени)
   - Organization names → translate (e.g. Hunter Association → Ассоциация Охотников)
   - Titles/headlines → translate fully
   The ONLY exceptions that stay in English: (a) gaming ranks: SS, SSS, S, A, B, C, D, E, F; (b) stat abbreviations: HP, MP, XP, NPC, PVP, PVE, DPS, AOE, ATK, DEF; (c) real-world brands: iPhone, Google.
8. Preserve the author's tone and intent, but express it with the full richness of Russian — varied vocabulary, expressive word order, natural collocations.

NATURAL RUSSIAN — KILL TRANSLATIONESE. This is the #1 quality criterion: the reader must never sense English under the Russian. Concrete mechanics:
a. Russian drops pronouns. Never chain sentences on «Он…/Она…». Restructure: verb-first sentences, implied subjects, an occasional name. «Он встал. Он подошёл к окну. Он вздохнул» is a firing offense.
b. Drop possessives English forces in: "his heart was pounding" → «сердце колотилось», NOT «его сердце колотилось». Body parts, relatives, clothing — no possessive unless contrastive.
c. No light-verb calques: «начал бежать» → «побежал»; «имеет значение» → «важно»; «находился в состоянии» → just say it; «является» — almost never in fiction.
d. Do NOT mirror source sentence boundaries. Split sprawling English sentences; merge choppy ones. Russian prose has its own breath — long descriptive periods, short punchy action.
e. Russian information structure: the key/new element goes to the END of the sentence. "A stranger stood in the doorway" → «В дверях стоял незнакомец», not «Незнакомец стоял в дверях».
f. "It was… that/when" and "there was" are forbidden calques: "It was the first time he..." → «Впервые он...»; "There was silence" → «Стало тихо» / «Повисла тишина».
g. Dialogue must sound SPOKEN: particles (же, ведь, ну, -то, разве, вот), ellipsis, inversion. People don't talk in complete grammatical sentences.
g2. «Мистер» + name is forbidden: "Mr. Seo Gio" → «Со Джио», "Mr. Yoo" → «Ю Сонвун» — drop the title, keep only the name. «Мистер» may stay ONLY when it stands alone without a name. Korean names are never surname-only: bare "Yoo" → «Ю Сонвун» (full name), never just «Ю».
h. You may re-word freely as long as meaning, plot facts, tone and imagery survive. A faithful translation reads as if WRITTEN in Russian — not as if converted from English.
i. REWORDING IS NOT ABRIDGING. Every sentence, every detail, every image of the source must be present in the translation. You restructure phrasing — you NEVER compress, summarize, or drop anything. If your Russian text is shorter than the English source by more than ~15%, you have lost content — restore it. Full length, natural phrasing: both, always.

Example of the standard required (same content, natural Russian — NOT shortened):
EN: "He felt that his heart was beating faster as he began to walk towards the door."
BAD (translationese): «Он почувствовал, что его сердце забилось быстрее, когда он начал идти к двери.»
GOOD: «Сердце забилось чаще, и он шагнул к двери.»
9. Maintain paragraph structure from the original, except where dialogue must be reformatted per rule 4.
10. Adapt idioms and culturally-specific expressions so they feel organic in Russian. Do NOT invent or add content that isn't in the original.
11. Do NOT add translator's notes, explanations, or commentary.
12. Do NOT skip or summarize any part of the text.
13. The source text may contain HTML formatting tags: <b>bold</b>, <i>italic</i>, <b><i>bold italic</i></b>. You MUST preserve these tags exactly in your translation, wrapping the corresponding translated words. Never add, remove, or alter these tags. Keep the same nesting order.

IMPORTANT: If you receive context from a previous translation chunk (marked as [CONTEXT FROM PREVIOUS CHUNK]), use it ONLY to maintain consistency in tone, style, character names, and narrative flow. Do NOT re-translate the context — translate ONLY the new text that follows after the context block.

Return ONLY the translated text, nothing else."""

# Короткое напоминание в КОНЦЕ каждого запроса — на длинных чанках модель
# «забывает» правила из начала промпта; напоминание рядом с текстом решает это.
RULES_REMINDER = """
[REMINDER — check your output against these before answering:]
1. Dialogue: new line, em-dash + space («— Привет.»). Never quotation marks for speech.
2. Quotes inside narration: only «ёлочки» (nested: „лапки"). NEVER "..." or “...”.
3. Em-dash (—) only, never hyphen (-) for dialogue/dashes.
4. Zero English words in the output (exceptions: SS/S/A ranks, HP/MP/XP-style abbreviations, real brands).
5. Natural literary Russian — active voice, no calques, no translationese.
6. No «Он…/Она…» sentence chains; drop forced possessives («сердце колотилось», not «его сердце»); don't mirror English sentence boundaries — reword freely, keep meaning.
7. TRANSLATE EVERYTHING: no sentence, detail or image may disappear. Reworded ≠ shortened — your output must cover 100% of the source content.
8. Preserve <b>/<i> tags on the corresponding words."""


def build_system_prompt(glossary: dict[str, str], relations: list[dict] | None = None) -> str:
    """System prompt + глоссарий + матрица ты/вы (если есть)."""
    prompt = SYSTEM_PROMPT_BASE
    if glossary:
        glossary_lines = "\n".join(f"  {eng} → {rus}" for eng, rus in glossary.items())
        prompt += (
            "\n\nMANDATORY GLOSSARY — always use these exact translations.\n"
            "Gender annotations [m], [f] indicate the character's gender for correct Russian "
            "adjective/verb agreement. [indeclinable] means the name does NOT change by "
            "grammatical case in Russian (e.g., keep 'Элис' as 'Элис' in all cases).\n"
            "[р.п. ...] shows the genitive form as a declension example — decline the name "
            "following this pattern. Names without [indeclinable] MUST be declined normally.\n"
            + glossary_lines
        )
    if relations:
        rel_lines = "\n".join(
            f"  {r['a']} → {r['b']}: «{r['a_to_b']}»; {r['b']} → {r['a']}: «{r['b_to_a']}»"
            + (f" ({r['note']})" if r.get("note") else "")
            for r in relations
        )
        prompt += (
            "\n\nFORMS OF ADDRESS (ты/вы) — apply STRICTLY in dialogue between these "
            "characters. «ты» = informal ty-forms, «вы» = formal vy-forms:\n" + rel_lines
        )
    return prompt


def filter_relations_for_chunk(relations: list[dict], chunk: str) -> list[dict]:
    """Пары ты/вы, оба участника которых упоминаются в чанке."""
    if not relations:
        return []
    chunk_lower = chunk.lower()
    return [
        r for r in relations
        if r.get("a", "").lower() in chunk_lower and r.get("b", "").lower() in chunk_lower
    ]


def load_relations(filepath: str) -> list[dict]:
    """Load ты/вы relations from a structured glossary JSON."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("relations", []) or []
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load relations from %s: %s", filepath, e)
    return []


def build_user_message(
    chunk: str, previous_translation: str | None, story_context: str | None = None
) -> str:
    """Запрос: конспект сюжета (опц.) + контекст (опц.) + текст + напоминание правил."""
    parts = []
    if story_context:
        parts.append(
            "[STORY CONTEXT — brief summaries of the preceding text, use ONLY for "
            "continuity (names, tone, plot). Do NOT translate this:]\n"
            f"{story_context}\n"
        )
    if previous_translation:
        context = get_tail_paragraphs(previous_translation)
        parts.append(
            "[CONTEXT FROM PREVIOUS CHUNK — do NOT re-translate this, use only for continuity:]\n"
            f"{context}\n"
        )
    parts.append(f"[NEW TEXT TO TRANSLATE:]\n{chunk}")
    parts.append(RULES_REMINDER)
    return "\n\n".join(parts)


# ─────────────────────────── VALIDATION ───────────────────────────

# Латиница, которую можно оставлять
ALLOWED_LATIN = {
    # ранги / статы
    "ss", "sss", "ex", "hp", "mp", "xp", "npc", "pvp", "pve", "dps", "aoe",
    "atk", "def", "rpg", "mmorpg", "buff", "debuff", "ui", "ai", "id",
    # бренды и общеупотребительные
    "iphone", "google", "youtube", "android", "wifi", "tv", "vip", "ok",
    "sms", "gps", "pc", "ceo", "iq", "usa", "uk", "vs", "online", "offline",
}
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
ENGLISH_QUOTES_RE = re.compile(r'[“”"]')
BAD_DASH_RE = re.compile(r"^[-–]\s", re.MULTILINE)


def validate_output(text: str) -> list[str]:
    """Проверка перевода на нарушения русской пунктуации и английские остатки."""
    issues = []
    clean = _strip_html_tags(text)

    if ENGLISH_QUOTES_RE.search(clean):
        issues.append(
            'English quotation marks found — replace with Russian «ёлочки» '
            "(or reformat as dialogue with an em-dash if it is direct speech)"
        )
    if BAD_DASH_RE.search(clean):
        issues.append(
            "a dialogue line starts with a hyphen '-' or en-dash '–' — must be an em-dash '— '"
        )
    leftovers = sorted({
        w for w in LATIN_WORD_RE.findall(clean) if w.lower() not in ALLOWED_LATIN
    })
    if leftovers:
        issues.append(
            "untranslated English words remain: " + ", ".join(leftovers[:15])
        )
    return issues


def validate_completeness(source: str, result: str) -> list[str]:
    """Линтер полноты: отношение длин ru/en и числа абзацев.
    Ловит «модель выкинула кусок» и «модель ударилась в пересказ»."""
    issues = []
    src = _strip_html_tags(source)
    out = _strip_html_tags(result)
    ratio = len(out) / max(len(src), 1)
    if ratio < 0.75:
        issues.append(f"перевод подозрительно короткий ({ratio:.2f}x от оригинала) — вероятно, пропущен кусок")
    elif ratio > 1.7:
        issues.append(f"перевод подозрительно длинный ({ratio:.2f}x) — возможен пересказ/добавления")

    src_paras = len([p for p in source.split("\n\n") if p.strip()])
    out_paras = len([l for l in re.split(r"\n+", result) if l.strip()])
    # Диалоги при переформатировании могут УВЕЛИЧИТЬ число абзацев — флагуем только потерю
    if src_paras >= 3 and out_paras < src_paras * 0.75:
        issues.append(f"абзацев меньше, чем в оригинале ({out_paras} против {src_paras}) — вероятно, потерян абзац")
    return issues


# ─────────────────────────── TRANSLITERATION DRIFT ───────────────────────────


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def detect_transliteration_drift(
    translated_chunks: list[str], glossary: dict[str, str]
) -> list[tuple[str, int, str, int]]:
    """Ищет искажённые транслитерации: «Поварит» при каноне «Паварит».
    Токен подозрителен, если он близок к канону по Левенштейну (≤2), но не
    начинается с его основы, и встречается сильно реже канона.
    Возвращает [(подозрительный_токен, его_частота, канон, частота_канона)]."""
    from collections import Counter

    full_text = "\n".join(translated_chunks)
    tokens = Counter(re.findall(r"(?<![\wа-яёА-ЯЁ])[А-ЯЁ][а-яё]{3,}(?![\wа-яёА-ЯЁ])", full_text))

    canons = set()
    for value in glossary.values():
        clean = re.sub(r"\s*\[.*?\]\s*$", "", value).strip()
        for word in clean.split():
            if re.match(r"^[А-ЯЁ][а-яё]{4,}$", word):  # ≥5 букв — меньше ложных срабатываний
                canons.add(word)

    findings = []
    for canon in canons:
        canon_l = canon.lower()
        stem = canon_l.rstrip(_RU_VOWELS_DRIFT) or canon_l
        canon_freq = sum(c for t, c in tokens.items() if t.lower().startswith(stem))
        if canon_freq < 5:
            continue  # редкий канон — статистики мало, не судим
        for token, count in tokens.items():
            token_l = token.lower()
            if token_l.startswith(stem):
                continue  # каноническая форма или её склонение
            if abs(len(token_l) - len(canon_l)) > 2:
                continue
            if _levenshtein(token_l, canon_l) <= 2 and count * 5 <= canon_freq:
                findings.append((token, count, canon, canon_freq))
    return findings


_RU_VOWELS_DRIFT = "аеёиоуыэюя"


# ─────────────────────────── LITRPG TEMPLATES ───────────────────────────


def load_litrpg_templates() -> dict[str, str]:
    """Load the fixed LitRPG term dictionary (litrpg_glossary.json next to script)."""
    path = Path(__file__).parent / "litrpg_glossary.json"
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load litrpg templates: %s", e)
        return {}


def apply_litrpg_templates(text: str, templates: dict[str, str]) -> str:
    """Детерминированная замена английских ЛитРПГ-терминов, уцелевших в переводе.
    Стопроцентная консистентность статов — то, что читатели замечают первым."""
    for eng, rus in templates.items():
        text = re.sub(
            rf"(?<![A-Za-z]){re.escape(eng)}(?![A-Za-z])", rus, text
        )
    return text


def deterministic_fixes(text: str) -> str:
    """Механические фиксы того, что можно исправить без модели."""
    # английские кавычки → «ёлочки» (только парные, в пределах строки) — до фиксов тире,
    # чтобы «»-символ уже был на месте для следующего паттерна
    text = re.sub(r"“([^”\n]+)”", r"«\1»", text)
    text = re.sub(r'"([^"\n]+)"', r"«\1»", text)
    # дефис/среднее тире в начале реплики → длинное тире
    text = re.sub(r"^[-–]\s", "— ", text, flags=re.MULTILINE)
    # дефис в атрибуции диалога («, - сказал он») → длинное тире
    text = re.sub(r"([,.!?…»])\s[-–]\s", r"\1 — ", text)
    return text


# ─────────────────────────── GEMINI CALLS ───────────────────────────


def _is_daily_quota_error(e: Exception) -> bool:
    s = str(e)
    return "429" in s and (
        "PerDay" in s or "per_day" in s or "per_model_per_day" in s
        or "free_tier" in s  # free tier: limit 0 — тоже «квота исчерпана» навсегда
    )


def _is_model_unavailable_error(e: Exception) -> bool:
    """404 «model no longer available» — модель недоступна этому ключу навсегда."""
    s = str(e)
    return "404" in s and ("no longer available" in s or "NOT_FOUND" in s)


def _generate(client: genai.Client, model: str, system_prompt: str, user_message: str,
              temperature: float = TEMPERATURE) -> tuple[str, int]:
    """Один вызов Gemini с ретраями. Возвращает (текст, токены)."""
    last_error = None
    config_kwargs = dict(
        temperature=temperature,
        system_instruction=system_prompt,
    )
    budget = _resolve_thinking_budget(model)
    if budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
    pool = client if isinstance(client, ClientPool) else None
    for attempt in range(1, MAX_RETRIES + 1):
        if pool is not None:
            client = pool.get(model)  # ротация живых ключей (исчерпанные пропускаются)
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_message,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            if response.text is None:
                raise RuntimeError(
                    f"Gemini вернул пустой ответ (finish/safety block), попытка {attempt}"
                )
            usage = response.usage_metadata
            _track_usage(model, usage)
            tokens = usage.total_token_count if usage else 0
            return response.text.strip(), tokens
        except DailyQuotaExceeded:
            raise  # из pool.get(): все ключи мертвы для этой модели
        except Exception as e:
            if _is_model_unavailable_error(e) and pool is not None:
                alive = pool.mark_exhausted(client, model)
                log.warning(
                    "Модель %s недоступна этому ключу (404), живых ключей: %d", model, alive
                )
                if alive > 0:
                    continue
                raise DailyQuotaExceeded(
                    f"Модель {model} недоступна ни одному ключу пула"
                ) from e
            if _is_daily_quota_error(e):
                if pool is not None:
                    alive = pool.mark_exhausted(client, model)
                    log.warning(
                        "Ключ исчерпал дневную квоту %s, живых ключей: %d. 429: %.500s",
                        model, alive, e,
                    )
                    if alive > 0:
                        continue  # тот же attempt-бюджет, следующий ключ
                # Единственный ключ (или все мертвы): ретраи бессмысленны — стоп
                raise DailyQuotaExceeded(
                    f"Дневная квота модели {model} исчерпана (429 PerDay)"
                ) from e
            last_error = e
            delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
            log.warning("Gemini error (attempt %d/%d): %s — retry in %ds",
                        attempt, MAX_RETRIES, e, delay)
            if attempt < MAX_RETRIES:
                time.sleep(delay)
    raise RuntimeError(f"Gemini: все {MAX_RETRIES} попыток неудачны: {last_error}")


CORRECTION_PROMPT = """You are a Russian copy editor. The Russian translation below has specific issues.
Fix ONLY the listed issues. Do NOT rewrite, rephrase, or change anything else.
Preserve all line breaks, paragraph structure and <b>/<i> tags.
Return ONLY the corrected text."""


def correct_issues(client: genai.Client, model: str, text: str, issues: list[str],
                   chunk_num: int, total: int) -> str:
    """Корректирующий запрос: просим модель исправить только найденные нарушения."""
    log.info("Chunk %d/%d: correction pass for %d issue(s): %s",
             chunk_num, total, len(issues), "; ".join(issues))
    user_message = (
        "Issues to fix:\n- " + "\n- ".join(issues) + "\n\nText:\n" + text
    )
    try:
        fixed, tokens = _generate(client, model, CORRECTION_PROMPT, user_message, temperature=0.1)
        # если модель сломала структуру (потеряла >20% текста) — не доверяем фиксу
        if len(fixed) < len(text) * 0.8:
            log.warning("Correction pass shortened text too much (%d → %d chars), keeping original",
                        len(text), len(fixed))
            return text
        return fixed
    except Exception as e:
        log.warning("Correction pass failed: %s — keeping original", e)
        return text


def _ensure_completeness(
    client: genai.Client, model: str, result: str, chunk: str,
    system_prompt: str, user_message: str, chunk_num: int, total: int,
    litrpg: dict[str, str] | None, quiet: bool = False,
) -> str:
    """Линтер полноты: слишком короткий/длинный перевод или потерянные абзацы →
    один полный ретрай с жёсткой инструкцией, выбираем лучший вариант."""
    def _ratio_dist(res: str) -> float:
        """Расстояние отношения длин до идеала ~1.0 (чем меньше, тем лучше)."""
        return abs(1.0 - len(_strip_html_tags(res)) / max(len(_strip_html_tags(chunk)), 1))

    comp_issues = validate_completeness(chunk, result)
    if not comp_issues:
        return result
    log.warning("Chunk %d/%d completeness: %s", chunk_num, total, "; ".join(comp_issues))
    if not quiet:
        print(f"↻ полнота", end=" ", flush=True)
    retry_message = user_message + (
        "\n\n[CRITICAL: your translation MUST include EVERY sentence and paragraph "
        "of the source text. Do not skip, merge, or summarize anything. "
        "The Russian text must be roughly the same length as the English source.]"
    )
    best, best_issues = result, len(comp_issues)
    for attempt in (1, 2):
        try:
            retry_result, _ = _generate(client, model, system_prompt, retry_message)
            retry_result = _postprocess(client, retry_result, chunk, chunk_num, total, litrpg)
            n_issues = len(validate_completeness(chunk, retry_result))
            if n_issues < best_issues or (
                n_issues == best_issues and _ratio_dist(retry_result) < _ratio_dist(best)
            ):
                best, best_issues = retry_result, n_issues
                log.info("Chunk %d/%d: completeness retry %d improved result", chunk_num, total, attempt)
            if best_issues == 0:
                break
        except Exception as e:
            log.warning("Chunk %d/%d completeness retry %d failed: %s", chunk_num, total, attempt, e)
    return best


def _postprocess(client: genai.Client, result: str, chunk: str, chunk_num: int, total: int,
                 litrpg: dict[str, str] | None) -> str:
    """Общий пост-процессинг: механика → коррекция моделью (если нужна) → шаблоны."""
    # Сначала бесплатные механические фиксы (кавычки, тире) — платный
    # корректирующий запрос уходит только если после них что-то осталось
    result = deterministic_fixes(result)
    if litrpg:
        result = apply_litrpg_templates(result, litrpg)
    issues = validate_output(result)
    if issues:
        print(f"⚠️ ({len(issues)} наруш.)", end=" ", flush=True)
        result = correct_issues(client, CORRECTION_MODEL, result, issues, chunk_num, total)
        result = deterministic_fixes(result)
        if litrpg:
            result = apply_litrpg_templates(result, litrpg)
    return result


def translate_chunk(
    client: genai.Client,
    model: str,
    chunk: str,
    chunk_num: int,
    total: int,
    previous_translation: str | None = None,
    glossary: dict[str, str] | None = None,
    relations: list[dict] | None = None,
    litrpg: dict[str, str] | None = None,
    story_context: str | None = None,
    quiet: bool = False,
) -> str:
    """Перевод одного чанка: перевод → пост-процессинг → проверка полноты с ретраем."""
    ctx_label = " +ctx" if (previous_translation or story_context) else ""
    log.info("Translating chunk %d/%d (%d chars%s)", chunk_num, total, len(chunk), ctx_label)
    if not quiet:
        print(f"  📝 Перевожу чанк {chunk_num}/{total} ({len(chunk)} символов{ctx_label})...",
              end=" ", flush=True)

    chunk_glossary = filter_glossary_for_chunk(glossary or {}, chunk)
    chunk_relations = filter_relations_for_chunk(relations or [], chunk)
    system_prompt = build_system_prompt(chunk_glossary, chunk_relations)
    user_message = build_user_message(chunk, previous_translation, story_context)

    try:
        result, tokens = _generate(client, model, system_prompt, user_message)
    except DailyQuotaExceeded:
        raise  # наверх — конвейер должен остановиться, а не писать ошибки в файл
    except Exception as e:
        log.error("Chunk %d/%d failed: %s", chunk_num, total, e)
        if not quiet:
            print(f"❌ Ошибка: {e}")
        return f"[ОШИБКА ПЕРЕВОДА ЧАНКА {chunk_num}: {e}]"

    result = _postprocess(client, result, chunk, chunk_num, total, litrpg)
    result = _ensure_completeness(
        client, model, result, chunk, system_prompt, user_message,
        chunk_num, total, litrpg, quiet,
    )

    final_issues = validate_output(result) + validate_completeness(chunk, result)
    status = "✅" if not final_issues else f"⚠️ осталось: {len(final_issues)}"
    if not quiet:
        print(f"{status} (токенов: {tokens})")
    log.info("Chunk %d/%d done: %d tokens, %d chars out, %d unresolved issues",
             chunk_num, total, tokens, len(result), len(final_issues))
    return result


# ─────────────────────────── CACHE (map-based, возобновляемый) ───────────────────────────


def _cache_path(input_path: str) -> str:
    stem = Path(input_path).stem
    return str(Path(input_path).parent / f".{stem}_translation_cache_gemini.json")


def save_cache(cache_file: str, data: dict) -> None:
    """Atomic write: tmp + rename."""
    tmp = cache_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, cache_file)


def load_cache(cache_file: str, total_chunks: int) -> dict:
    """Load cache; returns {"summaries": [...]|None, "results": {int: str}}.
    Migrates the old list-based format. Invalidates on chunk-count mismatch."""
    empty = {"summaries": None, "results": {}}
    if not os.path.isfile(cache_file):
        return empty
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load cache %s: %s", cache_file, e)
        return empty

    # старый формат (список подряд идущих чанков)
    if "translated" in data and isinstance(data["translated"], list):
        results = {i + 1: t for i, t in enumerate(data["translated"])}
        return {"summaries": None, "results": results}

    if data.get("total_chunks") not in (None, total_chunks):
        log.warning(
            "Cache chunk count mismatch (%s vs %d) — cache ignored",
            data.get("total_chunks"), total_chunks,
        )
        print("⚠️  Кэш от другой нарезки (--chunk-size изменился?) — игнорирую")
        return empty

    results = {int(k): v for k, v in (data.get("results") or {}).items()}
    return {"summaries": data.get("summaries"), "results": results}


# ─────────────────────────── STORY SUMMARIES (пре-пасс по исходнику) ───────────────────────────

# Конспекты чанков строятся по АНГЛИЙСКОМУ исходнику до перевода. Это убирает
# последовательную зависимость чанков друг от друга (контекст больше не хвост
# предыдущего перевода) — весь перевод можно гнать параллельно или батчем.

SUMMARY_SYSTEM_PROMPT = (
    "Summarize this fragment of a novel in 2-3 English sentences. "
    "Focus on plot events, which characters are present, and their emotional state. "
    "Return ONLY the summary."
)
SUMMARY_RECENT = 6  # сколько последних конспектов идёт в контекст целиком


def build_summaries(
    client: genai.Client, chunks: list[str], cached: list | None,
    workers: int = 4,
) -> list[str | None]:
    """Конспект каждого чанка дешёвой моделью, параллельно. Возобновляемо."""
    import concurrent.futures

    summaries: list[str | None] = list(cached) if cached and len(cached) == len(chunks) else [None] * len(chunks)
    todo = [i for i, s in enumerate(summaries) if not s]
    if not todo:
        return summaries

    print(f"📋 Конспектирую исходник: {len(todo)}/{len(chunks)} чанков...")

    def _summarize(i: int) -> tuple[int, str | None]:
        try:
            text, _ = _generate(
                client, CORRECTION_MODEL, SUMMARY_SYSTEM_PROMPT, chunks[i], temperature=0.2
            )
            return i, text
        except Exception as e:
            log.warning("Summary for chunk %d failed: %s", i + 1, e)
            return i, None

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, text in pool.map(_summarize, todo):
            summaries[i] = text
            done += 1
            if done % 20 == 0:
                print(f"   ...{done}/{len(todo)}")
    ok = sum(1 for s in summaries if s)
    print(f"   Готово: {ok}/{len(chunks)} конспектов")
    return summaries


def build_story_context(summaries: list[str | None], upto: int) -> str | None:
    """Контекст сюжета для чанка #upto (0-based): первые 2 конспекта + последние N."""
    prev = [s for s in summaries[:upto] if s]
    if not prev:
        return None
    if len(prev) <= 2 + SUMMARY_RECENT:
        parts = prev
    else:
        omitted = len(prev) - 2 - SUMMARY_RECENT
        parts = prev[:2] + [f"[... {omitted} fragments omitted ...]"] + prev[-SUMMARY_RECENT:]
    return "\n".join(f"- {p}" for p in parts)


# ─────────────────────────── PARALLEL MODE ───────────────────────────


def translate_parallel(
    client: genai.Client, model: str, chunks: list[str],
    results: dict[int, str], summaries: list[str | None],
    glossary: dict, relations: list[dict], litrpg: dict[str, str],
    cache_file: str, cache_data_fn, workers: int,
) -> None:
    """Параллельный перевод независимых чанков (контекст — из конспектов)."""
    import concurrent.futures
    import threading

    lock = threading.Lock()
    todo = [i for i in range(1, len(chunks) + 1) if i not in results]
    print(f"🚀 Параллельный перевод: {len(todo)} чанков, {workers} потоков")

    def _work(i: int) -> None:
        ctx = build_story_context(summaries, i - 1)
        result = translate_chunk(
            client, model, chunks[i - 1], i, len(chunks),
            glossary=glossary, relations=relations, litrpg=litrpg,
            story_context=ctx, quiet=True,
        )
        with lock:
            results[i] = result
            save_cache(cache_file, cache_data_fn())
            n_done = len(results)
        issues = len(validate_output(result)) + len(validate_completeness(chunks[i - 1], result))
        marker = "✅" if not issues and not result.startswith("[ОШИБКА") else "⚠️"
        print(f"  {marker} чанк {i}/{len(chunks)} готов ({n_done}/{len(chunks)} всего)")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_work, todo))
    except DailyQuotaExceeded as e:
        save_cache(cache_file, cache_data_fn())
        print(f"\n🛑 {e}")
        print(f"   Прогресс сохранён: {len(results)}/{len(chunks)} чанков.")
        print(f"   Квота сбросится в течение суток. Продолжить: тот же запуск с --resume")
        print(f"   Или добавь ещё GEMINI_API_KEY_2/_3 в .env (у каждого ключа своя квота).")
        sys.exit(2)


# ─────────────────────────── BATCH MODE (−50% к цене) ───────────────────────────

BATCH_POLL_INTERVAL = 30
BATCH_RUNNING_STATES = {"JOB_STATE_PENDING", "JOB_STATE_RUNNING", "JOB_STATE_QUEUED"}


def translate_batch(
    client: genai.Client, model: str, chunks: list[str],
    results: dict[int, str], summaries: list[str | None],
    glossary: dict, relations: list[dict], litrpg: dict[str, str],
    cache_file: str, cache_data_fn,
) -> None:
    """Перевод через Gemini Batch API: все чанки одним заданием, цена −50%.
    Пост-обработка и точечные доработки — обычными вызовами после получения."""
    todo = [i for i in range(1, len(chunks) + 1) if i not in results]
    if not todo:
        return

    if isinstance(client, ClientPool):
        client = client.get()  # batch — одно задание, один ключ

    print(f"📦 Batch-режим: {len(todo)} чанков одним заданием (цена −50%)")

    budget = _resolve_thinking_budget(model)
    inline_requests = []
    for i in todo:
        chunk = chunks[i - 1]
        chunk_glossary = filter_glossary_for_chunk(glossary or {}, chunk)
        chunk_relations = filter_relations_for_chunk(relations or [], chunk)
        config: dict = {
            "temperature": TEMPERATURE,
            "system_instruction": build_system_prompt(chunk_glossary, chunk_relations),
        }
        if budget is not None:
            config["thinking_config"] = {"thinking_budget": budget}
        inline_requests.append({
            "contents": [{
                "role": "user",
                "parts": [{"text": build_user_message(chunk, None, build_story_context(summaries, i - 1))}],
            }],
            "config": config,
        })

    job = client.batches.create(
        model=model,
        src=inline_requests,
        config={"display_name": f"translate_{Path(cache_file).stem}"},
    )
    print(f"   Задание создано: {job.name}")
    log.info("Batch job created: %s", job.name)

    while True:
        state = getattr(job.state, "name", str(job.state))
        if state not in BATCH_RUNNING_STATES:
            break
        print(f"   ⏳ {state}, проверка через {BATCH_POLL_INTERVAL}с...")
        time.sleep(BATCH_POLL_INTERVAL)
        job = client.batches.get(name=job.name)

    state = getattr(job.state, "name", str(job.state))
    if state != "JOB_STATE_SUCCEEDED":
        raise RuntimeError(f"Batch job завершился со статусом {state}")

    responses = job.dest.inlined_responses
    print(f"   Получено ответов: {len(responses)}. Пост-обработка...")

    for idx, i in enumerate(todo):
        chunk = chunks[i - 1]
        inline = responses[idx] if idx < len(responses) else None
        response = getattr(inline, "response", None) if inline else None
        error = getattr(inline, "error", None) if inline else "no response"
        text = getattr(response, "text", None) if response else None
        if text:
            _track_usage(f"{model} (batch)", getattr(response, "usage_metadata", None))
            result = _postprocess(client, text.strip(), chunk, i, len(chunks), litrpg)
            chunk_glossary = filter_glossary_for_chunk(glossary or {}, chunk)
            chunk_relations = filter_relations_for_chunk(relations or [], chunk)
            result = _ensure_completeness(
                client, model, result, chunk,
                build_system_prompt(chunk_glossary, chunk_relations),
                build_user_message(chunk, None, build_story_context(summaries, i - 1)),
                i, len(chunks), litrpg, quiet=True,
            )
            results[i] = result
        else:
            log.error("Batch chunk %d failed: %s", i, error)
            results[i] = f"[ОШИБКА ПЕРЕВОДА ЧАНКА {i}: batch error {error}]"
        save_cache(cache_file, cache_data_fn())

    print(f"   ✅ Batch завершён")


# ─────────────────────────── MAIN ───────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="English → Russian Literary Translator (Google Gemini)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python3 eng_translator_gemini.py book.docx
  python3 eng_translator_gemini.py book.epub -o перевод.docx
  python3 eng_translator_gemini.py book.docx --batch          # −50%% к цене
  python3 eng_translator_gemini.py book.docx --parallel 8     # в 8 раз быстрее
  python3 eng_translator_gemini.py book.docx --model gemini-2.5-pro
  python3 eng_translator_gemini.py book.docx --glossary glossary.json
  python3 eng_translator_gemini.py book.docx --resume
        """,
    )
    parser.add_argument("input", help="Путь к .epub, .docx, .md или .txt файлу")
    parser.add_argument("-o", "--output",
                        help="Путь к выходному .docx (по умолчанию: input_translated.docx)")
    parser.add_argument("--model", default=MODEL,
                        help=f"Модель Gemini (по умолчанию: {MODEL} — дешёвая; "
                             "максимальное качество: gemini-2.5-pro, ~4x дороже)")
    parser.add_argument("--thinking-budget", type=int, default=None,
                        help="Бюджет thinking-токенов (по умолчанию: 0 для flash, 128 для pro; "
                             "-1 = динамический режим модели — ДОРОГО, thinking билятся как вывод)")
    parser.add_argument("--chunk-size", type=int, default=MAX_CHARS_PER_CHUNK,
                        help=f"Макс. символов на чанк (по умолчанию: {MAX_CHARS_PER_CHUNK}; "
                             "больше 5000 не рекомендуется — модель начинает забывать правила)")
    parser.add_argument("--context", type=int, default=CONTEXT_PARAGRAPHS,
                        help=f"Кол-во абзацев из предыдущего перевода для контекста "
                             f"(по умолчанию: {CONTEXT_PARAGRAPHS}, 0 = отключить)")
    parser.add_argument("--delay", type=float, default=DELAY_BETWEEN_REQUESTS,
                        help=f"Пауза между запросами в секундах (по умолчанию: {DELAY_BETWEEN_REQUESTS})")
    parser.add_argument("--glossary", type=str, default=None,
                        help="Путь к JSON-глоссарию (по умолчанию: авто-поиск <имя>_glossary.json)")
    parser.add_argument("--no-glossary", action="store_true",
                        help="Отключить авто-поиск глоссария")
    parser.add_argument("--resume", action="store_true",
                        help="Возобновить перевод из кэша")
    parser.add_argument("--redo-chunks", type=str, default=None, metavar="LIST",
                        help="Перегнать только указанные чанки из кэша: \"61,229,300-310\" "
                             "(вместе с --resume)")
    parser.add_argument("--summaries", action="store_true",
                        help="Пре-пасс конспектов исходника для контекста сюжета "
                             "(в --parallel и --batch включён всегда)")
    parser.add_argument("--parallel", type=int, default=0, metavar="N",
                        help="Параллельный перевод в N потоков (чанки независимы "
                             "благодаря конспектам; быстрее в N раз)")
    parser.add_argument("--batch", action="store_true",
                        help="Gemini Batch API: все чанки одним заданием, цена −50%% "
                             "(может занять до пары часов, обычно быстрее)")
    parser.add_argument("--no-litrpg", action="store_true",
                        help="Не подключать словарь ЛитРПГ-шаблонов (litrpg_glossary.json)")

    args = parser.parse_args()

    if not API_KEY:
        sys.exit("GEMINI_API_KEY не найден в .env\nПолучить: https://aistudio.google.com/apikey")

    global THINKING_BUDGET_OVERRIDE
    THINKING_BUDGET_OVERRIDE = args.thinking_budget

    if not os.path.isfile(args.input):
        sys.exit(f"Файл не найден: {args.input}")

    if args.chunk_size > 5000:
        print(f"⚠️  Чанк {args.chunk_size} символов — на длинных чанках модель хуже "
              "держит правила. Рекомендуется ≤ 5000.")

    output_path = args.output or f"{Path(args.input).stem}_translated.docx"

    # Глоссарий: явный путь или авто-поиск <stem>_glossary.json
    glossary = {}
    relations = []
    glossary_path = args.glossary
    if not glossary_path and not args.no_glossary:
        candidate = f"{Path(args.input).stem}_glossary.json"
        if os.path.isfile(candidate):
            glossary_path = candidate
            print(f"📚 Найден глоссарий: {candidate}")
    if glossary_path:
        if not os.path.isfile(glossary_path):
            sys.exit(f"Словарь не найден: {glossary_path}")
        glossary = load_glossary(glossary_path)
        relations = load_relations(glossary_path)
        print(f"📚 Словарь загружен: {len(glossary)} терминов"
              + (f", матрица ты/вы: {len(relations)} пар" if relations else ""))

    # ЛитРПГ-шаблоны: фиксированные переводы статов/системных терминов
    litrpg = {} if args.no_litrpg else load_litrpg_templates()
    if litrpg:
        for eng, rus in litrpg.items():
            glossary.setdefault(eng, rus)  # пользовательский глоссарий в приоритете
        print(f"🎮 ЛитРПГ-шаблоны: {len(litrpg)} терминов")

    # Извлечение и чанкование
    print(f"📖 Читаю файл: {args.input}")
    text = extract_text(args.input)
    print(f"   Извлечено {len(text)} символов")
    if not text.strip():
        sys.exit("Файл пуст или не удалось извлечь текст.")

    chunks = split_into_chunks(text, max_chars=args.chunk_size)
    print(f"✂️  Разбито на {len(chunks)} чанков (макс. {args.chunk_size} символов)")
    print(f"📎 Контекст: {args.context} абзацев из предыдущего перевода")

    # Оценка стоимости (thinking отключён, поэтому без надбавки на размышления)
    est_in = len(text) / 4 * 1.35  # текст + системный промпт + глоссарий + контекст + reminder
    est_out = len(text) / 4 * 1.2  # русский чуть длиннее в токенах
    for m, (in_price, out_price) in PRICING.items():
        cost = (est_in * in_price + est_out * out_price) / 1_000_000
        marker = " ← выбрана" if m == args.model else ""
        print(f"💰 {m}: ~${cost:.2f}{marker}")
    if args.model not in PRICING:
        in_price, out_price = PRICING[MODEL]
        cost = (est_in * in_price + est_out * out_price) / 1_000_000
        print(f"💰 {args.model}: ~${cost:.2f} (цены неизвестны, оценка по {MODEL}) ← выбрана")
    print()

    confirm = input("Продолжить? [Y/n]: ").strip().lower()
    if confirm == "n":
        sys.exit("Отменено.")

    cache_file = _cache_path(args.input)
    client = make_client_pool()
    if len(client) > 1:
        print(f"🔑 API-ключей в пуле: {len(client)} (round-robin)")

    use_summaries = args.summaries or args.parallel > 0 or args.batch

    # Кэш (map-based): при --resume подхватываются и конспекты, и переводы
    cached = load_cache(cache_file, len(chunks)) if args.resume else {"summaries": None, "results": {}}
    results: dict[int, str] = cached["results"]
    if results:
        print(f"🔄 Возобновление из кэша: {len(results)}/{len(chunks)} чанков уже переведено")
        log.info("Resumed from cache: %d/%d chunks", len(results), len(chunks))

    # --redo-chunks: выбросить указанные чанки из кэша, чтобы перегнать только их
    if args.redo_chunks and results:
        redo: set[int] = set()
        for part in args.redo_chunks.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                redo.update(range(int(a), int(b) + 1))
            elif part:
                redo.add(int(part))
        dropped = [i for i in redo if results.pop(i, None) is not None]
        print(f"♻️  Перегон чанков: {len(dropped)} шт. выброшено из кэша")
        log.info("Redo chunks: %s", sorted(dropped))

    summaries: list[str | None] = [None] * len(chunks)
    if use_summaries:
        summaries = build_summaries(
            client, chunks, cached.get("summaries"),
            workers=max(args.parallel, 4),
        )

    def cache_data() -> dict:
        return {
            "metadata": {"input": args.input, "model": args.model, "chunk_size": args.chunk_size},
            "total_chunks": len(chunks),
            "summaries": summaries if use_summaries else None,
            "results": {str(k): v for k, v in results.items()},
        }

    save_cache(cache_file, cache_data())

    # ── Перевод: batch / parallel / последовательный ──
    if args.batch:
        try:
            translate_batch(
                client, args.model, chunks, results, summaries,
                glossary, relations, litrpg, cache_file, cache_data,
            )
        except Exception as e:
            log.error("Batch mode failed: %s", e)
            print(f"❌ Batch-режим не сработал: {e}")
            print("   Прогресс сохранён. Продолжить обычным способом: --resume "
                  "(или --resume --parallel 4)")
            sys.exit(1)
    elif args.parallel > 0:
        translate_parallel(
            client, args.model, chunks, results, summaries,
            glossary, relations, litrpg, cache_file, cache_data,
            workers=args.parallel,
        )
    else:
        try:
            for i in range(1, len(chunks) + 1):
                if i in results:
                    continue
                prev = None
                story_ctx = None
                if use_summaries:
                    story_ctx = build_story_context(summaries, i - 1)
                elif args.context > 0 and (i - 1) in results:
                    prev = _strip_html_tags(results[i - 1])

                result = translate_chunk(
                    client, args.model, chunks[i - 1], i, len(chunks),
                    previous_translation=prev, glossary=glossary,
                    relations=relations, litrpg=litrpg, story_context=story_ctx,
                )
                results[i] = result
                save_cache(cache_file, cache_data())

                if i < len(chunks):
                    time.sleep(args.delay)
        except DailyQuotaExceeded as e:
            save_cache(cache_file, cache_data())
            print(f"\n🛑 {e}")
            print(f"   Прогресс сохранён: {len(results)}/{len(chunks)} чанков. Продолжить: --resume")
            print(f"   Или добавь ещё GEMINI_API_KEY_2/_3 в .env (у каждого ключа своя квота).")
            sys.exit(2)

    translated = [
        results.get(i, f"[ОШИБКА ПЕРЕВОДА ЧАНКА {i}: результат отсутствует]")
        for i in range(1, len(chunks) + 1)
    ]

    # Сохранение
    log.info("Saving translation to %s", output_path)
    save_to_docx(translated, output_path)

    # Итоговая сводка + сквозная проверка
    all_issues = []
    bad_chunks: set[int] = set()
    for idx, chunk_text in enumerate(translated, 1):
        for issue in validate_output(chunk_text):
            all_issues.append((idx, issue))
        for issue in validate_completeness(chunks[idx - 1], chunk_text):
            all_issues.append((idx, issue))
            bad_chunks.add(idx)

    # Кэш удаляем ТОЛЬКО если нарушений полноты нет — иначе он нужен
    # для точечного перегона плохих чанков без оплаты всей книги заново
    if os.path.isfile(cache_file):
        if bad_chunks:
            ranges = ",".join(str(i) for i in sorted(bad_chunks)[:50])
            print(f"   💾 Кэш сохранён — плохие чанки можно перегнать точечно:")
            print(f"      python3 eng_translator_gemini.py {args.input} --resume "
                  f"--redo-chunks \"{ranges}{'...' if len(bad_chunks) > 50 else ''}\" ...")
            log.info("Cache kept: %d chunks flagged for redo", len(bad_chunks))
        else:
            os.remove(cache_file)
            log.info("Translation cache removed after successful save")

    # Детектор дрейфа транслитерации: «Поварит» при каноне «Паварит»
    drift = detect_transliteration_drift(translated, glossary) if glossary else []

    total_chars_in = sum(len(c) for c in chunks)
    total_chars_out = sum(len(c) for c in translated)
    errors = sum(1 for c in translated if c.startswith("[ОШИБКА ПЕРЕВОДА"))
    print(f"\n📊 Итого:")
    print(f"   Исходный текст: {total_chars_in:,} символов")
    print(f"   Перевод:        {total_chars_out:,} символов")
    print(f"   Чанков:         {len(chunks)}")
    for m, u in USAGE.items():
        print(f"   Токены ({m}): {u['in']:,} in / {u['out']:,} out")
    print(f"   💰 Фактическая стоимость: ${actual_cost():.3f}")
    if glossary:
        print(f"   Словарь:        {len(glossary)} терминов")
    if errors:
        print(f"   ❌ Чанков с ошибками: {errors} — перезапусти с --resume после исправления")
    if all_issues:
        print(f"   ⚠️  Осталось нарушений (пунктуация/английский/полнота): {len(all_issues)}")
        for idx, issue in all_issues[:10]:
            print(f"      чанк {idx}: {issue}")
        print(f"   Добить остатки: python3 fix_english_remnants.py {output_path}")
    else:
        print(f"   ✅ Проверка пунктуации, английского и полноты: чисто")
    if drift:
        print(f"   ⚠️  Возможный дрейф транслитерации ({len(drift)}):")
        for token, count, canon, canon_freq in drift[:10]:
            print(f"      «{token}» ×{count} — вероятно, искажение канона «{canon}» (×{canon_freq})")
    elif glossary:
        print(f"   ✅ Дрейф транслитерации: не обнаружен")
    print(f"   Файл:           {output_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nПрервано. Продолжить: --resume")
        log.info("Interrupted by user (Ctrl+C)")
        sys.exit(1)
