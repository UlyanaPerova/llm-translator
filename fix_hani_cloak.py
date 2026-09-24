#!/usr/bin/env python3
"""
1) Хани — мальчик (source-verified: Honey, муж. местоимения 210 против 56).
   Чиним женское согласование, относящееся ИМЕННО к Хани; чужие роды не трогаем.
2) Остатки «Чёрный Плащ / Чёрная Накидка» → «Существо в чёрном плаще»
   с расширенным словарём пар согласования (она↔он(о), её↔его, весь↔всё...).
Оба прохода — GPT + пословный дифф-контроль; при сомнении абзац не меняется.
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

from logger import setup_logger
setup_logger(prefix="fix_hani_cloak")
log = logging.getLogger("fix_hani_cloak")

from docx import Document
from openai import OpenAI
from fix_name_poisoning import para_tagged_text, rewrite_para

TARGET = "The_Artist_Who_Paints_Dungeon_translated_pro.docx"
MODEL = "gpt-5.1"

# симметричные пары согласования (муж/жен/ср род коротких слов)
AGREEMENT_PAIRS = {
    frozenset(p) for p in (
        ("он", "она"), ("он", "оно"), ("она", "оно"),
        ("его", "её"), ("него", "неё"), ("ему", "ей"), ("нему", "ней"),
        ("им", "ею"), ("ним", "ней"), ("нём", "ней"),
        ("сам", "сама"), ("сам", "само"), ("сама", "само"),
        ("весь", "вся"), ("весь", "всё"), ("вся", "всё"),
        ("тот", "та"), ("тот", "то"), ("та", "то"),
        ("этот", "эта"), ("этот", "это"), ("эта", "это"),
        ("один", "одна"), ("один", "одно"),
        ("его", "него"), ("её", "неё"), ("ему", "нему"), ("ей", "ней"),
    )
}

def _strip(w): return w.strip("«»\"'()[].,!?;:…—-").lower()

def is_agreement(x: str, y: str) -> bool:
    xs, ys = _strip(x), _strip(y)
    if not xs or not ys:
        return False
    if frozenset((xs, ys)) in AGREEMENT_PAIRS:
        return True
    cp = os.path.commonprefix([xs, ys])
    if len(cp) >= 4:
        return True
    # отличие только в последней букве: была↔было, шла↔шло, был↔была
    if len(xs) >= 3 and len(ys) >= 3 and xs[:-1] == ys[:-1]:
        return True
    if xs == ys[:-1] or ys == xs[:-1]:
        return True
    return len(cp) == min(len(xs), len(ys)) >= 2 and abs(len(xs) - len(ys)) <= 2


CLOAK_RE = re.compile(
    r"Ч[её]рн(?:ый|ого|ому|ым|ом)\s+Плащ[а-яё]*(?!-)|Ч[её]рн(?:ая|ой|ую)\s+Накидк[а-яё]*")
CLOAK_WORDS_RE = re.compile(r"^[«»\"'(\[]?(Ч[её]рн\w*|Плащ\w*|Накидк\w*)[.,!?;:…»\"')\]]*$")
CLOAK_INSERT_RE = re.compile(r"^[«»\"'(\[]?(Существ[оаеу]м?|в|ч[её]рном|плаще)[.,!?;:…»\"')\]]*$")

FEM_MARK_RE = re.compile(r"\b(она|её|ей|неё|ней|сама)\b|[а-яё]+ла(?:сь)?\b")

CLOAK_PROMPT = """You are a precise text-repair tool for a Russian novel translation.
Replace every form of «Чёрный Плащ» / «Чёрная Накидка» with «Существо в чёрном плаще»
in the correct grammatical case. «Существо» is NEUTER — retune agreement of dependent
words (verbs, adjectives, pronouns: «он»→«оно», «сказал»→«сказало»).
Never touch «Чёрный Плащ-ним». Change nothing else. Return ONLY the paragraph text."""

HANI_PROMPT = """You are a precise text-repair tool for a Russian novel translation.
The character «Хани» is a BOY (male). In the paragraph, find feminine grammatical forms
that refer to Хани (past-tense verbs «сказала»→«сказал», pronouns «она»→«он»,
«её»→«его», adjectives, «сама»→«сам») and change them to masculine.
CRITICAL: only forms whose referent is Хани. Feminine forms referring to OTHER
(female) characters must stay untouched. If nothing refers to Хани in feminine,
return the paragraph unchanged. Change nothing else. Return ONLY the paragraph text."""


def make_guard(words_re, insert_re):
    def guard(original: str, fixed: str) -> bool:
        a, b = original.split(), fixed.split()
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            removed, inserted = a[i1:i2], b[j1:j2]
            for w in inserted:
                if insert_re and insert_re.match(w):
                    continue
                if any(is_agreement(w, r) for r in removed):
                    continue
                return False
            for w in removed:
                if words_re and words_re.match(w):
                    continue
                if any(is_agreement(w, ins) for ins in inserted):
                    continue
                return False
        return True
    return guard


cloak_guard = make_guard(CLOAK_WORDS_RE, CLOAK_INSERT_RE)
hani_guard = make_guard(None, None)  # только пары согласования, ничего больше


def run_pass(client, paras, indices, prompt, guard, label, post_check=None):
    import concurrent.futures, threading
    lock = threading.Lock()
    stats = {"ok": 0, "kept": 0, "guard": 0, "error": 0}
    results = {}

    def _fix(i):
        tagged = para_tagged_text(paras[i])
        try:
            resp = client.chat.completions.create(
                model=MODEL, temperature=0.1,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": tagged}])
            fixed = resp.choices[0].message.content.strip()
            if fixed == tagged:
                stats["kept"] += 1
            elif guard(tagged, fixed) and (post_check is None or post_check(fixed)):
                stats["ok"] += 1
            else:
                fixed = tagged
                stats["guard"] += 1
        except Exception as e:
            log.warning("%s абзац %d: %s", label, i, e)
            fixed = tagged
            stats["error"] += 1
        with lock:
            results[i] = fixed

    with __import__("concurrent.futures", fromlist=["x"]).ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_fix, indices))
    applied = 0
    for i, fixed in results.items():
        if para_tagged_text(paras[i]) != fixed:
            rewrite_para(paras[i], fixed)
            applied += 1
    print(f"{label}: изменено={applied}, ok={stats['ok']}, без изм.={stats['kept']}, "
          f"страж={stats['guard']}, ошибок={stats['error']}")


def main():
    api_key = os.getenv("OPENAI_API_KEY") or sys.exit("OPENAI_API_KEY не найден")
    client = OpenAI(api_key=api_key)
    doc = Document(TARGET)
    paras = doc.paragraphs

    backup = f"{TARGET.rsplit('.',1)[0]}_before_hani_{datetime.now():%Y%m%d_%H%M%S}.docx"
    shutil.copy2(TARGET, backup)
    print(f"бэкап: {backup}")

    cloak_idx = [i for i, p in enumerate(paras) if CLOAK_RE.search(p.text)]
    print(f"Плащ/Накидка: {len(cloak_idx)} абзацев")
    run_pass(client, paras, cloak_idx, CLOAK_PROMPT, cloak_guard, "Плащ",
             post_check=lambda t: not CLOAK_RE.search(t))

    hani_idx = [i for i, p in enumerate(paras)
                if "Хани" in p.text and FEM_MARK_RE.search(p.text)]
    print(f"Хани + жен. маркеры: {len(hani_idx)} абзацев")
    run_pass(client, paras, hani_idx, HANI_PROMPT, hani_guard, "Хани")

    doc.save(TARGET)

    doc2 = Document(TARGET)
    text = "\n".join(p.text for p in doc2.paragraphs)
    print("осталось Плащ/Накидка:", len(CLOAK_RE.findall(text)))
    fem = re.findall(r"Хани\s+[а-яё]+ла\b", text)
    print("явных «Хани + глагол-ла»:", len(fem))


if __name__ == "__main__":
    main()
