# eng_translator_qwen.py — литературный перевод EN→RU через QWEN-MT

Переводит `.docx`, `.epub`, `.md` или `.txt` с английского на литературный русский через Alibaba Cloud QWEN-MT. Сохраняет форматирование (жирный, курсив), поддерживает глоссарий, контекст между чанками и возобновление после обрыва.

## Отличия от eng_translator.py (GPT)

| | eng_translator.py | eng_translator_qwen.py |
|---|---|---|
| Модель | GPT-5.1 (OpenAI) | QWEN-MT (Alibaba DashScope) |
| API-ключ | `OPENAI_API_KEY` | `DASHSCOPE_API_KEY` |
| System-промпт | Да | Нет (QWEN-MT не поддерживает) |
| Глоссарий | Через промпт | Через `translation_options.terms` + промпт |
| Стоимость | ~$5–15 за книгу | ~$0.10–0.50 за книгу |
| Модели | Одна | 4 на выбор (plus/flash/lite/turbo) |

## Подготовка (один раз)

```bash
cd translation_project
source venv/bin/activate
pip install openai python-docx python-dotenv
# Для .epub:
pip install ebooklib beautifulsoup4 lxml
```

Добавь ключ в `.env`:

```
DASHSCOPE_API_KEY=sk-ваш-ключ
```

Получить ключ: [DashScope Console](https://dashscope-intl.console.aliyun.com/) → API Keys.

## Запуск

```bash
cd translation_project
source venv/bin/activate

# Базовый запуск
python3 eng_translator_qwen.py book.docx

# С указанием выходного файла
python3 eng_translator_qwen.py book.docx -o перевод.docx

# Из epub
python3 eng_translator_qwen.py book.epub -o перевод.docx

# Из markdown/txt
python3 eng_translator_qwen.py chapters.md -o перевод.docx
```

## Все флаги

| Флаг | Описание | По умолчанию |
|------|----------|--------------|
| `input` | Путь к файлу (.docx, .epub, .md, .txt) | обязательный |
| `-o`, `--output` | Имя выходного .docx файла | `{имя}_translated_qwen.docx` |
| `--model` | Модель QWEN-MT (см. ниже) | `qwen-mt-plus` |
| `--chunk-size` | Максимум символов в одном чанке | `4000` |
| `--context` | Абзацев контекста из предыдущего перевода | `3` |
| `--delay` | Задержка между запросами (секунды) | `1.5` |
| `--glossary` | Путь к JSON-глоссарию | нет |
| `--resume` | Продолжить с кэша (после обрыва) | выкл |

## Модели

| Модель | Качество | Скорость | Цена (input/output за 1M токенов) |
|--------|----------|----------|-----------------------------------|
| `qwen-mt-plus` | Лучшее | Медленнее | $0.259 / $0.775 |
| `qwen-mt-flash` | Хорошее | Быстрее | $0.101 / $0.280 |
| `qwen-mt-lite` | Базовое | Самый быстрый | $0.086 / $0.229 |
| `qwen-mt-turbo` | (deprecated) | — | как flash |

```bash
# Максимальное качество (по умолчанию)
python3 eng_translator_qwen.py book.docx --model qwen-mt-plus

# Быстрее и дешевле
python3 eng_translator_qwen.py book.docx --model qwen-mt-flash

# Самый дешёвый
python3 eng_translator_qwen.py book.docx --model qwen-mt-lite
```

## Глоссарий

Поддерживает два формата:

### Простой (flat)

```json
{
  "Pawarit": "Паварит",
  "Archmage": "Архимаг",
  "Gate Break": "Прорыв врат"
}
```

### Структурированный (с полом и склонением)

```json
{
  "characters": [
    {
      "original": "Kim Giryeo",
      "translation": "Ким Гирё",
      "gender": "m",
      "indeclinable": false,
      "aliases": ["Giryeo"]
    }
  ],
  "terms": [
    {"original": "Gate Break", "translation": "Прорыв врат"}
  ],
  "locations": [
    {"original": "Blue Gate", "translation": "Синие врата"}
  ]
}
```

Глоссарий передаётся двумя путями одновременно:
- **`translation_options.terms`** — нативный механизм QWEN-MT для принудительного соблюдения терминов
- **В промпте** — аннотации пола для правильного согласования в русском

```bash
python3 eng_translator_qwen.py book.docx --glossary glossary.json
```

## Контекст между чанками

По умолчанию последние 3 абзаца предыдущего перевода передаются как контекст для следующего чанка. Это обеспечивает связность текста на стыках.

```bash
# Больше контекста
python3 eng_translator_qwen.py book.docx --context 5

# Без контекста
python3 eng_translator_qwen.py book.docx --context 0
```

## Возобновление после обрыва

Кэш сохраняется после каждого чанка в файл `.{имя}_qwen_translation_cache.json`. Если скрипт упал или ты его прервала:

```bash
python3 eng_translator_qwen.py book.docx --resume
```

Скрипт подхватит с последнего успешного чанка. После успешного завершения кэш удаляется автоматически.

## Форматирование

Скрипт сохраняет **жирный** и *курсив* из исходного файла:
- Из `.docx` — извлекает bold/italic через стили ранов
- Из `.epub` — парсит `<b>`, `<strong>`, `<i>`, `<em>`, CSS-классы и inline-стили
- Из `.md` — конвертирует `**bold**` и `*italic*` в HTML-теги

Теги `<b>`, `<i>` передаются в QWEN-MT вместе с текстом, модель должна их сохранить в переводе (правило 13 в инструкциях). В выходном `.docx` теги конвертируются обратно в форматирование Word.

## Логи

Пишутся в `logs/eng_translate_qwen_YYYY-MM-DD.log` и в консоль.

## Структура файлов

```
eng_translator_qwen.py                — основной скрипт
eng_translator.py                     — аналог на GPT (OpenAI)
logger.py                             — настройка логгера
glossary_*.json                       — глоссарии
.{имя}_qwen_translation_cache.json    — кэш незавершённого перевода (автоудаление)
logs/                                 — логи
```

## Ограничения QWEN-MT

- **Нет system-сообщений** — инструкции передаются в user message
- **Лимит 8192 входных токенов** — не ставь `--chunk-size` выше ~5000
- **`terms`** работает только для терминов — пол и склонение передаются отдельно через промпт
- **Это модель перевода, не чат** — formatting transfer (pass 2) через неё работает нестабильно, поэтому отключён
