"""
Единая точка входа: сама выбирает переводчик по тому, какой API-ключ есть.

    python3 translate.py book.epub                    # движок по ключу из .env
    python3 translate.py book.epub --ask-key          # спросить ключ, определить модель, перевести
    python3 translate.py book.epub --engine openai    # принудительно
    python3 translate.py book.epub --parallel 8       # прочие флаги уходят в сам переводчик
    python3 translate.py --add-key                    # только определить ключ и сохранить в .env
    python3 translate.py --which-keys                 # какие ключи найдены

Приоритет при нескольких ключах: Gemini → OpenAI → Qwen.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.api_keys import (  # noqa: E402
    PROVIDERS, ask_key, available_providers, save_key, use_key,
)

ENGINES = {
    "gemini": ROOT / "translators" / "eng_translator_gemini.py",
    "openai": ROOT / "translators" / "eng_translator.py",
    "qwen": ROOT / "translators" / "eng_translator_qwen.py",
}
PRIORITY = ["gemini", "openai", "qwen"]


def main():
    parser = argparse.ArgumentParser(
        description="EN→RU перевод книги: движок выбирается по доступному API-ключу. "
                    "Неизвестные флаги передаются выбранному переводчику.",
    )
    parser.add_argument("input", nargs="?", help=".epub, .docx, .md или .txt")
    parser.add_argument("--engine", choices=["auto", *PRIORITY], default="auto",
                        help="Переводчик (по умолчанию auto — по ключу)")
    parser.add_argument("--ask-key", action="store_true",
                        help="Спросить API-ключ, определить по нему модель и переводить ею")
    parser.add_argument("--add-key", action="store_true",
                        help="Только определить провайдера ключа и сохранить его в .env")
    parser.add_argument("--which-keys", action="store_true",
                        help="Показать, для каких провайдеров есть ключи")
    args, engine_args = parser.parse_known_args()

    if args.which_keys:
        have = available_providers()
        for p in PRIORITY:
            mark = "✓" if p in have else "—"
            print(f"  {mark} {PROVIDERS[p]['name']:<26} {PROVIDERS[p]['env']}")
        return

    if args.add_key:
        provider, key = ask_key()
        print(f"✅ Сохранено в .env как {save_key(provider, key)}")
        return

    if not args.input:
        parser.error("укажи файл книги")

    if args.ask_key:
        expected = None if args.engine == "auto" else args.engine
        engine, key = ask_key(expected=expected)
        use_key(engine, key)
    elif args.engine != "auto":
        engine = args.engine  # переводчик сам спросит ключ, если его нет
    else:
        have = available_providers()
        engine = next((p for p in PRIORITY if p in have), None)
        if engine is None:
            print("⚠️  В .env нет ни одного API-ключа.")
            engine, key = ask_key()
            use_key(engine, key)

    print(f"🚀 Движок: {PROVIDERS[engine]['name']} ({ENGINES[engine].relative_to(ROOT)})\n")
    cmd = [sys.executable, str(ENGINES[engine]), args.input, *engine_args]
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
