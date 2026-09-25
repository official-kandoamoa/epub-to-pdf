# /// script
# dependencies = [
#   "weasyprint",
#   "xhtml2pdf",
#   "pypdf",
#   "lxml",
# ]
# ///

"""Convert an EPUB (e.g. one produced by oreilly_downloader.py) into a PDF.

All spine chapters are concatenated into a single HTML document and rendered
in one pass. This makes same-book links work: a link like
<a href="ch02.xhtml#note1"> is rewritten to an in-document anchor and becomes
a real internal PDF link. Rendering chapters as separate PDFs and merging
them afterward leaves such links pointing at a temporary extraction directory
that no longer exists once the script finishes.

Multiple spine items that belong to the same logical chapter are combined
into continuous flowing content (no forced page-break between them). Natural
pagination still occurs according to the page size and the book's own CSS.

Two rendering engines are supported:
  - weasyprint: a real CSS engine, best fidelity (colors, fonts, layout),
    but needs native libraries (pango, etc) that are not always available
    on constrained platforms like Termux/Android.
  - xhtml2pdf: pure Python, no native dependencies, works everywhere,
    but has weaker CSS support (no flexbox/grid, simpler layout).

The script tries weasyprint first and falls back to xhtml2pdf automatically
if weasyprint's native libraries are not importable. Use --engine to force
one explicitly.

When no page size is supplied on the command line the script attempts to
detect an @page size rule from the EPUB's own stylesheets. If none is found
it falls back to letter.

CSS is fully inlined (rather than referenced via file:// links) so that both
engines reliably load styles, fonts and other resources that exist inside
the EPUB and are correctly declared according to HTML rules.

Usage:
    python epub_to_pdf.py book.epub
    python epub_to_pdf.py book.epub --engine xhtml2pdf
"""

import argparse
import io
import posixpath
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from lxml import etree
from lxml import html as lhtml
from pypdf import PdfReader, PdfWriter

OPF_NS = {'opf': 'http://www.idpf.org/2007/opf'}
CONTAINER_NS = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
DC_NS = {'dc': 'http://purl.org/dc/elements/1.1/'}

PAGE_CSS = {
    # US/office sizes
    'letter': '@page { size: 8.5in 11in; margin: 1in 0.9in; }',
    'legal': '@page { size: 8.5in 14in; margin: 1in 0.9in; }',
    'executive': '@page { size: 7.25in 10.5in; margin: 0.8in; }',
    'ledger': '@page { size: 11in 17in; margin: 1in; }',
    # ISO 216 A-series
    'a3': '@page { size: 297mm 420mm; margin: 2.5cm; }',
    'a4': '@page { size: 210mm 297mm; margin: 2.2cm 1.8cm; }',
    'a5': '@page { size: 148mm 210mm; margin: 1.5cm; }',
    'a6': '@page { size: 105mm 148mm; margin: 1cm; }',
    # ISO 216 B-series
    'b4': '@page { size: 250mm 353mm; margin: 2cm; }',
    'b5': '@page { size: 176mm 250mm; margin: 1.8cm; }',
    'b6': '@page { size: 125mm 176mm; margin: 1cm; }',
}

LENGTH_RE = re.compile(r'^\d+(\.\d+)?(in|cm|mm|pt|px)$', re.IGNORECASE)
XML_DECL_RE = re.compile(r'^\s*<\?xml[^>]*\?>\s*')
# Match @page { ... size: <value> ... }
PAGE_SIZE_RE = re.compile(
    r'@page\s*(?:[^{]*)\{\s*[^}]*?\bsize\s*:\s*([^;}] +)',
    re.IGNORECASE | re.DOTALL,
)
# Match url(...) inside CSS so relative resource paths can be absolutised.
CSS_URL_RE = re.compile(
    r'''url\(\s*(['"]?)([^'")]+)\1\s*\)''',
    re.IGNORECASE,
)
# Match @import rules so they can be resolved and inlined.
CSS_IMPORT_RE = re.compile(
    r'''@import\s+(?:url\(\s*(['"]?)([^'")]+)\1\s*\)|(['"])([^'"]+)\3)\s*[^;]*;''',
    re.IGNORECASE,
)
# Match @media screen { ... } blocks so their rules can be applied in PDF context.
# Many EPUBs put the primary colour / typography rules under media="screen".
MEDIA_SCREEN_RE = re.compile(
    r'@media\s+(?:only\s+)?screen\b[^{]*\{',
    re.IGNORECASE,
)

