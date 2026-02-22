import sys
import time
from playwright.sync_api import sync_playwright
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("clean_read") 


def process_page(page):
    """Скролл + очистка текущей страницы."""
    print("Жду загрузки...")
    time.sleep(10)

    print("Скроллю...")
    prev = 0
    while True:
        page.evaluate("window.scrollBy(0, 800)")
        time.sleep(0.3)
        curr = page.evaluate("window.scrollY")
        if curr == prev:
            break
        prev = curr

    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(2)

    print("Чищу страницу...")
    page.evaluate("""
    document.querySelectorAll('.parComment').forEach(e => e.remove());

    // Удаление по ID
    ['headerMenu','navHeaderChapter', 'talkAreaBox','action_controller_footer',
     'share_article_div','donate_top_div','formCommentBox','comment_div',
     'footerContent','footerContentMobile','cookieConsentBar',
    ].forEach(id => {
        var el = document.getElementById(id);
        if(el) el.remove();
    });

    // Удаление по точным наборам классов
    [
        '.rounded.bg-accent.p-4.mx-auto.shadow-lg.md\\\\:max-w-4xl',
        '.py-2.mb-4',
        '.mb-2.mx-auto.md\\\\:max-w-4xl',
        '.relative.mb-4',
    ].forEach(sel => {
        document.querySelectorAll(sel).forEach(e => e.remove());
    });

    // Навигация, футеры, реклама и т.д.
    document.querySelectorAll(
        'nav, .navbar, .header-menu-2021, .bottomToolbar, ' +
        '.footer-2021, #footer, .modal, .modal-backdrop, ' +
        '.google-auto-placed, footer.bg-header'
    ).forEach(e => e.remove());

    // Удаление элементов с Shadow DOM, содержащих ipr-container
    document.querySelectorAll('*').forEach(el => {
        if (el.shadowRoot && el.shadowRoot.querySelector('.ipr-container')) {
            el.remove();
        }
    });

    // Livewire-компоненты (комментарии)
    document.querySelectorAll('[wire\\\\:id]').forEach(e => e.remove());
""")

    print("✅ Готово! Фоткай через CleanShot X.")


def main():
    if len(sys.argv) < 2:
        print('Использование: python clean_read.py "URL"')
        sys.exit(1)

    url = sys.argv[1]

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        page = browser.new_page(viewport={"width": 1600, "height": 900})

        # Открываем первую ссылку
        print(f"Открываю {url}...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        # Обрабатываем первую страницу (перезагружаем после логина)
        print(f"Перезагружаю {url} после логина...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        process_page(page)

        # Цикл для следующих ссылок
        while True:
            print()
            next_url = input("Вставь следующую ссылку (или 'q' для выхода): ").strip()
            if not next_url or next_url.lower() == "q":
                break
            print(f"Открываю {next_url}...")
            page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
            process_page(page)

        print("Закрываю браузер...")
        browser.close()


if __name__ == "__main__":
    main()