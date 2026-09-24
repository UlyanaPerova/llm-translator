# LLM Novel Translator (EN → RU)

A toolkit for translating web novels from **English into Russian** with LLMs — from capturing
chapters in the browser to a finished, formatted `.epub`.

The pipeline is built around a few ideas:

- **Glossary first.** Before translating, a glossary of characters, terms and locations is
  extracted from the whole book, with gender, declension rules and "ты/вы" relations between
  characters. It is injected per chunk, so names stay consistent across hundreds of chapters.
- **Several translation engines.** The same pipeline works with OpenAI GPT, Alibaba Qwen-MT and
  Google Gemini, so engines can be compared on quality and cost.
- **Automatic QA on every chunk.** Russian punctuation (« » quotes, em-dash dialogue), leftover
  English words, truncated output and transliteration drift are detected and fixed.
- **Nothing is paid for twice.** Every long-running step caches its progress and can resume
  after a crash or a hit quota.
- **Paste any key, the program figures out the rest.** `translate.py` detects whether a key
  belongs to Gemini, OpenAI or Alibaba DashScope and runs the matching translator.

![Translation run in the terminal](docs/images/demo_translate.png)

![Source and translation side by side](docs/images/demo_result.png)

<sub>The demo chapter in [`examples/sample_chapter.md`](examples/sample_chapter.md) is an original
text written for this README; the translation above is the real, unedited output of the run shown.</sub>

## Timeline

The translator was developed between **22 February 2026** and **21 July 2026**.

| Period | Milestone |
|---|---|
| Feb 22, 2026 | Initial commit: browser capture (`clean_read.py`), GPT translator, `docx2epub.py` |
| Feb 28, 2026 | `glossary_builder.py` — GPT glossary extraction, per-chunk glossary filtering |
| Mar 1, 2026 | Formatting preservation (two-pass: translate clean, then transfer bold/italic) |
| Mar 3–10, 2026 | `docx2epub` fixes, `merge_and_split.py` (merge + slice screenshots) |
| Mar 13–15, 2026 | Qwen-MT translator, `ocr_vision.py`, `fix_english_remnants.py`, `heading.py` |
| Jul 20–21, 2026 | Gemini translator with QA pipeline, parallel/batch modes; repair and canon-fix scripts; glossary gender/declension/relations |

Git does not store file creation dates. Files that were committed later were committed one by one
with their original file timestamps as both author and committer date
(`GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE`), so the commit history reflects when each file was
actually written.

## Pipeline

```
 web chapters ─► capture/clean_read.py ─► screenshots/*.png
                                        │
                          capture/merge_and_split.py (merge, slice on blank bands, 300 DPI)
                                        │
                             capture/ocr_vision.py (Gemini / GPT-4o Vision → .docx)
                                        │
             source .docx / .epub / .md / .txt
                                        │
                        glossary/glossary_builder.py (→ <book>_glossary.json)
                                        │
                     translate.py  (picks the engine by API key)
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
 eng_translator.py            eng_translator_qwen.py           eng_translator_gemini.py
   (OpenAI GPT)                  (Qwen-MT)                        (Gemini)
        └───────────────────────────────┼───────────────────────────────┘
                                        │
          postedit/ scripts (failed chunks, names, leftovers)
                                        │
              export/heading.py  (chapter headings, page breaks)
                                        │
             export/docx2epub.py (→ .epub with cover and TOC)
```

## Project structure

```
translate.py            entry point: detects the API key's provider and runs that translator
capture/                browser capture, screenshot merge/slice, Vision OCR
glossary/               glossary builder + fixed LitRPG term templates
translators/            GPT, Qwen-MT and Gemini translators
postedit/               repair and diff-guarded fix scripts
export/                 chapter headings, DOCX → EPUB
common/                 logging, API key loading / prompting / provider detection
docs/                   per-script docs (Russian) and README images
examples/               demo chapter
```

Scripts can be run from anywhere (`python3 translators/eng_translator.py …`); each one adds the
project root to `sys.path` and reads `.env` from the root. Logs go to `logs/`.

### `capture/` — capture and OCR

| File | What it does |
|---|---|
| `clean_read.py` | Attaches to a running Chrome over the DevTools protocol (Playwright, port 9222) so existing logins and cookies are reused. Opens each URL from `chapters.txt`, hides site chrome, takes screenshots. Saves progress and can resume. |
| `merge_and_split.py` | Merges PNGs vertically and slices them into pages, cutting on blank horizontal bands so lines of text are never split; sets 300 DPI. |
| `ocr_vision.py` | OCR of screenshots through Gemini 2.5 Flash (default) or GPT-4o Vision, keeping bold/italic, written to `.docx`. Results are cached. |

### `glossary/` — glossary

| File | What it does |
|---|---|
| `glossary_builder.py` | Extracts characters, terms and locations chunk by chunk with GPT, consolidates each category in batches, deduplicates aliases, counts occurrences in the source and drops rare entries. Infers gender from surrounding pronouns, applies Russian declension rules, and builds a "ты/вы" relation matrix from dialogue excerpts. Caches raw results (`--from-raw`) and merges with a hand-edited glossary. |
| `litrpg_glossary.json` | Fixed translations for LitRPG stats and system terms (Level → Уровень, Strength → Сила…). |

### `translators/` — translators

All three read `.docx`, `.epub`, `.md` or `.txt`, split text into chunks with a few paragraphs of
overlap for context, inject only the glossary entries that occur in the current chunk, and write
a formatted `.docx`.

