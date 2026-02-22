1. Для начала зайти в папку, где лежит `clean_read.py`:

```
cd /Users/ulyanaperova/Pet_projects/Books/deobfuscation
```

2. Открыть страницу "чистой" для дальнейшей работы с CleanShotX (до этого скачав firefox через playwright)

```
python3 -m venv venv
source venv/bin/activate
pip install playwright
playwright install firefox
python3 clean_read.py "ВАША_ССЫЛКА_НА_САЙТЕ_READAWRITE.COM"
```

3. Потом ждём немного и запускаем через верхнюю панель scrolling capture. 
4. Сохраняем