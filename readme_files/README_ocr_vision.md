# ocr_vision.py — OCR скриншотов через AI Vision → .docx

Распознаёт текст со скриншотов (PNG) через Gemini 2.5 Flash или GPT-4o, сохраняет в `.docx` с форматированием (жирный, курсив). Автоматически разрезает слишком длинные скриншоты (scrolling capture) на части.

## Подготовка (один раз)

```bash
cd /Users/ulyanaperova/Code/translation_project
source venv/bin/activate
pip install google-genai Pillow python-docx python-dotenv openai
```

В `.env` нужен хотя бы один ключ:

```
GEMINI_API_KEY=ваш-ключ      # https://aistudio.google.com/apikey
OPENAI_API_KEY=sk-ваш-ключ   # для --provider openai
```

## Запуск

```bash
cd /Users/ulyanaperova/Code/translation_project
source venv/bin/activate

# Все PNG из screenshots/ (по умолчанию)
python3 ocr_vision.py -o chapters_380_415.docx

# Из другой папки
python3 ocr_vision.py -s screenshots_2 -o chapters_416_450.docx

# Конкретные файлы
python3 ocr_vision.py screenshots/381.png screenshots/382.png -o two_chapters.docx

# Тест на одном файле (вывод в консоль, без сохранения)
python3 ocr_vision.py --test screenshots/381.png
```

## Все флаги

| Флаг | Описание | По умолчанию |
|------|----------|--------------|
| `images` | PNG-файлы (если не указаны — берутся из `--source-dir`) | — |
| `-s`, `--source-dir` | Папка с PNG | `screenshots/` |
| `-o`, `--output` | Имя выходного .docx файла (если не указано — спросит) | — |
| `--test` | Тест на одном файле — вывод в консоль, без сохранения | — |
| `--provider` | AI-провайдер: `gemini` или `openai` | `gemini` |
| `--fresh` | Начать заново, игнорируя кэш | выкл |

## Провайдеры

| Провайдер | Модель | Цена за скриншот | API-ключ |
|-----------|--------|------------------|----------|
| `gemini` | Gemini 2.5 Flash | ~$0.002 | `GEMINI_API_KEY` |
| `openai` | GPT-4o | ~$0.003 | `OPENAI_API_KEY` |

```bash
# Gemini (по умолчанию, дешевле)
python3 ocr_vision.py -o result.docx

# GPT-4o (если Gemini не работает)
python3 ocr_vision.py --provider openai -o result.docx
```

## Кэш и возобновление

Кэш (`.ocr_vision_cache.json`) загружается **автоматически** при каждом запуске — не нужно указывать никакой флаг.

- После каждого распознанного скриншота результат сохраняется в кэш (атомарная запись)
- При повторном запуске уже распознанные файлы пропускаются
- Если прервать на 20-й главе из 35, при следующем запуске начнётся с 21-й
- `.docx` промежуточно сохраняется каждые 5 глав

Чтобы начать заново (сбросить кэш):

```bash
python3 ocr_vision.py --fresh -o result.docx
```

Или удалить кэш вручную:

```bash
rm .ocr_vision_cache.json
```

## Автонарезка больших изображений

Скриншоты с высотой больше **8000 пикселей** (например, длинные scrolling capture) автоматически разрезаются на части с перекрытием 200 px. Каждая часть распознаётся отдельно, затем результаты склеиваются с дедупликацией строк на стыках.

Пример из лога:

```
Изображение 415.png слишком высокое (43392 px) — разрезаю на части...
  Часть 1: y=0..8000 (8000 px)
  Часть 2: y=7800..15800 (8000 px)
  ...
  Часть 6: y=39000..43392 (4392 px)
  Склеено 6 частей → 16579 символов
```

Временные файлы частей удаляются автоматически после обработки.

## Форматирование в .docx

Модель возвращает текст с HTML-тегами (`<b>`, `<i>`) или markdown (`**bold**`, `*italic*`). Скрипт понимает оба формата и конвертирует в форматирование Word:

- Times New Roman 12pt
- Поля: 2 см сверху/снизу, 2.5 см слева/справа
- Отступ первой строки: 1.25 см
- Каждый файл начинается с заголовка `Chapter {номер}`

## Ошибки и retry

- **429 (Rate Limit)**: автоматически ждёт 30/60/90 секунд и повторяет (до 3 попыток)
- **Ошибка OCR**: повторяет через 10 секунд, при повторной ошибке пропускает файл с пометкой `[OCR ERROR]`
- **400 (INVALID_ARGUMENT)**: обычно из-за слишком большого изображения — автонарезка решает эту проблему

## Логи

Пишутся в `logs/clean_read_YYYY-MM-DD.log` и в консоль.

## Структура файлов

```
ocr_vision.py                 — основной скрипт
logger.py                     — настройка логгера
.ocr_vision_cache.json         — кэш распознанных файлов (автоудаление не предусмотрено)
screenshots/                   — папка со скриншотами по умолчанию
logs/                          — логи
```

## Типичный воркфлоу

```bash
# 1. Сделать скриншоты через CleanShot X (scrolling capture)
# 2. Переименовать по номерам глав
# 3. Распознать
python3 ocr_vision.py -s screenshots -o chapters_380_415.docx

# 4. Проверить результат, если нужно повторить конкретный файл:
python3 ocr_vision.py --test screenshots/415.png

# 5. Отправить .docx на перевод
python3 eng_translator_qwen.py chapters_380_415.docx -o перевод.docx --glossary glossary.json
```
