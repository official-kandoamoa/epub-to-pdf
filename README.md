# epub-to-pdf

Convert an EPUB book into a single, well-formed PDF — with working internal
links, a detected or chosen page size, and a rendering engine that works even
on constrained platforms like Termux/Android where a full CSS engine isn't
available.

## Why not just use Calibre / a browser's "print to PDF"?

Both work, but tend to fall short in one of two ways this script specifically
avoids:

-   **Merging chapters as separate PDFs** breaks any link between chapters
    (e.g. `<a href="ch02.xhtml#note1">`) — the link ends up pointing at a
    temporary file that no longer exists once the conversion is done. This
    script combines the whole book into **one HTML document before
    rendering**, so those links become real, working internal PDF links.
-   **A full CSS engine isn't always available.** On Termux, minimal Docker
    images, or other constrained environments, the native libraries
    WeasyPrint needs (Pango, Cairo, GObject) often aren't installable. This
    script falls back automatically to a pure-Python renderer so it still
    works there — with reduced CSS fidelity, but it works.

## Features

-   **Working cross-chapter links.** The whole book is assembled into one
    continuous document first, so internal references become real PDF
    links instead of dead ones.
-   **Two rendering engines, chosen automatically.** Prefers
    [WeasyPrint](https://weasyprint.org/) for best-quality output (accurate
    colors, fonts, and layout), and transparently falls back to
    [xhtml2pdf](https://github.com/xhtml2pdf/xhtml2pdf) — pure Python, no
    native dependencies — when WeasyPrint's native libraries aren't
    available.
-   **Automatic page-size detection.** Reads an `@page` size straight out of
    the EPUB's own CSS when possible, so the book's own tuning (font sizes,
    code-block widths, image dimensions) stays proportioned correctly,
    rather than being squeezed into a size it wasn't designed for.
-   **Twelve built-in page-size presets** covering common US, ISO A/B, and
    tech-book trim sizes, or specify an exact custom size yourself.
-   **Cover pages rendered correctly.** Handles the common
    `<svg viewBox="..."><image .../></svg>` cover-image pattern used by most
    real-world EPUBs, and gives a cover (or any other image-only page) its
    own dedicated page in the output rather than letting it run into
    whatever text follows.
-   **Continuous flow within a chapter, natural pagination between them.**
    Multiple spine files that make up one logical chapter are combined
    without a forced page break; pagination otherwise follows the page size
    and the book's own CSS, the same way a real reader would lay it out.
-   **Preserves title/author metadata** from the EPUB into the output PDF.
-   **Tells you what it couldn't do**, rather than failing silently: a
    missing or unparsable chapter is reported by name as a warning, with
    the rest of the book still rendered.

## Requirements

-   Python 3.9+
-   [`uv`](https://docs.astral.sh/uv/) (recommended), or `pip`
-   For best quality: WeasyPrint's native dependencies (Pango, Cairo,
    GObject) — see [Rendering engines](#rendering-engines) below if
    installing these isn't practical on your platform

## Installation

The script declares its own dependencies inline, so with
[`uv`](https://docs.astral.sh/uv/) installed there's no separate install
step — `uv run` fetches everything the first time it's needed:

```bash
$ uv run epub_to_pdf.py book.epub
```

Without `uv`, install the dependencies yourself and run it with `python3`:

```bash
$ pip install weasyprint xhtml2pdf pypdf lxml
$ python3 epub_to_pdf.py book.epub
```

Every example below uses `python3 epub_to_pdf.py ...`; substitute
`uv run epub_to_pdf.py ...` if that's how you installed it.

## Quick start

```bash
$ python3 epub_to_pdf.py book.epub
using weasyprint to render the book
combining 24 chapter(s) into one continuous document
detected page size from EPUB CSS: 7.375in 9.125in

created book.pdf (312 pages)
```

By default, the output is written next to the input with a `.pdf`
extension (`book.epub` → `book.pdf`). Use `-o` to choose a different path:

```bash
$ python3 epub_to_pdf.py book.epub -o ~/Books/book.pdf
```

## Command-line reference

```bash
python3 epub_to_pdf.py EPUB [options]
```

| Argument | Description |
|---|---|
| `epub` | *(required)* Path to the input `.epub` file. |
| `-o`, `--output PATH` | Output PDF path. Default: same name as the input, with a `.pdf` extension. |
| `--page-size NAME` | A built-in preset — see [Page sizes](#page-sizes) below. Ignored if `--page-width`/`--page-height` are given. If omitted entirely, the script tries to detect the size from the EPUB's own CSS first, falling back to `letter` only if nothing is found. |
| `--page-width WIDTH` | Custom page width with a unit, e.g. `7.375in` or `18.7cm`. Overrides `--page-size`; use together with `--page-height`. |
| `--page-height HEIGHT` | Custom page height with a unit. Use together with `--page-width`. |
| `--margin MARGIN` | Page margin when using `--page-width`/`--page-height`, e.g. `0.5in`. Default: `0.5in`. |
| `--engine {weasyprint,xhtml2pdf}` | Force a specific rendering engine instead of auto-detecting. See [Rendering engines](#rendering-engines). |
| `-h`, `--help` | Show the built-in help. |

## Page sizes

If you don't specify a size at all, the script looks for an `@page` rule in
the EPUB's own stylesheets first, and only falls back to `letter` if it
can't find one — so in most cases you don't need to think about this at all.

To choose explicitly, `--page-size` accepts:

| Preset | Dimensions | Notes |
|---|---|---|
| `letter` | 8.5in × 11in | US default |
| `legal` | 8.5in × 14in | |
| `executive` | 7.25in × 10.5in | |
| `ledger` | 11in × 17in | |
| `a3` | 297mm × 420mm | |
| `a4` | 210mm × 297mm | |
| `a5` | 148mm × 210mm | |
| `a6` | 105mm × 148mm | |
| `b4` | 250mm × 353mm | |
| `b5` | 176mm × 250mm | |
| `b6` | 125mm × 176mm | |

For anything else, give an exact size directly:

```bash
$ python3 epub_to_pdf.py book.epub --page-width 6in --page-height 9in --margin 0.6in
```

## Rendering engines

Two engines are supported, and the script picks automatically unless you
force one with `--engine`:

-   **WeasyPrint** — a real CSS engine, and the preferred choice: accurate
    colors, fonts, and layout, including things a simpler renderer can't do
    (proper flexbox/grid, more complete font handling). Needs native
    libraries (Pango, Cairo, GObject) that aren't always installable —
    notably often unreliable on Termux/Android. See WeasyPrint's own
    [installation docs](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html)
    if it isn't picked up automatically.
-   **xhtml2pdf** — pure Python, no native dependencies, so it works in
    places WeasyPrint can't (Termux, minimal containers, restricted
    environments generally). Simpler CSS support (no flexbox/grid), so
    layout fidelity is lower.

The script tries WeasyPrint first and falls back to xhtml2pdf automatically
if WeasyPrint's native libraries aren't importable — you'll see which one
was actually used in the first line of output:

```bash
using weasyprint to render the book
```

or

```bash
using xhtml2pdf to render the book
```

Force a specific engine with `--engine weasyprint` or `--engine xhtml2pdf`.
If neither is usable at all, the script exits with a clear explanation of
what's missing and how to fix it, rather than a bare traceback.

## How it works, briefly

1.  Extracts the EPUB and reads its package document (`.opf`) to get the
    book's title, author, and spine (reading order).
2.  Combines every spine chapter into a single HTML document, rewriting
    every internal link, image reference, and stylesheet `url()`/`@import`
    along the way so they resolve correctly once everything lives in one
    document instead of many separate files.
3.  Inlines all CSS directly into the document (rather than referencing it
    via `file://` links), so both rendering engines reliably pick it up.
4.  Detects (or accepts) a page size and renders the whole thing in one
    pass with the chosen engine.
5.  Sets the output PDF's title/author metadata from the EPUB, and reports
    the final page count.

## Troubleshooting

**`error: no PDF rendering engine is available.`**
Neither engine could be imported. The error message lists what's missing
and how to fix each one — usually either installing WeasyPrint's native
dependencies, or `pip install xhtml2pdf` for the dependency-free fallback.

**`WARNING: text/chXX.xhtml: missing from EPUB (was it a failed download?)`**
A chapter the package document lists couldn't be found or parsed. The rest
of the book is still rendered; this chapter is simply skipped.

**`error: every chapter is missing or failed to parse - no PDF produced.`**
None of the spine chapters could be read at all — check that the file is
actually a valid EPUB and isn't corrupted or empty.

**Code blocks or images look cramped/oversized.**
Usually means the page size doesn't match what the book's CSS was actually
designed for. Try `--page-size b4` for a typical tech-book layout, or
omit `--page-size` entirely and let the script try to detect it from the
book's own CSS first.

## Limitations

-   xhtml2pdf's simpler CSS support means some layouts (especially anything
    relying on flexbox/grid, or unusual CSS selectors) will look plainer,
    or in rare cases fail to apply, compared to WeasyPrint.
-   Only EPUB (`.epub`) input is supported — not other ebook formats.
-   Very large books can take a while to render in one pass, since the
    whole book is combined into a single document before any page is
    produced.

## Similar projects

-   [Calibre](https://calibre-ebook.com/) — general-purpose ebook manager
    and converter; a heavier dependency but handles many more formats.
-   [`ebook-convert`](https://manual.calibre-ebook.com/generated/en/ebook-convert.html) —
    Calibre's own command-line converter.
-   [pandoc](https://pandoc.org/) — general document converter; can go
    EPUB → PDF via LaTeX, with different tradeoffs around styling fidelity.
