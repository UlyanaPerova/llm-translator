# merge_png — объединение больших PNG-файлов

Скрипт для склейки PNG-изображений любого размера по вертикали или горизонтали.

---

## Запуск с нуля (после перезагрузки)

Открой **Terminal** (`Cmd + Space` → «Terminal») и выполни:

```bash
cd "path/to/images_folder"
```

### Первый запуск (один раз)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install Pillow
```

### Повторный запуск (после перезагрузки)

```bash
source .venv/bin/activate
```

---

## Использование

### Вертикально (друг под другом) — по умолчанию

```bash
python merge_png.py page1.png page2.png page3.png -o result.png
```

### Все PNG в папке, вертикально

```bash
python merge_png.py *.png -o result.png
```

### Горизонтально (в ряд)

```bash
python merge_png.py *.png -o result.png -d horizontal
```

### С отступами между изображениями

```bash
python merge_png.py *.png -o result.png --gap 20
```

### Прозрачный фон (вместо белого)

```bash
python merge_png.py *.png -o result.png --bg transparent
```

### Подогнать все изображения под одну ширину

```bash
python merge_png.py *.png -o result.png --resize fit_width
```

### Подогнать все изображения под одну высоту

```bash
python merge_png.py *.png -o result.png --resize fit_height
```

### Комбинирование параметров

```bash
python merge_png.py *.png -o result.png -d horizontal --gap 10 --bg black --resize fit_height
```

---

## Все параметры

| Параметр | Значение | По умолчанию | Описание |
|---|---|---|---|
| `images` | пути к файлам | — | PNG-файлы для объединения (можно `*.png`) |
| `-o`, `--output` | путь | `merged.png` | Имя результата |
| `-d`, `--direction` | `vertical` / `horizontal` | `vertical` | Направление склейки |
| `--gap` | число | `0` | Отступ между изображениями (px) |
| `--bg` | `white` / `black` / `transparent` | `white` | Цвет фона и отступов |
| `--resize` | `none` / `fit_width` / `fit_height` | `none` | Подгонка размеров |

---

## Примечания

- Лимит на размер изображений снят — файлы любого разрешения обработаются.
- Если изображения разной ширины (при вертикальной склейке) — они центрируются.
- `*.png` подставляет файлы в алфавитном порядке. Если нужен конкретный порядок — перечисляй файлы вручную.