# lxml's HTML parser (used so malformed EPUB (X)HTML is tolerated) lowercases
# every attribute name, including inside embedded SVG. SVG attribute names
# are case-sensitive, so "viewBox" surviving as "viewbox" is silently
# ignored by real SVG renderers. This matters a lot for cover pages: most
# EPUB tooling (Calibre included) wraps the cover image in an <svg viewBox=
# "..."> so it scales to the page; losing viewBox/preserveAspectRatio can
# make that cover render at the wrong size, cropped to nothing, or blank.
SVG_ATTR_CASE_MAP = {
    'viewbox': 'viewBox',
    'preserveaspectratio': 'preserveAspectRatio',
    'patternunits': 'patternUnits',
    'patterncontentunits': 'patternContentUnits',
    'patterntransform': 'patternTransform',
    'gradientunits': 'gradientUnits',
    'gradienttransform': 'gradientTransform',
    'spreadmethod': 'spreadMethod',
    'clippathunits': 'clipPathUnits',
    'markerwidth': 'markerWidth',
    'markerheight': 'markerHeight',
    'markerunits': 'markerUnits',
    'refx': 'refX',
    'refy': 'refY',
    'textlength': 'textLength',
    'lengthadjust': 'lengthAdjust',
    'baseprofile': 'baseProfile',
    'diffuseconstant': 'diffuseConstant',
    'specularconstant': 'specularConstant',
    'specularexponent': 'specularExponent',
    'surfacescale': 'surfaceScale',
    'kernelmatrix': 'kernelMatrix',
    'kernelunitlength': 'kernelUnitLength',
    'targetx': 'targetX',
    'targety': 'targetY',
    'edgemode': 'edgeMode',
    'xchannelselector': 'xChannelSelector',
    'ychannelselector': 'yChannelSelector',
    'primitiveunits': 'primitiveUnits',
    'filterunits': 'filterUnits',
    'stddeviation': 'stdDeviation',
    'tablevalues': 'tableValues',
    'requiredextensions': 'requiredExtensions',
    'requiredfeatures': 'requiredFeatures',
    'systemlanguage': 'systemLanguage',
    'externalresourcesrequired': 'externalResourcesRequired',
    'zoomandpan': 'zoomAndPan',
    'attributename': 'attributeName',
    'attributetype': 'attributeType',
    'repeatcount': 'repeatCount',
    'repeatdur': 'repeatDur',
    'calcmode': 'calcMode',
    'keytimes': 'keyTimes',
    'keysplines': 'keySplines',
    'keypoints': 'keyPoints',
}


def fix_svg_attribute_case(svg_root):
    """Restore the correct camelCase spelling of SVG attributes."""
    for el in svg_root.iter():
        attrib = el.attrib
        for key in list(attrib.keys()):
            fixed = SVG_ATTR_CASE_MAP.get(key)
            if fixed and fixed not in attrib:
                attrib[fixed] = attrib.pop(key)
        if el.tag == 'svg' and 'width' not in attrib and 'height' not in attrib:
            view_box = attrib.get('viewBox')
            if view_box:
                parts = view_box.replace(',', ' ').split()
                if len(parts) == 4:
                    try:
                        w, h = float(parts[2]), float(parts[3])
                        if w > 0 and h > 0:
                            attrib['width'] = _format_svg_length(w)
                            attrib['height'] = _format_svg_length(h)
                    except ValueError:
                        pass


