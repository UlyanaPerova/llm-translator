import argparse
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта в sys.path
from common.logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("clean_read")

PROGRESS_FILE = "clean_read_progress.json"

CDP_PORT = 9222


# ── Загрузка списка глав ──────────────────────────────────────────────

def load_chapters(path: str) -> list[str]:
    """Читает chapters.txt — по одной ссылке на строку, пропуская # и пустые."""
    p = Path(path)
    if not p.exists():
        log.error("Файл %s не найден", path)
        raise SystemExit(1)

    urls = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)

    if not urls:
        log.error("В файле %s нет ссылок", path)
        raise SystemExit(1)

    log.info("Загружено %d глав из %s", len(urls), path)
    return urls


# ── Прогресс и бэкап ─────────────────────────────────────────────────

def save_progress(chapters_file: str, current: int, total: int, chapters: list[str]):
    """Сохраняет текущий прогресс в JSON-файл."""
    data = {
        "chapters_file": chapters_file,
        "current_index": current,
        "total": total,
        "processed": chapters[:current + 1],
        "timestamp": datetime.now().isoformat(),
    }
    p = Path(PROGRESS_FILE)
    # Бэкап предыдущего прогресс-файла
    if p.exists():
        backup = p.with_suffix(".json.bak")
        shutil.copy2(p, backup)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debug("Прогресс сохранён: %d/%d", current + 1, total)


def load_progress(chapters_file: str) -> int | None:
    """Загружает сохранённый прогресс. Возвращает индекс или None."""
    p = Path(PROGRESS_FILE)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("chapters_file") != chapters_file:
            log.info("Прогресс-файл от другого chapters.txt — игнорирую")
            return None
        idx = data["current_index"]
        log.info("Найден сохранённый прогресс: глава %d/%d от %s",
                 idx + 1, data["total"], data["timestamp"])
        return idx
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("Повреждённый прогресс-файл: %s", e)
        return None


def backup_chapters(path: str):
    """Создаёт резервную копию chapters.txt при первом запуске дня."""
    src = Path(path)
    if not src.exists():
        return
    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
    backup_name = f"{src.stem}_{datetime.now():%Y-%m-%d}{src.suffix}"
    dest = backup_dir / backup_name
    if not dest.exists():
        shutil.copy2(src, dest)
        log.info("Бэкап: %s → %s", src, dest)


# ── Обработка страницы ────────────────────────────────────────────────

def scroll_page(page):
    """Скролл до конца для подгрузки lazy-load контента."""
    log.debug("Скролл страницы...")
    prev = 0
    while True:
        page.evaluate("window.scrollBy(0, 800)")
        time.sleep(0.3)
        curr = page.evaluate("window.scrollY")
        if curr == prev:
            break
        prev = curr
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1)


def clean_page(page):
    """Удаление мешающих элементов со страницы."""
    log.debug("Чистка страницы...")
    page.evaluate("""
    document.querySelectorAll('.parComment').forEach(e => e.remove());

    ['headerMenu','navHeaderChapter','talkAreaBox','action_controller_footer',
     'share_article_div','donate_top_div','formCommentBox','comment_div',
     'footerContent','footerContentMobile','cookieConsentBar',
    ].forEach(id => {
        var el = document.getElementById(id);
        if(el) el.remove();
    });

    [
        '.rounded.bg-accent.p-4.mx-auto.shadow-lg.md\\\\:max-w-4xl',
        '.py-2.mb-4',
        '.mb-2.mx-auto.md\\\\:max-w-4xl',
        '.relative.mb-4',
    ].forEach(sel => {
        document.querySelectorAll(sel).forEach(e => e.remove());
    });

    document.querySelectorAll(
        'nav, .navbar, .header-menu-2021, .bottomToolbar, ' +
        '.footer-2021, #footer, .modal, .modal-backdrop, ' +
        '.google-auto-placed, footer.bg-header'
    ).forEach(e => e.remove());

    document.querySelectorAll('*').forEach(el => {
        if (el.shadowRoot && el.shadowRoot.querySelector('.ipr-container')) {
            el.remove();
        }
    });

    document.querySelectorAll('[wire\\\\:id]').forEach(e => e.remove());
    """)


def process_page(page, do_scroll: bool):
    """Полная обработка: ожидание + скролл (опционально) + очистка."""
    log.info("Жду загрузки...")
    time.sleep(10)

    if do_scroll:
        scroll_page(page)

    clean_page(page)
    log.info("Страница готова для скриншота")


