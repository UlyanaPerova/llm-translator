# Image Split & OCR — пошаговая инструкция

## 1. Перейти в папку с проектом

```bash
cd path/to/screenshots_folder
```

## 2. Создать виртуальное окружение (один раз)

```bash
python3 -m venv .venv
```

## 3. Активировать окружение

```bash
source .venv/bin/activate
```

## 4. Установить зависимости (один раз, после создания окружения)

```bash
pip install Pillow numpy
```

## 5. Запустить скрипт

```bash
python3 imagedivide.py
```

## 6. Выйти из окружения (когда закончила)

```bash
deactivate
```

---

## В следующий раз

Шаги 2 и 4 уже не нужны. Достаточно:

```bash
cd path/to/screenshots_folder
source .venv/bin/activate
python3 imagedivide.py
deactivate
```