def _format_svg_length(value):
    """Render a numeric SVG length without a trailing '.0' for whole numbers."""
    return str(int(value)) if value == int(value) else str(value)


def simplify_svg_cover_images(body):
    """Collapse common <svg><image/></svg> cover wrappers into plain <img> tags."""
    for svg_el in list(body.iter('svg')):
        children = list(svg_el)
        images = [c for c in children if c.tag == 'image']
        others = [c for c in children if c.tag not in ('image', 'title', 'desc', 'metadata')]
        if len(images) != 1 or others:
            continue
        image_el = images[0]
        href = image_el.get('xlink:href') or image_el.get('href')
        if not href:
            continue
        img = etree.Element('img')
        img.set('src', href)
        alt = image_el.get('alt') or svg_el.get('aria-label') or ''
        img.set('alt', alt)
        width = image_el.get('width') or svg_el.get('width')
        height = image_el.get('height') or svg_el.get('height')
        if width:
            img.set('width', width)
        if height:
            img.set('height', height)
        img.set('style', 'max-width: 100%; max-height: 100%; height: auto;')
        parent = svg_el.getparent()
        if parent is not None:
            parent.replace(svg_el, img)


def build_page_css(page_size, width=None, height=None, margin=None):
    if width or height:
        if not (width and height):
            sys.exit('error: --page-width and --page-height must be given together')
        for label, value in (('--page-width', width), ('--page-height', height)):
            if not LENGTH_RE.match(value):
                sys.exit(f'error: {label} "{value}" needs a unit, e.g. "7.375in" or "18.7cm"')
        margin = margin or '0.5in'
        if not LENGTH_RE.match(margin):
            sys.exit(f'error: --margin "{margin}" needs a unit, e.g. "0.5in" or "1.2cm"')
        return f'@page {{ size: {width} {height}; margin: {margin}; }}'
    if page_size is None:
        return None
    return PAGE_CSS[page_size]


_STYLE_CLOSE_RE = re.compile(r'</style\s*>', re.IGNORECASE)


def _neutralize_style_terminator(css_text):
    return _STYLE_CLOSE_RE.sub(r'<\\/style>', css_text)


def sniff_text(raw):
    for bom, enc in ((b'\xef\xbb\xbf', 'utf-8-sig'),
                      (b'\xff\xfe', 'utf-16-le'),
                      (b'\xfe\xff', 'utf-16-be')):
        if raw.startswith(bom):
            return raw.decode(enc, errors='replace')

    head = raw[:1024].decode('ascii', errors='ignore')
    m = (re.search(r'encoding=["\']([\w-]+)["\']', head, re.IGNORECASE) or
         re.search(r'charset=["\']?([\w-]+)', head, re.IGNORECASE))
    declared = m.group(1) if m else None

    for enc in filter(None, [declared, 'utf-8']):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('latin-1')


def detect_engine(preferred=None):
    errors = []
    if preferred in (None, 'weasyprint'):
        try:
            from weasyprint import CSS, HTML  # noqa: F401
            return 'weasyprint', errors
        except (ImportError, OSError) as exc:
            errors.append(f'weasyprint unavailable: {exc}')
            if preferred == 'weasyprint':
                return None, errors

    try:
        from xhtml2pdf import pisa  # noqa: F401
        return 'xhtml2pdf', errors
    except ImportError as exc:
        errors.append(f'xhtml2pdf unavailable: {exc}')

    return None, errors


def find_opf_path(extracted_dir):
    container = extracted_dir / 'META-INF' / 'container.xml'
    tree = etree.parse(str(container))
    rootfile = tree.find('.//c:rootfile', CONTAINER_NS)
    if rootfile is None:
        raise ValueError('container.xml has no <rootfile> - not a valid EPUB')
    return extracted_dir / unquote(rootfile.get('full-path'))