# ── Подключение к Chrome ──────────────────────────────────────────────

def connect_to_chrome(pw):
    """Подключается к Chrome через CDP (remote debugging)."""
    endpoint = f"http://127.0.0.1:{CDP_PORT}"
    log.info("Подключаюсь к Chrome на %s...", endpoint)
    try:
        browser = pw.chromium.connect_over_cdp(endpoint)
    except Exception as e:
        log.error("Не удалось подключиться к Chrome: %s", e)
        print()
        print("╔════════════════════════════════════════════════════════════════════╗")
        print("║  Chrome не запущен с remote debugging.                            ║")
        print("║  Сначала закрой Chrome, потом запусти:                            ║")
        print("║                                                                   ║")
        print("║  killall -9 'Google Chrome'                                       ║")
        print("║  sleep 2                                                          ║")
        print("║  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\ ║")
        print("║    --remote-debugging-port=9222 \\                                 ║")
        print("║    --user-data-dir=/tmp/chrome-debug-profile &                    ║")
        print("║                                                                   ║")
        print("║  Потом запусти скрипт снова.                                      ║")
        print("╚════════════════════════════════════════════════════════════════════╝")
        raise SystemExit(1)

    log.info("Подключён. Контексты: %d", len(browser.contexts))
    return browser


# ── Главный цикл ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Очистка веб-страниц для скриншотов")
    parser.add_argument(
        "-f", "--file",
        default="chapters.txt",
        help="Путь к файлу со ссылками (по умолчанию chapters.txt)",
    )
    parser.add_argument(
        "--no-scroll",
        action="store_true",
        help="Пропустить скролл (быстрее, но lazy-load контент может не подгрузиться)",
    )
    args = parser.parse_args()

    chapters = load_chapters(args.file)
    do_scroll = not args.no_scroll

    # Бэкап chapters.txt
    backup_chapters(args.file)

    # Проверка сохранённого прогресса
    current = 0
    saved = load_progress(args.file)
    if saved is not None and saved < len(chapters) - 1:
        answer = input(f"Продолжить с главы {saved + 2}/{len(chapters)}? (y/n): ").strip().lower()
        if answer in ("y", "yes", "д", "да"):
            current = saved + 1
            log.info("Возобновление с главы %d", current + 1)
        else:
            log.info("Начинаю сначала")

    log.info("Старт сессии: %d глав (с %d), скролл %s",
             len(chapters), current + 1, "вкл" if do_scroll else "выкл")

    with sync_playwright() as p:
        browser = connect_to_chrome(p)

        # Берём существующий контекст Chrome (со всеми cookies и сессиями)
        context = browser.contexts[0]
        page = context.new_page()

        # Первая страница
        url = chapters[current]
        log.info("[%d/%d] Открываю %s", current + 1, len(chapters), url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        process_page(page, do_scroll)
        save_progress(args.file, current, len(chapters), chapters)
        log.info("[%d/%d] Готово — фоткай через CleanShot X", current + 1, len(chapters))

        # Цикл навигации
        while True:
            remaining = len(chapters) - current - 1
            print()
            if remaining > 0:
                prompt = f"[{current + 1}/{len(chapters)}] 'next' — следующая глава, Enter — выход: "
            else:
                prompt = f"[{current + 1}/{len(chapters)}] Все главы пройдены. Enter — выход: "

            cmd = input(prompt).strip().lower()

            if cmd == "next":
                if current + 1 >= len(chapters):
                    log.info("Все главы уже обработаны")
                    continue
                current += 1
                url = chapters[current]
                log.info("[%d/%d] Открываю %s", current + 1, len(chapters), url)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    process_page(page, do_scroll)
                    save_progress(args.file, current, len(chapters), chapters)
                    log.info("[%d/%d] Готово — фоткай", current + 1, len(chapters))
                except Exception as e:
                    log.error("[%d/%d] Ошибка: %s", current + 1, len(chapters), e)
            elif cmd == "":
                log.info("Выход по запросу пользователя")
                break
            else:
                print(f"Неизвестная команда: '{cmd}'. Введи 'next' или Enter.")

        log.info("Сессия завершена. Обработано %d/%d глав", current + 1, len(chapters))
        # Закрываем только вкладку, НЕ браузер — Chrome остаётся открытым
        page.close()
        print("Вкладка закрыта. Chrome остаётся открытым.")


if __name__ == "__main__":
    main()
