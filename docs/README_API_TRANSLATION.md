# Thai/English → Russian Literary Translator

## Первый запуск (один раз)

```bash
cd ~/путь/к/папке/Thai_translation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Запуск после перезагрузки

```bash
cd ~/путь/к/папке/Thai_translation
source venv/bin/activate
python3 thai_translator.py all_4.docx -o перевод.docx
```

## Полезные флаги

# Базовый (контекст равный трем включён по умолчанию)
python3 eng_translator.py all_4_eng.docx -o перевод.docx

# С reasoning
python3 eng_translator.py all_4_eng.docx -o перевод.docx --reasoning low

# Побольше контекста (5 абзацев) и крупнее чанки
python3 eng_translator.py all_4_eng.docx -o перевод.docx --context 5 --chunk-size 5000

# Без контекста (как было раньше)
python3 eng_translator.py all_4_eng.docx -o перевод.docx --context 0

# Глоссарий
--glossary glossary_thai_novel.json

## Смена языка перевода

Открой `thai_translator.py`, найди `SYSTEM_PROMPT` (~строка 27) и замени текст промпта.

## Если `deactivate`

Если ты вышла из виртуальной среды или перезагрузила терминал — просто снова:

```bash
source venv/bin/activate
```

Заново ставить зависимости не нужно, они сохраняются в папке `venv/`.
