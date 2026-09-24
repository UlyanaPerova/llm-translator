"""
API-ключи: загрузка из .env, запрос у пользователя и автоопределение провайдера.

Провайдер определяется по формату ключа, а затем подтверждается бесплатным
запросом списка моделей (GET /models) — так различаются ключи с одинаковым
префиксом `sk-` (OpenAI и Alibaba DashScope).

Использование в скриптах:
    from common.api_keys import require_key
    api_key = require_key("openai")   # из .env, а если нет — спросит в терминале

Отдельно (добавить ключ в .env):
    python3 -m common.api_keys
"""
import getpass
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)

PROVIDERS = {
    "gemini": {
        "name": "Google Gemini",
        "env": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        "header": lambda k: {"x-goog-api-key": k},
        "get_key": "https://aistudio.google.com/apikey",
    },
    "openai": {
        "name": "OpenAI",
        "env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/models",
        "header": lambda k: {"Authorization": f"Bearer {k}"},
        "get_key": "https://platform.openai.com/api-keys",
    },
    "qwen": {
        "name": "Alibaba DashScope (Qwen)",
        "env": "DASHSCOPE_API_KEY",
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "header": lambda k: {"Authorization": f"Bearer {k}"},
        "get_key": "https://modelstudio.console.alibabacloud.com/?tab=playground#/api-key",
    },
}


def guess_providers(key: str) -> list[str]:
    """Кандидаты по формату ключа, от самого вероятного к менее вероятному."""
    key = key.strip()
    if key.startswith("AIza"):
        return ["gemini"]
    if key.startswith(("sk-proj-", "sk-svcacct-", "sk-admin-", "sk-None-")):
        return ["openai"]
    if key.startswith("sk-ant-"):
        return []  # Anthropic — этим проектом не поддерживается
    if re.fullmatch(r"sk-[0-9a-f]{32}", key):
        return ["qwen", "openai"]  # формат DashScope
    if key.startswith("sk-"):
        return ["openai", "qwen"]
    return list(PROVIDERS)


def verify_key(provider: str, key: str, timeout: float = 10) -> bool | None:
    """True — ключ рабочий, False — отвергнут, None — проверить не удалось (сеть)."""
    p = PROVIDERS[provider]
    req = urllib.request.Request(p["url"], headers=p["header"](key.strip()))
    for _ in range(3):  # сеть/TLS иногда рвётся — повторяем
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                json.load(r)
                return True
        except urllib.error.HTTPError as e:
            return False if e.code in (400, 401, 403) else None
        except (OSError, http.client.HTTPException, ValueError):
            continue  # обрыв соединения / таймаут
    return None


def detect_provider(key: str, verify: bool = True) -> tuple[str | None, bool | None]:
    """Определяет провайдера ключа. Возвращает (провайдер, подтверждён ли запросом)."""
    candidates = guess_providers(key)
    if not candidates:
        return None, None
    if not verify:
        return candidates[0], None
    unverifiable = None
    for provider in candidates:
        ok = verify_key(provider, key)
        if ok:
            return provider, True
        if ok is None and unverifiable is None:
            unverifiable = provider
    if unverifiable:
        return unverifiable, None  # сеть недоступна — верим формату
    return None, False


def available_providers() -> list[str]:
    """Провайдеры, для которых ключ уже есть в окружении/.env."""
    return [p for p, cfg in PROVIDERS.items() if os.getenv(cfg["env"])]


def save_key(provider: str, key: str) -> str:
    """Записывает ключ в .env. Второй и следующие ключи Gemini → GEMINI_API_KEY_2, _3…
    Возвращает имя переменной."""
    var = PROVIDERS[provider]["env"]
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    existing = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            existing[name.strip()] = value.strip().strip("\"'")

    if key in existing.values():
        return next(n for n, v in existing.items() if v == key)

    if provider == "gemini" and existing.get(var):
        n = 2
        while existing.get(f"{var}_{n}"):
            n += 1
        var = f"{var}_{n}"

    if existing.get(var) is not None:
        lines = [f"{var}={key}" if l.split("=", 1)[0].strip() == var else l for l in lines]
    else:
        lines.append(f"{var}={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)
    return var


def ask_key(expected: str | None = None) -> tuple[str, str]:
    """Спрашивает ключ в терминале (ввод скрыт), определяет провайдера.
    expected — нужный скрипту провайдер; ключ другого провайдера не принимается.
    Возвращает (провайдер, ключ)."""
    if not sys.stdin.isatty():
        sys.exit("Ключ не найден, а терминал не интерактивный. Добавь ключ в .env "
                 "(см. .env.example).")
    want = f" {PROVIDERS[expected]['name']}" if expected else ""
    while True:
        key = getpass.getpass(f"🔑 Вставь API-ключ{want} (ввод скрыт, пусто — выход): ").strip()
        if not key:
            sys.exit("Ключ не введён.")
        print("   Определяю провайдера…", end=" ", flush=True)
        provider, verified = detect_provider(key)
        if provider is None:
            print("не удалось." if verified is False else "формат не поддерживается.")
            print("   Ключ не подошёл ни к Gemini, ни к OpenAI, ни к DashScope. Попробуй ещё раз.")
            continue
        status = "ключ рабочий ✓" if verified else "проверить не удалось (нет сети), определено по формату"
        print(f"{PROVIDERS[provider]['name']} — {status}")
        if expected and provider != expected:
            print(f"   Этому скрипту нужен ключ {PROVIDERS[expected]['name']}. "
                  f"Получить: {PROVIDERS[expected]['get_key']}")
            continue
        return provider, key


def use_key(provider: str, key: str, offer_save: bool = True) -> None:
    """Делает ключ доступным текущему процессу и (по желанию) сохраняет в .env."""
    os.environ[PROVIDERS[provider]["env"]] = key
    if offer_save and sys.stdin.isatty():
        ans = input("   Сохранить ключ в .env, чтобы больше не спрашивать? [Y/n] ").strip().lower()
        if ans in ("", "y", "yes", "д", "да"):
            var = save_key(provider, key)
            print(f"   Сохранено в .env как {var}")


def require_key(provider: str) -> str:
    """Ключ провайдера из окружения/.env; если его нет — спрашивает у пользователя."""
    key = os.getenv(PROVIDERS[provider]["env"])
    if key:
        return key
    print(f"⚠️  {PROVIDERS[provider]['env']} не найден в .env")
    _, key = ask_key(expected=provider)
    use_key(provider, key)
    return key


def main():
    provider, key = ask_key()
    var = save_key(provider, key)
    print(f"✅ Сохранено в .env как {var}")


if __name__ == "__main__":
    main()