def parse_opf(opf_path):
    tree = etree.parse(str(opf_path))
    root = tree.getroot()
    opf_dir = opf_path.parent

    def meta(tag):
        el = root.find(f'.//dc:{tag}', DC_NS)
        return el.text.strip() if el is not None and el.text else None

    title = meta('title') or opf_path.stem
    author = meta('creator')

    manifest = {
        item.get('id'): unquote(item.get('href'))
        for item in root.findall('.//opf:manifest/opf:item', OPF_NS)
    }

    spine_paths = []
    for itemref in root.findall('.//opf:spine/opf:itemref', OPF_NS):
        href = manifest.get(itemref.get('idref'))
        if href:
            spine_paths.append((opf_dir / href).resolve())

    return title, author, spine_paths


def rewrite_css_urls(css_text, css_dir, tmp):
    def replacer(match):
        quote, url = match.group(1), match.group(2).strip()
        if url.startswith(('data:', 'http://', 'https://', 'file://', '/')):
            return match.group(0)
        resolved = posixpath.normpath(
            posixpath.join(css_dir, unquote(url)) if css_dir else unquote(url))
        file_path = tmp / resolved
        if file_path.exists():
            return f'url({quote}{file_path.as_uri()}{quote})'
        return match.group(0)

    return CSS_URL_RE.sub(replacer, css_text)


def unwrap_media_screen(css_text):
    result = []
    pos = 0
    for m in MEDIA_SCREEN_RE.finditer(css_text):
        result.append(css_text[pos:m.start()])
        depth = 1
        i = m.end()
        while i < len(css_text) and depth:
            if css_text[i] == '{':
                depth += 1
            elif css_text[i] == '}':
                depth -= 1
            i += 1
        result.append(css_text[m.end():i - 1])
        pos = i
    result.append(css_text[pos:])
    return ''.join(result)


def resolve_and_inline_imports(css_text, css_dir, tmp, seen_imports, problems, origin):
    def replacer(match):
        url = (match.group(2) or match.group(4) or '').strip()
        if not url or url.startswith(('data:', 'http://', 'https://')):
            return match.group(0)
        url = unquote(url)
        resolved = posixpath.normpath(
            posixpath.join(css_dir, url) if css_dir else url)
        file_path = tmp / resolved
        abs_key = str(file_path.resolve()) if file_path.exists() else None
        if not abs_key or abs_key in seen_imports:
            return ''
        if not file_path.exists():
            problems.append(f'{origin}: @import not found: {url}')
            return ''
        seen_imports.add(abs_key)
        try:
            imported = sniff_text(file_path.read_bytes())
        except Exception as exc:
            problems.append(f'{origin}: failed to read @import {url}: {exc}')
            return ''
        imported_dir = posixpath.dirname(resolved)
        imported = process_css(imported, imported_dir, tmp, seen_imports, problems, origin)
        return imported + '\n'

    return CSS_IMPORT_RE.sub(replacer, css_text)


def process_css(css_text, css_dir, tmp, seen_imports, problems, origin):
    css_text = resolve_and_inline_imports(css_text, css_dir, tmp, seen_imports, problems, origin)
    css_text = unwrap_media_screen(css_text)
    css_text = rewrite_css_urls(css_text, css_dir, tmp)
    return css_text


def extract_page_size_from_css(css_texts):
    for css in css_texts:
        m = PAGE_SIZE_RE.search(css)
        if m:
            size_val = m.group(1).strip()
            size_val = re.sub(r'\s*!important\s*$', '', size_val, flags=re.I)
            if re.search(r'\d', size_val):
                return size_val
            keyword = size_val.lower()
            if keyword in PAGE_CSS:
                preset = PAGE_CSS[keyword]
                sm = re.search(r'size\s*:\s*([^;]+)', preset, re.I)
                if sm:
                    return sm.group(1).strip()
            return size_val
    return None


