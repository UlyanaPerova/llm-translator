#!/usr/bin/env python3
"""
Второй проход: короткие формы «Улыбающ* тиран*» без «морской деревни».
Источник содержит эпитет всего 7 раз — большинство коротких форм в переводе
это порча имени Giovanni. GPT различает по контексту:
  - названное состояние/личность/ипостась (часто в кавычках, со словами
    «состояние», «личность», «форма») — ОСТАВИТЬ эпитет;
  - персонаж как действующее лицо повествования — заменить на «Джованни».
Дифф-контроль: менять можно только слова самой фразы, вставлять — только «Джованни».
"""

import difflib
import json
import os
import re
import sys
import logging
import shutil
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта в sys.path
from common.logger import setup_logger
from common.api_keys import require_key
setup_logger(prefix="fix_tyrant")
log = logging.getLogger("fix_tyrant")

from docx import Document
from openai import OpenAI
from postedit.fix_name_poisoning import para_tagged_text, rewrite_para

TARGET = "The_Artist_Who_Paints_Dungeon_translated_pro.docx"
CACHE = ".fix_tyrant_cache.json"
MODEL = "gpt-5.1"

SHORT_RE = re.compile(r"[Уу]лыбающ[а-яё]+\s+тиран[а-яё]*(?!\s+морской)")
REMOVABLE_RE = re.compile(r"^[«»\"'(\[]?([Уу]лыбающ|[Тт]иран)")
INSERTABLE_RE = re.compile(r"^[«»\"'(\[]?Джованни[.,!?;:…»\"')\]]*$")

SYSTEM_PROMPT = """You are a precise text-repair tool for a Russian novel translation.
The phrase «Улыбающийся тиран» (any grammatical form) appears in the paragraph.
It is EITHER a legitimate named state/persona (usually with words like «состояние»,
«личность», «форма», «ипостась», often in «...» quotes) — OR a corrupted rendering
of the character name «Джованни» (when the phrase simply acts as the person in the
narrative: he speaks, walks, thinks, is addressed).

For each occurrence decide:
- named state/persona concept → KEEP the phrase exactly as is;
- person acting/referenced → replace that occurrence with «Джованни» (indeclinable).

Change NOTHING else. Return ONLY the resulting paragraph text (it may be identical
to the input if all occurrences are legitimate)."""


def diff_is_safe(original: str, fixed: str) -> bool:
    a, b = original.split(), fixed.split()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        for w in a[i1:i2]:
            if not REMOVABLE_RE.match(w):
                return False
        for w in b[j1:j2]:
            if not INSERTABLE_RE.match(w):
                return False
    return True


def main():
    api_key = require_key("openai")
    client = OpenAI(api_key=api_key)

    doc = Document(TARGET)
    paras = doc.paragraphs
    affected = [i for i, p in enumerate(paras) if SHORT_RE.search(p.text)]
    print(f"абзацев с короткой формой: {len(affected)}")
    if not affected:
        return

    cache: dict[str, str] = {}
    if os.path.isfile(CACHE):
        cache = json.load(open(CACHE, encoding="utf-8"))

    import concurrent.futures, threading
    lock = threading.Lock()
    stats = {"replaced": 0, "kept": 0, "fallback_keep": 0}

    def _fix(i: int) -> None:
        if str(i) in cache:
            return
        tagged = para_tagged_text(paras[i])
        try:
            resp = client.chat.completions.create(
                model=MODEL, temperature=0.1,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": tagged}],
            )
            fixed = resp.choices[0].message.content.strip()
            if not diff_is_safe(tagged, fixed):
                fixed = tagged  # сомнение → не трогаем (сохранность важнее)
                stats["fallback_keep"] += 1
            elif fixed == tagged:
                stats["kept"] += 1
            else:
                stats["replaced"] += 1
        except Exception as e:
            log.warning("абзац %d: %s — оставлен как есть", i, e)
            fixed = tagged
            stats["fallback_keep"] += 1
        with lock:
            cache[str(i)] = fixed

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_fix, affected))

    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"заменено на Джованни: {stats['replaced']}; оставлено (легитимный эпитет): "
          f"{stats['kept']}; оставлено из осторожности: {stats['fallback_keep']}")

    backup = f"{TARGET.rsplit('.', 1)[0]}_before_tyrantfix_{datetime.now():%Y%m%d_%H%M%S}.docx"
    shutil.copy2(TARGET, backup)
    applied = 0
    for i_str, fixed in cache.items():
        i = int(i_str)
        if i < len(paras) and para_tagged_text(paras[i]) != fixed:
            rewrite_para(paras[i], fixed)
            applied += 1
    doc.save(TARGET)
    print(f"бэкап: {backup}; применено изменений: {applied}")
    os.remove(CACHE)


if __name__ == "__main__":
    main()
