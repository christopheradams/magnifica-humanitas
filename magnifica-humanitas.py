#!/usr/bin/env python3
"""
Convert the Vatican HTML source of Magnifica Humanitas to epub.
Usage: python3 html_to_epub.py <input.html> [cover.png] <output.epub>
Requires: beautifulsoup4, ebooklib, lxml
"""

import copy
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import epub

# ── configuration ─────────────────────────────────────────────────────────────

KEEP_ATTRS = {'href', 'name', 'id', 'style'}

CSS = """
body { serif; font-size:1em; line-height:1.3; margin:1em 2em; color:#111; }
h1 { font-size:1.6em; text-align:center; margin:2em 0 .5em; font-variant:small-caps; }
h2 { font-size:1.25em; text-align:center; margin:1.5em 0 .4em; }
h3 { font-size:1.05em; text-align:center; font-style:italic; margin:.3em 0 1em; }
p  { margin:.55em 0; text-align:justify; }
a  { color:inherit; text-decoration:none; }
sup { font-size:.72em; }
/* Contents page */
.toc-chapter    { margin:1em 0 .2em; font-weight:bold; }
.toc-section    { margin:.15em 0 .15em 1.5em; }
.toc-subsection { margin:.1em 0 .1em 3em; font-style:italic; }
"""

CSS_COVER = """
body { margin:0; padding:0; }
img.cover { display:block; width:100%; height:100vh; object-fit:contain; }
"""

# ── helpers ───────────────────────────────────────────────────────────────────

def nodes_to_html(nodes, skip_indices=None, ftnref_rewrite=None,
                  ftn_backlink_rewrite=None):
    """
    Serialise a list of bs4 nodes, stripping non-essential attributes.

    ftnref_rewrite: dict {old_href -> new_href} for in-body footnote refs
                    e.g. '#_ftn1' -> 'footnotes.xhtml#_ftn1'
    ftn_backlink_rewrite: dict {old_href -> new_href} for footnote back-links
                    e.g. '#_ftnref1' -> 'intro.xhtml#_ftnref1'
    """
    skip_indices = skip_indices or set()
    parts = []
    for i, n in enumerate(nodes):
        if i in skip_indices:
            continue
        if isinstance(n, NavigableString):
            parts.append(str(n))
        elif isinstance(n, Tag):
            c = copy.deepcopy(n)
            # Rewrite footnote hrefs before stripping attributes
            if ftnref_rewrite or ftn_backlink_rewrite:
                for a in c.find_all('a', href=True):
                    href = a['href']
                    if ftnref_rewrite and href in ftnref_rewrite:
                        a['href'] = ftnref_rewrite[href]
                    elif ftn_backlink_rewrite and href in ftn_backlink_rewrite:
                        a['href'] = ftn_backlink_rewrite[href]
            for tag in [c] + c.find_all(True):
                for attr in list(tag.attrs):
                    if attr not in KEEP_ATTRS:
                        del tag[attr]
            parts.append(str(c))
    return '\n'.join(parts)


def is_centred_bold(tag):
    if not isinstance(tag, Tag) or tag.name != 'p':
        return False
    if 'text-align: center' not in tag.get('style', ''):
        return False
    return bool(tag.find('b'))


def is_chapter_heading(tag):
    if not is_centred_bold(tag):
        return False
    return bool(re.match(r'^(CHAPTER|INTRODUCTION|CONCLUSION)',
                         tag.get_text(' ', strip=True)))


def heading_anchor(p_tag):
    a = p_tag.find('a', attrs={'name': True})
    return a['name'] if a else ''


