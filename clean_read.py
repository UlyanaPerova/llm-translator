import argparse
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("clean_read")

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
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  Chrome не запущен с remote debugging.                      ║")
        print("║  Запусти его командой:                                      ║")
        print("║                                                             ║")
        print('║  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\    ║')
        print('║  Chrome --remote-debugging-port=9222                        ║')
        print("║                                                             ║")
        print("║  Потом запусти скрипт снова.                                ║")
        print("╚══════════════════════════════════════════════════════════════╝")
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
    current = 0

    log.info("Старт сессии: %d глав, скролл %s", len(chapters), "вкл" if do_scroll else "выкл")

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
