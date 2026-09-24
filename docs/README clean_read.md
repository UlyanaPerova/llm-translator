# clean_read.py — очистка веб-страниц для скриншотов

Скрипт подключается к Chrome через CDP, открывает главы по очереди из `chapters.txt`, убирает весь мусор (навигация, комментарии, реклама, футеры) и оставляет чистую страницу для скриншота через CleanShot X.

## Подготовка (один раз)

```bash
cd translation_project
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Перед каждым запуском

Закрыть Chrome полностью, потом запустить с remote debugging:

```bash
killall -9 "Google Chrome"
sleep 2
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug-profile &
```

> `--user-data-dir` обязателен, иначе Chrome откажется включить remote debugging.

## Запуск

```bash
cd translation_project
source venv/bin/activate
python3 clean_read.py
```

### Опции

| Флаг | Описание |
|------|----------|
| `-f FILE` | Путь к файлу со ссылками (по умолчанию `chapters.txt`) |
| `--no-scroll` | Пропустить скролл (быстрее, но lazy-load контент может не подгрузиться) |

Пример с другим файлом:

```bash
python3 clean_read.py -f my_chapters.txt --no-scroll
```

## Формат chapters.txt

По одной ссылке на строку. Пустые строки и строки с `#` игнорируются:

```
# Главы 381-400
https://storyseedling.com/series/223312/381/
https://storyseedling.com/series/223312/382/
...
```

## Работа со скриптом

1. Скрипт открывает первую главу, чистит страницу и ждёт
2. Делаешь скриншот через CleanShot X (scrolling capture)
3. Вводишь `next` — скрипт открывает следующую главу
4. Enter без текста — выход

## Сохранение прогресса

- Прогресс автоматически сохраняется в `clean_read_progress.json` после каждой главы
- При перезапуске скрипт предложит продолжить с того места, где остановились
- Бэкап `chapters.txt` создаётся в папке `backups/` (раз в день)

## Логи

Пишутся в `logs/clean_read_YYYY-MM-DD.log` и в консоль.

## Структура файлов

```
clean_read.py              — основной скрипт
logger.py                  — настройка логгера
chapters.txt               — список ссылок
clean_read_progress.json   — автосохранение прогресса
backups/                   — бэкапы chapters.txt
logs/                      — логи
```