def wrap_xhtml(title, body_html, heading_html='', extra_css=''):
    if not body_html.strip():
        body_html = '<p>&#160;</p>'
    style_link = '<link rel="stylesheet" type="text/css" href="../style/main.css"/>'
    inline_style = f'<style>{extra_css}</style>' if extra_css else ''
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head><meta charset="utf-8"/><title>{title}</title>
{style_link}
{inline_style}
</head>
<body>
{heading_html}
{body_html}
</body>
</html>'''


# ── TOC parsing ───────────────────────────────────────────────────────────────

def _bold_links(p):
    b = p.find('b')
    return b.find_all('a', href=True) if b else []

def _italic_links(p):
    links = []
    for i_tag in p.find_all('i'):
        links.extend(i_tag.find_all('a', href=True))
    return links

def _bare_links(p):
    return [a for a in p.find_all('a', href=True)
            if not a.find_parent('b') and not a.find_parent('i')]

def _join_links(links):
    """Deduplicate consecutive same-href links, merging their text."""
    if not links:
        return []
    seen_href = None
    result = []
    for a in links:
        href = a['href']
        text = a.get_text(' ', strip=True)
        if href == seen_href and result:
            result[-1] = (result[-1][0] + ' ' + text, result[-1][1])
        else:
            result.append((text, href))
            seen_href = href
    return result


def parse_toc_nodes(top_children, toc_end_index):
    """
    Parse TOC paragraph nodes (children[0:toc_end_index]) into structured
    entries: list of {'level': 'chapter'|'section'|'subsection',
                      'text': str, 'href': str}

    Heading patterns:

    A) number + subtitle in one bold <p>, multiple <a> tags:
         <p><b><a>CHAPTER ONE</a><br/><a>SUBTITLE</a></b></p>

    B) number alone in one bold <p>, subtitle in the next bold <p>:
         <p><b><a>CHAPTER THREE</a></b></p>
         <p><b><a>SUBTITLE…</a></b></p>

    C) single bold link with section links in same paragraph (CONCLUSION):
         <p><b><a>CONCLUSION</a></b><i><a>section…</a>…</i></p>
    """
    raw = [c for c in top_children[:toc_end_index]
           if isinstance(c, Tag) and c.get_text(strip=True)]

    entries = []
    i = 0
    while i < len(raw):
        p = raw[i]
        bold_links = _bold_links(p)

        if bold_links:
            first_text = bold_links[0].get_text(' ', strip=True)
            ch_href    = bold_links[0]['href']
            is_ch_num  = bool(re.match(r'^CHAPTER\s+\w+$', first_text))

            if is_ch_num and len(bold_links) == 1:
                # Pattern B: standalone chapter number, peek for subtitle
                sub_parts = []
                while i + 1 < len(raw):
                    nxt = raw[i + 1]
                    nxt_bold = _bold_links(nxt)
                    if not nxt_bold:
                        break
                    nxt_first = nxt_bold[0].get_text(' ', strip=True)
                    if re.match(r'^(CHAPTER|INTRODUCTION|CONCLUSION)', nxt_first):
                        break
                    sub_parts.append(
                        ' '.join(a.get_text(' ', strip=True)
                                 for a in nxt_bold).strip()
                    )
                    i += 1
                combined = first_text
                if sub_parts:
                    combined += ': ' + ' '.join(sub_parts)
                entries.append({'level': 'chapter', 'text': combined,
                                'href': ch_href})

            elif len(bold_links) > 1:
                # Pattern A: number + subtitle in same paragraph
                subtitle = ' '.join(
                    a.get_text(' ', strip=True) for a in bold_links[1:]
                ).strip()
                combined = first_text + ': ' + subtitle if subtitle else first_text
                entries.append({'level': 'chapter', 'text': combined,
                                'href': ch_href})

            else:
                # Pattern C: single bold link (INTRODUCTION, CONCLUSION, …)
                entries.append({'level': 'chapter', 'text': first_text,
                                'href': ch_href})
                # Non-bold links in same paragraph → section entries
                # Merge consecutive same-href links (e.g. split Magnificat entry)
                all_section = _italic_links(p) + _bare_links(p)
                for text, href in _join_links(all_section):
                    if text:
                        entries.append({'level': 'section', 'text': text,
                                        'href': href})
        else:
            # Plain section / subsection paragraph
            for a in p.find_all('a', href=True):
                text = a.get_text(' ', strip=True)
                if not text:
                    continue
                level = 'subsection' if a.find_parent('i') else 'section'
                entries.append({'level': level, 'text': text,
                                'href': a['href']})

        i += 1

    return entries


def build_contents_html(toc_entries, anchor_to_file):
    css = {'chapter': 'toc-chapter', 'section': 'toc-section',
           'subsection': 'toc-subsection'}
    lines = ['<h2>Contents</h2>']
    for entry in toc_entries:
        anchor   = entry['href'].lstrip('#')
        resolved = anchor_to_file.get(anchor, f'intro.xhtml#{anchor}')
        cls      = css[entry['level']]
        lines.append(
            f'<p class="{cls}"><a href="{resolved}">{entry["text"]}</a></p>'
        )
    return '\n'.join(lines)


# ── chapter heading builder ───────────────────────────────────────────────────

def collect_heading_ps(top_children, start_idx):
    idxs = []
    j = start_idx
    while j < len(top_children):
        c = top_children[j]
        if isinstance(c, Tag) and is_centred_bold(c):
            idxs.append(j)
        elif isinstance(c, NavigableString):
            pass
        else:
            break
        j += 1
    return idxs


def make_chapter_heading(cid, heading_ps):
    if cid == 'footnotes':
        return '<h2>Notes</h2>'
    if not heading_ps:
        return ''
    parts = []
    for idx, p in enumerate(heading_ps):
        text = p.get_text(' ', strip=True)
        name = heading_anchor(p)
        id_attr = f' id="{name}"' if name else ''
        tag = 'h2' if idx == 0 else 'h3'
        parts.append(f'<{tag}{id_attr}>{text}</{tag}>')
    return '\n'.join(parts)


# ── footnote link rewriting ───────────────────────────────────────────────────

def build_footnote_rewrites(top_children, split_indices, chapter_ids):
    """
    Return two dicts for rewriting footnote hrefs across file boundaries.

    ftnref_rewrite:   '#_ftnN'    -> 'footnotes.xhtml#_ftnN'
                      (in-body refs that point forward to the footnotes file)

    backlink_rewrite: '#_ftnrefN' -> 'chap?.xhtml#_ftnrefN'
                      (back-links in footnotes.xhtml that point into body chapters)
    """
    ftnref_rewrite   = {}   # body refs -> footnotes.xhtml
    backlink_rewrite = {}   # footnote back-refs -> correct chapter file

    for k, cid in enumerate(chapter_ids):
        for j in range(split_indices[k], split_indices[k + 1]):
            c = top_children[j]
            if not isinstance(c, Tag):
                continue
            for a in c.find_all('a', attrs={'name': True}):
                name = a['name']
                if re.match(r'^_ftnref', name):
                    # This anchor lives in this chapter file
                    backlink_rewrite[f'#{name}'] = f'{cid}.xhtml#{name}'
                elif re.match(r'^_ftn\d', name):
                    # This anchor lives in footnotes.xhtml
                    ftnref_rewrite[f'#{name}'] = f'footnotes.xhtml#{name}'

    return ftnref_rewrite, backlink_rewrite


# ── main ──────────────────────────────────────────────────────────────────────

def build_epub(input_html, output_epub_path, cover_path=None):
    with open(input_html, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'lxml')

    content_divs = soup.find_all('div', class_='vaticanrichtext')
    abstract_div = content_divs[0]
    main_div     = content_divs[1]
    top_children = list(main_div.children)

    # ── structural boundaries ─────────────────────────────────────────────────

    split_indices = [i for i, c in enumerate(top_children)
                     if is_chapter_heading(c)]

    footnote_start = next(
        (i for i, c in enumerate(top_children)
         if isinstance(c, Tag) and c.find('a', attrs={'name': '_ftn1'})),
        len(top_children)
    )
    split_indices.append(footnote_start)
    split_indices.append(len(top_children))

    chapter_ids = ['intro', 'chap1', 'chap2', 'chap3', 'chap4', 'chap5',
                   'concl', 'footnotes']

    # ── anchor → xhtml-file map (for Contents page links) ────────────────────
    anchor_to_file = {}
    for k, cid in enumerate(chapter_ids):
        for j in range(split_indices[k], split_indices[k + 1]):
            c = top_children[j]
            if isinstance(c, Tag):
                for a in c.find_all('a', attrs={'name': True}):
                    anchor_to_file[a['name']] = f'{cid}.xhtml#{a["name"]}'

    # ── footnote cross-file link rewrites ─────────────────────────────────────
    ftnref_rewrite, backlink_rewrite = build_footnote_rewrites(
        top_children, split_indices, chapter_ids)

    # ── heading-p suppression map ─────────────────────────────────────────────
    heading_p_map = {s: collect_heading_ps(top_children, s)
                     for s in split_indices[:-2]}

    # ── epub assembly ─────────────────────────────────────────────────────────

    book = epub.EpubBook()
    book.set_identifier('magnifica-humanitas-leo-xiv-2026')
    book.set_title('Magnifica Humanitas')
    book.set_language('en')
    book.add_author('Pope Leo XIV')
    book.add_metadata('DC', 'description',
        'Encyclical Letter on Safeguarding the Human Person '
        'in the Time of Artificial Intelligence')
    book.add_metadata('DC', 'date', '2026-05-15')

    style_item = epub.EpubItem(uid='css', file_name='style/main.css',
                               media_type='text/css', content=CSS)
    book.add_item(style_item)

    spine_items = []
    toc_links   = []

    # ── cover page ────────────────────────────────────────────────────────────
    if cover_path:
        cover_data = Path(cover_path).read_bytes()
        # Add raw image to manifest
        cover_img = epub.EpubItem(
            uid='cover-image',
            file_name='images/cover.png',
            media_type='image/png',
            content=cover_data,
        )
        book.add_item(cover_img)
        # Mark it as the cover image in OPF metadata
        book.add_metadata(None, 'meta', '', {'name': 'cover',
                                             'content': 'cover-image'})
        # Explicit cover XHTML page
        cover_body = '<img class="cover" src="images/cover.png" alt="Cover"/>'
        cover_ch = epub.EpubHtml(
            title='Cover', file_name='cover.xhtml', lang='en')
        cover_ch.content = wrap_xhtml(
            'Cover', cover_body, extra_css=CSS_COVER).encode('utf-8')
        cover_ch.add_item(style_item)
        book.add_item(cover_ch)
        spine_items.append(cover_ch)   # first visible page

    # ── title page (spine only, not in toc) ───────────────────────────────────
    abs_copy = copy.deepcopy(abstract_div)
    for a in abs_copy.find_all('a', href=True):
        if 'vaticanevents' in a.get('href', ''):
            p = a.find_parent('p')
            if p:
                p.decompose()
            break

    title_ch = epub.EpubHtml(title='Title Page', file_name='title.xhtml', lang='en')
    title_ch.content = wrap_xhtml(
        'Title Page', str(abs_copy),
        '<h1>Magnifica Humanitas</h1>\n'
        '<h2>Pope Leo XIV</h2>\n'
        '<h3>Encyclical Letter · 15 May 2026</h3>'
    ).encode('utf-8')
    title_ch.add_item(style_item)
    book.add_item(title_ch)
    spine_items.append(title_ch)

    # ── copyright page (spine only, not in toc) ───────────────────────────────
    copyright_body = (
        '<p>Copyright &#169; Libreria Editrice Vaticana</p>\n'
        '<p><i>Texts from www.vatican.va</i></p>'
    )
    copyright_ch = epub.EpubHtml(title='Copyright', file_name='copyright.xhtml', lang='en')
    copyright_ch.content = wrap_xhtml('Copyright', copyright_body).encode('utf-8')
    copyright_ch.add_item(style_item)
    book.add_item(copyright_ch)
    spine_items.append(copyright_ch)

    # ── contents page (spine only, not in toc) ────────────────────────────────
    toc_entries   = parse_toc_nodes(top_children, split_indices[0])
    contents_body = build_contents_html(toc_entries, anchor_to_file)

    contents_ch = epub.EpubHtml(title='Contents', file_name='contents.xhtml', lang='en')
    contents_ch.content = wrap_xhtml('Contents', contents_body).encode('utf-8')
    contents_ch.add_item(style_item)
    book.add_item(contents_ch)
    spine_items.append(contents_ch)

    # ── body chapters (spine + toc) ───────────────────────────────────────────
    chapter_defs = [
        ('intro',     'Introduction'),
        ('chap1',     'Chapter One'),
        ('chap2',     'Chapter Two'),
        ('chap3',     'Chapter Three'),
        ('chap4',     'Chapter Four'),
        ('chap5',     'Chapter Five'),
        ('concl',     'Conclusion'),
        ('footnotes', 'Notes'),
    ]

    for k, (cid, ctitle) in enumerate(chapter_defs):
        start = split_indices[k]
        end   = split_indices[k + 1]
        nodes = list(top_children[start:end])

        heading_ps_global = heading_p_map.get(start, [])
        suppress          = {j - start for j in heading_ps_global}

        # Footnote files get back-link rewrites; body chapters get ftnref rewrites
        if cid == 'footnotes':
            body_html = nodes_to_html(nodes, skip_indices=suppress,
                                      ftn_backlink_rewrite=backlink_rewrite)
        else:
            body_html = nodes_to_html(nodes, skip_indices=suppress,
                                      ftnref_rewrite=ftnref_rewrite)

        heading_html = make_chapter_heading(
            cid, [top_children[j] for j in heading_ps_global])

        ch = epub.EpubHtml(title=ctitle, file_name=f'{cid}.xhtml', lang='en')
        ch.content = wrap_xhtml(ctitle, body_html, heading_html).encode('utf-8')
        ch.add_item(style_item)
        book.add_item(ch)
        spine_items.append(ch)
        toc_links.append(epub.Link(f'{cid}.xhtml', ctitle, cid))

    # ── ncx / spine ───────────────────────────────────────────────────────────
    book.toc   = tuple(toc_links)
    book.spine = spine_items
    book.add_item(epub.EpubNcx())

    epub.write_epub(output_epub_path, book)
    import os
    print(f"Written: {output_epub_path}  ({os.path.getsize(output_epub_path):,} bytes)")


if __name__ == '__main__':
    if len(sys.argv) == 3:
        build_epub(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 4:
        build_epub(sys.argv[1], sys.argv[3], cover_path=sys.argv[2])
    else:
        print(f"Usage: {sys.argv[0]} input.html [cover.png] output.epub")
        sys.exit(1)