def build_combined_document(tmp, spine_paths):
    spine_rel = [p.relative_to(tmp).as_posix() for p in spine_paths]
    path_to_index = {rel: i for i, rel in enumerate(spine_rel)}

    combined = etree.Element('html')
    head = etree.SubElement(combined, 'head')
    etree.SubElement(head, 'meta', charset='utf-8')
    combined_body = etree.SubElement(combined, 'body')

    content_wrapper = etree.SubElement(combined_body, 'div')
    content_wrapper_id = None

    default_style = etree.SubElement(head, 'style')
    default_style.set('type', 'text/css')
    default_style.text = (
        'html, body { color: #000000; background-color: #ffffff; }\n'
        '/* default fallback – overridden by book CSS when present */'
    )

    seen_stylesheets = set()
    seen_imports = set()
    collected_css = []
    problems = []
    first_included = True
    html_classes = set()
    body_classes = set()
    body_style_parts = []

    for i, (path, rel) in enumerate(zip(spine_paths, spine_rel)):
        if not path.exists():
            problems.append(f'{rel}: missing from EPUB (was it a failed download?)')
            continue

        prefix = f'c{i}_'
        chapter_dir = posixpath.dirname(rel)

        def resolve(href):
            href = unquote(href)
            target = posixpath.normpath(
                posixpath.join(chapter_dir, href) if chapter_dir else href)
            return target

        try:
            text = XML_DECL_RE.sub('', sniff_text(path.read_bytes()))
            doc = lhtml.document_fromstring(text)
        except Exception as exc:
            problems.append(f'{rel}: failed to parse ({exc})')
            continue

        orig_html = doc.find('.//html') if doc.tag != 'html' else doc
        if orig_html is not None:
            cls = orig_html.get('class')
            if cls:
                html_classes.update(cls.split())
        orig_body = doc.find('.//body')
        if orig_body is not None:
            cls = orig_body.get('class')
            if cls:
                body_classes.update(cls.split())
            st = orig_body.get('style')
            if st:
                body_style_parts.append(st)

        for svg_el in doc.iter('svg'):
            fix_svg_attribute_case(svg_el)

        if content_wrapper_id is None and orig_body is not None:
            candidates = []
            for el in orig_body.iter():
                eid = el.get('id')
                if not eid:
                    continue
                eid_lower = eid.lower()
                if 'content' in eid_lower or eid_lower in (
                        'sbo-rt-content', 'book-content', 'main-content',
                        'chapter-content', 'rt-content'):
                    candidates.insert(0, eid)
                else:
                    candidates.append(eid)
            if candidates:
                content_wrapper_id = candidates[0]
            else:
                content_wrapper_id = 'sbo-rt-content'
            content_wrapper.set('id', content_wrapper_id)

        for link_el in doc.findall('.//link'):
            if (link_el.get('rel') or '').lower() != 'stylesheet':
                continue
            href = link_el.get('href')
            if not href:
                continue
            file_path = tmp / resolve(href)
            if not file_path.exists():
                problems.append(f'{rel}: stylesheet not found: {href}')
                continue
            abs_key = str(file_path.resolve())
            if abs_key in seen_stylesheets:
                continue
            seen_stylesheets.add(abs_key)
            try:
                css_text = sniff_text(file_path.read_bytes())
            except Exception as exc:
                problems.append(f'{rel}: failed to read stylesheet {href}: {exc}')
                continue
            css_dir = posixpath.dirname(resolve(href))
            css_text = process_css(css_text, css_dir, tmp, seen_imports, problems, rel)
            collected_css.append(css_text)
            style_el = etree.SubElement(head, 'style')
            style_el.set('type', 'text/css')
            style_el.text = _neutralize_style_terminator(css_text)

        for style_el in doc.findall('.//style'):
            css_text = style_el.text or ''
            if not css_text.strip():
                continue
            css_text = process_css(css_text, chapter_dir, tmp, seen_imports, problems, rel)
            collected_css.append(css_text)
            new_style = etree.SubElement(head, 'style')
            new_style.set('type', 'text/css')
            new_style.text = _neutralize_style_terminator(css_text)

        body = doc.find('.//body')
        if body is None:
            body = doc

        simplify_svg_cover_images(body)

        ided_elements = [el for el in body.iter() if el.get('id')]
        for el in ided_elements:
            old_id = el.get('id')
            new_id = prefix + old_id
            el.set('id', new_id)
            if el.tag == 'a' and el.get('name') == old_id:
                el.set('name', new_id)
            else:
                prev = el.getprevious()
                if prev is not None and prev.tag == 'a' and prev.get('name') == old_id:
                    prev.set('name', new_id)
                else:
                    anchor = etree.Element('a')
                    anchor.set('name', new_id)
                    el.addprevious(anchor)

        for el in body.iter('a'):
            href = el.get('href')
            if not href:
                continue
            if href.startswith(('http://', 'https://', 'mailto:')):
                continue
            if href.startswith('#'):
                el.set('href', '#' + prefix + href[1:])
                continue
            target, _, frag = href.partition('#')
            resolved = resolve(target) if target else rel
            if resolved in path_to_index:
                el.set('href', '#' + f'c{path_to_index[resolved]}_' + (frag or 'top'))
            else:
                file_path = tmp / resolved
                if file_path.exists():
                    el.set('href', file_path.as_uri() + (f'#{frag}' if frag else ''))

        for el in body.iter('img', 'source', 'image'):
            if el.tag == 'image':
                attr = 'xlink:href' if el.get('xlink:href') else 'href'
            else:
                attr = 'src'
            src = el.get(attr)
            if not src or src.startswith(('http://', 'https://', 'data:')):
                continue
            file_path = tmp / resolve(src)
            if file_path.exists():
                el.set(attr, file_path.as_uri())

        for el in body.iter():
            style_attr = el.get('style')
            if style_attr and 'url(' in style_attr.lower():
                el.set('style', rewrite_css_urls(style_attr, chapter_dir, tmp))

        section = etree.SubElement(content_wrapper, 'div')
        section.set('id', prefix + 'top')
        if body_classes:
            section.set('class', ' '.join(sorted(body_classes)))
        top_anchor = etree.SubElement(section, 'a')
        top_anchor.set('name', prefix + 'top')

        page_text = ''.join(section.itertext()).strip()
        is_image_only_page = not page_text and section.find('.//img') is not None
        if is_image_only_page:
            style_parts = ['page-break-after: always']
            if i > 0:
                style_parts.append('page-break-before: always')
            existing_style = section.get('style')
            new_style = '; '.join(style_parts)
            section.set('style', f'{existing_style}; {new_style}' if existing_style else new_style)

        content_root = body
        if content_wrapper_id:
            existing = body.find(f'.//*[@id="{content_wrapper_id}"]')
            if existing is not None:
                content_root = existing
        for child in list(content_root):
            section.append(child)
        first_included = False

    if first_included:
        return None, problems, collected_css

    if content_wrapper.get('id') is None:
        content_wrapper.set('id', content_wrapper_id or 'sbo-rt-content')

    if html_classes:
        combined.set('class', ' '.join(sorted(html_classes)))
    if body_classes:
        combined_body.set('class', ' '.join(sorted(body_classes)))
    if body_style_parts:
        combined_body.set('style', '; '.join(body_style_parts))

    combined_html = etree.tostring(combined, encoding='unicode', method='html')
    return combined_html, problems, collected_css


