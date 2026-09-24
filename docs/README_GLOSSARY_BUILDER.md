# Glossary Builder — GPT-анализ текста для словаря перевода

Скрипт анализирует текст книги через GPT и строит структурированный JSON-словарь для использования с `eng_translator.py`.

## Что извлекает

- **Персонажи** — имя, русская транслитерация, пол (м/ж), склоняемость, алиасы
- **Термины мира** — магические системы, ранги, титулы, организации, предметы
- **Локации** — названия мест с переводом/транслитерацией

## Запуск

```bash
# Базовый запуск
python3 glossary_builder.py novel.epub

# С указанием выходного файла
python3 glossary_builder.py novel.docx -o glossary.json

# Крупнее чанки (больше контекста для GPT, но дороже)
python3 glossary_builder.py novel.epub --chunk-size 8000

# Слияние с существующим словарём (существующие записи приоритетны)
python3 glossary_builder.py novel.docx --merge existing_glossary.json

# Без GPT-консолидации (только локальная дедупликация, дешевле)
python3 glossary_builder.py novel.epub --no-consolidate
```

## Как работает

1. Извлекает текст из .docx/.epub
2. Разбивает на чанки (по умолчанию 6000 символов)
3. **Фаза 1:** каждый чанк отправляется GPT — извлечение имён, терминов, локаций
4. Локальная агрегация и дедупликация
5. **Фаза 2:** GPT консолидирует результаты — мержит дубликаты, разрешает конфликты
6. Сохраняет JSON-файл для ревью в VSCode

## Формат словаря

```json
{
  "meta": {
    "source_file": "novel.epub",
    "created_at": "2026-02-28T14:30:00",
    "model": "gpt-5.2",
    "version": 1
  },
  "characters": [
    {
      "original": "Pawarit",
      "translation": "Паварит",
      "gender": "m",
      "indeclinable": false,
      "aliases": ["Paw"],
      "alternatives": [],
      "notes": "main character, prince"
    }
  ],
  "terms": [
    {
      "original": "Spirit Weaving",
      "translation": "Плетение духов",
      "category": "magic_system",
      "alternatives": [],
      "notes": "primary magic system"
    }
  ],
  "locations": [
    {
      "original": "Thornwall Keep",
      "translation": "Крепость Торнволл",
      "alternatives": [],
      "notes": "northern fortress"
    }
  ]
}
```

## Пол и склоняемость

- `gender`: `"m"` / `"f"` / `"unknown"` — GPT определяет по местоимениям в тексте
- `indeclinable`: несклоняемое имя (не меняется по падежам в русском). Например, женское имя «Элис» не склоняется, а мужское «Паварит» — склоняется
- `alternatives`: если GPT предложил разные варианты перевода из разных чанков — все сохраняются для ручного выбора

## Использование с eng_translator.py

Словарь можно сразу передать в переводчик:

```bash
python3 eng_translator.py novel.docx --glossary novel_glossary.json
```

Переводчик автоматически распознаёт новый формат и передаёт GPT информацию о поле и склоняемости персонажей.

## Флаги

| Флаг | Описание | По умолчанию |
|------|----------|-------------|
| `-o`, `--output` | Путь к выходному JSON | `<input>_glossary.json` |
| `--chunk-size` | Макс. символов на чанк | 6000 |
| `--delay` | Пауза между запросами (сек) | 1.5 |
| `--merge` | Слияние с существующим словарём | — |
| `--no-consolidate` | Пропуск GPT-консолидации | — |

## Бэкап

При перезаписи существующего словаря автоматически создаётся бэкап: `glossary_backup_20260228_143000.json`

## Логи

Логи пишутся в `logs/glossary_builder_YYYY-MM-DD.log`