| File | Engine | Notes |
|---|---|---|
| `eng_translator.py` | OpenAI GPT (`gpt-5.1` by default) | Base translator. Two-pass formatting: translate clean text, then transfer `<b>`/`<i>` tags in a separate call. Output-truncation detection, translation cache. Its helpers are shared by the other scripts. |
| `eng_translator_qwen.py` | Qwen-MT (`qwen-mt-plus`) via Alibaba DashScope, OpenAI-compatible API | Qwen-MT has no system prompt, so instructions go into the user message and the glossary is passed through the native `terms` parameter. |
| `eng_translator_gemini.py` | Google Gemini (`gemini-2.5-flash` / `gemini-2.5-pro`) | Smaller chunks plus a rules reminder at the end of every request. Per-chunk checks for punctuation, leftover English and completeness, with a cheap correction call and regex fixes. Optional story summaries as context (`--summaries`), `--parallel N`, Gemini Batch API (`--batch`, half price), rotation across several API keys when a daily quota is hit, cost estimate and actual spend report, transliteration drift detector (Levenshtein). |

### `postedit/` — repair and post-editing

| File | What it does |
|---|---|
| `repair_pro_translation.py` | Re-translates only chunks that failed and replaces the error markers in the `.docx`. |
| `fix_english_remnants.py` | Finds English phrases left in a translated `.docx` and translates them with Gemini Flash. |
| `fix_name_poisoning.py` | Fixes names corrupted by a glossary alias clash. |
| `fix_tyrant_short.py` | Decides from context whether an epithet refers to a named state or to the character, and replaces it only in the second case. |
| `fix_hani_cloak.py` | Fixes grammatical gender agreement for one character and replaces a leftover epithet. |
| `apply_canon_rules.py` | Applies the final naming canon (names, honorifics) in the correct grammatical case. |

These scripts share one safety mechanism: GPT rewrites a paragraph, then a **word-level diff** checks
that only the allowed words changed. If the model touched anything else, its version is rejected and
a deterministic replacement is used or the paragraph is left alone. They all back up the `.docx` and
cache progress. Some of them are specific to one book.

### `export/` and `common/`

| File | What it does |
|---|---|
| `heading.py` | Removes duplicate chapter headings, applies Heading 1 and page breaks, saves a backup. |
| `docx2epub.py` | Converts `.docx` to EPUB and keeps indents, bold/italic/underline, fonts, colors, alignment, lists, images, footnotes, links, tables and headings. Adds a cover and TOC. |
| `common/logger.py` | Shared logging: console + daily files in `logs/`. |
| `common/api_keys.py` | Loads keys from `.env`; if a key is missing, asks for it in the terminal (hidden input), detects the provider and offers to save it to `.env`. |

Per-script documentation (in Russian) is in [`docs/`](docs).

## Tools and libraries

- **Python 3.10+**
- **LLM APIs:** `openai` (OpenAI GPT and DashScope Qwen-MT through the OpenAI-compatible endpoint),
  `google-genai` (Gemini, including the Batch API)
- **Browser automation:** `playwright` connected to Chrome via CDP
- **Documents:** `python-docx`, `ebooklib`, `beautifulsoup4`, `lxml`
- **Images:** `Pillow`, `numpy`
- **Config:** `python-dotenv`

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env   # add keys here, or let the scripts ask for them
```

### API keys and provider detection

You don't have to know which variable a key belongs to. Paste it and the program detects the provider:

```bash
python3 translate.py --add-key       # detect the key's provider and save it to .env
python3 translate.py --which-keys    # show which providers have keys
```

![Key detection](docs/images/demo_keys.png)

Detection works in two steps:

1. **Format.** `AIza…` means Google Gemini. `sk-proj-…`, `sk-svcacct-…` and similar prefixes mean
   OpenAI. `sk-` followed by 32 hex characters is the DashScope format.
2. **Verification.** A free `GET /models` request to the candidate providers confirms the key.
   This is what tells OpenAI and DashScope keys apart, since both start with `sk-`. If the network
   is unavailable, the format guess is used.

Any script that needs a missing key asks for it the same way instead of exiting. A key for the
wrong provider is rejected with a link to where the right one can be obtained. Extra Gemini keys
are saved as `GEMINI_API_KEY_2`, `_3`… and the Gemini translator switches between them when one hits its daily quota.

| Variable | Used by |
|---|---|
| `OPENAI_API_KEY` | GPT translator, glossary builder, fix scripts, `ocr_vision.py --provider openai` |
| `DASHSCOPE_API_KEY` | Qwen-MT translator |
| `GEMINI_API_KEY`, `GEMINI_API_KEY_2`, … | Gemini translator (rotates through all keys), OCR, `fix_english_remnants.py` |

`.env` is in `.gitignore` and must never be committed.

## Usage

```bash
# 1. Build a glossary
python3 glossary/glossary_builder.py book.epub

# 2. Translate: the engine is chosen by the available key (Gemini → OpenAI → Qwen)
python3 translate.py book.epub
python3 translate.py book.epub --ask-key            # paste a key, the matching model is used
python3 translate.py book.epub --engine openai      # force an engine
python3 translate.py book.epub --parallel 8         # extra flags go to the translator itself
python3 translate.py book.epub --resume             # continue after an interruption

# or call a translator directly
python3 translators/eng_translator_gemini.py book.epub --glossary book_glossary.json

# 3. Format and export
python3 export/heading.py book_translated.docx
python3 export/docx2epub.py book_translated.docx --cover cover.jpg --lang ru --title "Title" --author "Author"
```

Run any script with `-h` to see all options. Translators write `<name>_translated.docx`
to the current directory; `-o` sets another path.

## Not in the repository

Book sources, translations, glossaries of specific books, caches, screenshots, covers and logs are
excluded through `.gitignore`. They are copyrighted content or local working data.