def render_combined_weasyprint(combined_html, page_css_string, base_dir=None):
    from weasyprint import CSS, HTML
    stylesheets = []
    if page_css_string:
        stylesheets.append(CSS(string=page_css_string))
    return HTML(string=combined_html).write_pdf(stylesheets=stylesheets)


def _discover_system_fonts():
    candidates = [
        ('DejaVuSans', Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')),
        ('DejaVuSansMono', Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf')),
        ('FreeSans', Path('/usr/share/fonts/truetype/freefont/FreeSans.ttf')),
        ('NotoSans', Path('/usr/share/fonts/SlidesCarnival/google/Noto Sans/static/NotoSans-Regular.ttf')),
    ]
    found = []
    seen_families = set()
    font_dirs = set()
    for family, fpath in candidates:
        if not fpath.is_file():
            continue
        if family in seen_families:
            continue
        seen_families.add(family)
        found.append((family, fpath))
        font_dirs.add(fpath.parent.resolve())
    return found, font_dirs


def _xhtml2pdf_font_css(font_faces):
    rules = []
    families = []
    for family, fpath in font_faces:
        uri = fpath.resolve().as_uri()
        rules.append(
            f'@font-face {{ font-family: "{family}"; src: url("{uri}"); }}'
        )
        families.append(f'"{family}"')
    families.extend([
        '"STSong-Light"',
        '"MSung-Light"',
        '"HeiseiMin-W3"',
        '"HeiseiKakuGo-W5"',
        '"HYSMyeongJo-Medium"',
        '"HYGothic-Medium"',
        'sans-serif',
    ])
    stack = ', '.join(families)
    rules.append(
        f'html, body, #sbo-rt-content {{ font-family: {stack}; }}'
    )
    rules.append(
        'pre, code, tt, kbd, samp { font-family: '
        '"DejaVuSansMono", "Courier", monospace; }'
    )
    return '\n'.join(rules)


_UNSUPPORTED_XHTML2PDF_SELECTOR_RULE_RE = re.compile(
    r'[^{}]*?\[[^\]]*(?:\^=|\$=|\*=)[^\]]*\][^{}]*\{[^{}]*\}'
)


def _strip_xhtml2pdf_unsupported_selectors(css_text):
    return _UNSUPPORTED_XHTML2PDF_SELECTOR_RULE_RE.sub('', css_text)


def _sanitize_css_for_xhtml2pdf(combined_html):
    def clean_style_block(m):
        return m.group(1) + _strip_xhtml2pdf_unsupported_selectors(m.group(2)) + m.group(3)

    return re.sub(
        r'(<style[^>]*>)(.*?)(</style>)',
        clean_style_block,
        combined_html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def render_combined_xhtml2pdf(combined_html, page_css_string, base_dir=None):
    from xhtml2pdf import pisa

    combined_html = _sanitize_css_for_xhtml2pdf(combined_html)

    font_faces, font_dirs = _discover_system_fonts()
    font_css = _xhtml2pdf_font_css(font_faces)

    injected = []
    if page_css_string:
        injected.append(page_css_string)
    if font_css:
        injected.append(font_css)
    if injected:
        style_tag = '<style type="text/css">' + '\n'.join(injected) + '</style>'
        if '<head>' in combined_html:
            combined_html = combined_html.replace('<head>', f'<head>{style_tag}', 1)
        else:
            combined_html = style_tag + combined_html

    def link_callback(uri, rel):
        if uri.startswith('file://'):
            path = unquote(urlparse(uri).path)
            from pathlib import Path as _P
            try:
                p = _P(path)
                if p.exists():
                    return str(p)
            except Exception:
                pass
            return path
        return uri

    kwargs = {}
    try:
        from xhtml2pdf.config.resources import ResourceAccessPolicy
        extra = tuple(sorted(font_dirs)) if font_dirs else ()
        if base_dir is not None:
            kwargs['resource_policy'] = ResourceAccessPolicy(
                base_dir=base_dir,
                extra_roots=extra,
            )
        elif extra:
            kwargs['resource_policy'] = ResourceAccessPolicy(
                extra_roots=extra,
            )
    except ImportError:
        pass

    buf = io.BytesIO()
    result = pisa.CreatePDF(combined_html, dest=buf, link_callback=link_callback, **kwargs)
    if result.err:
        raise RuntimeError(f'xhtml2pdf reported {result.err} error(s)')
    return buf.getvalue()


RENDERERS = {
    'weasyprint': render_combined_weasyprint,
    'xhtml2pdf': render_combined_xhtml2pdf,
}


def convert(epub_path, pdf_path, page_css_string, engine=None):
    engine, errors = detect_engine(engine)
    if engine is None:
        sys.exit(
            'error: no PDF rendering engine is available.\n' +
            '\n'.join(f'  - {e}' for e in errors) +
            '\n\nFix one of these:\n'
            '  weasyprint (best quality): needs native pango/cairo - on '
            'Termux this is often unreliable, see '
            'https://doc.courtbouillon.org/weasyprint/stable/first_steps.html\n'
            '  xhtml2pdf (pure Python, works everywhere): '
            'pip install xhtml2pdf'
        )
    print(f'using {engine} to render the book')
    render = RENDERERS[engine]

    epub_path = Path(epub_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(epub_path) as zf:
            zf.extractall(tmp)

        opf_path = find_opf_path(tmp)
        title, author, spine_paths = parse_opf(opf_path)

        if not spine_paths:
            raise ValueError('EPUB spine is empty - nothing to render')

        print(f'combining {len(spine_paths)} chapter(s) into one continuous document')
        combined_html, problems, collected_css = build_combined_document(tmp, spine_paths)
        for p in problems:
            print(f'  WARNING: {p}')

        if combined_html is None:
            raise ValueError(
                'every chapter is missing or failed to parse - no PDF produced. '
                'Problems encountered:\n  ' + '\n  '.join(problems or ['(none recorded)'])
            )

        if page_css_string is None:
            detected = extract_page_size_from_css(collected_css)
            if detected:
                print(f'detected page size from EPUB CSS: {detected}')
                page_css_string = f'@page {{ size: {detected}; margin: 0.5in; }}'
            else:
                print('no @page size found in EPUB CSS; defaulting to letter')
                page_css_string = PAGE_CSS['letter']

        pdf_bytes = render(combined_html, page_css_string, base_dir=tmp)

        writer = PdfWriter()
        writer.append(io.BytesIO(pdf_bytes))
        writer.add_metadata({'/Title': title, '/Author': author or ''})
        with open(pdf_path, 'wb') as f:
            writer.write(f)

        page_count = len(writer.pages)

    print(f'\ncreated {pdf_path} ({page_count} pages)')
    if problems:
        print(f'\nWARNING: {len(problems)} chapter(s) had issues (see above)')


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('epub', help='path to the input .epub file')
    parser.add_argument('-o', '--output', help='output .pdf path '
                         '(default: same name as input, .pdf extension)')
    parser.add_argument('--page-size', choices=sorted(PAGE_CSS), default=None,
                         help='preset page size: letter, legal, executive, ledger, '
                              'a3/a4/a5/a6, b4/b5/b6 for the common '
                              '7-3/8" x 9-1/8" tech-book trim (ignored if '
                              '--page-width/--page-height are given). '
                              'When omitted the script attempts to read the '
                              'size from the EPUB\'s own CSS.')
    parser.add_argument('--page-width', help='custom page width with a unit, '
                         'e.g. "7.375in" or "18.7cm" - overrides --page-size, '
                         'use together with --page-height')
    parser.add_argument('--page-height', help='custom page height with a unit, '
                         'e.g. "9.125in" or "23.2cm" - use with --page-width')
    parser.add_argument('--margin', help='page margin when using '
                         '--page-width/--page-height, e.g. "0.5in" (default: 0.5in)')
    parser.add_argument('--engine', choices=sorted(RENDERERS),
                         help='force a specific rendering engine instead of '
                              'auto-detecting (weasyprint preferred, '
                              'xhtml2pdf as fallback)')
    args = parser.parse_args()

    epub_path = Path(args.epub)
    if not epub_path.exists():
        sys.exit(f'error: {epub_path} not found')

    page_css_string = build_page_css(args.page_size, args.page_width,
                                      args.page_height, args.margin)
    pdf_path = args.output or epub_path.with_suffix('.pdf')
    convert(epub_path, pdf_path, page_css_string, engine=args.engine)


if __name__ == '__main__':
    main()
