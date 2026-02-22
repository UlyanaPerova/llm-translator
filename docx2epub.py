#!/usr/bin/env python3
"""
DOCX → EPUB Converter with Full Formatting Preservation
========================================================
Preserves: tabs, indents, bold/italic/underline/strikethrough,
fonts, colors, sizes, alignment, lists, images, footnotes,
hyperlinks, headings, page breaks, tables, and paragraph spacing.

Usage:
    python docx2epub.py input.docx [-o output.epub] [--title "Title"] [--author "Author"]
    python docx2epub.py input.docx --cover cover.jpg --lang ru

Requirements:
    pip install python-docx ebooklib lxml Pillow
"""

import argparse
import base64
import io
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from logger import setup_logger
import logging

setup_logger()
log = logging.getLogger("docx2epub") 

from docx import Document
from docx.shared import Pt, Cm, Inches, Emu, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn, nsmap
from ebooklib import epub



# ─── Namespace helpers ───────────────────────────────────────────────

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _val(element, tag, ns=W_NS):
    """Get w:val attribute from a child element."""
    child = element.find(f"{{{ns}}}{tag}")
    if child is not None:
        return child.get(f"{{{W_NS}}}val") or child.get("val")
    return None


def _get_xml_attr(element, tag, attr, ns=W_NS):
    """Get specific attribute from a child element."""
    child = element.find(f"{{{ns}}}{tag}")
    if child is not None:
        return child.get(f"{{{ns}}}{attr}") or child.get(attr)
    return None


# ─── Style extraction ────────────────────────────────────────────────

class RunStyle:
    """Extracted run-level formatting."""
    def __init__(self):
        self.bold = False
        self.italic = False
        self.underline = False
        self.strikethrough = False
        self.superscript = False
        self.subscript = False
        self.small_caps = False
        self.all_caps = False
        self.font_name: Optional[str] = None
        self.font_size: Optional[float] = None  # in pt
        self.color: Optional[str] = None  # hex
        self.highlight: Optional[str] = None
        self.bg_color: Optional[str] = None


class ParaStyle:
    """Extracted paragraph-level formatting."""
    def __init__(self):
        self.alignment: Optional[str] = None
        self.indent_left: Optional[float] = None   # in pt
        self.indent_right: Optional[float] = None
        self.indent_first: Optional[float] = None
        self.hanging: Optional[float] = None
        self.space_before: Optional[float] = None
        self.space_after: Optional[float] = None
        self.line_spacing: Optional[float] = None
        self.line_rule: Optional[str] = None
        self.keep_next = False
        self.page_break_before = False
        self.outline_level: Optional[int] = None  # heading level
        self.is_list = False
        self.list_level: int = 0
        self.num_id: Optional[str] = None
        self.tabs: list = []  # list of (position_pt, alignment)
        self.border_bottom = False
        self.bg_color: Optional[str] = None


def _is_on(val):
    """Check if an on/off value is 'on'."""
    if val is None:
        return False
    return val in ("1", "true", "on", "")


def _twips_to_pt(twips):
    """Convert twips to points."""
    if twips is None:
        return None
    try:
        return int(twips) / 20.0
    except (ValueError, TypeError):
        return None


def _half_pt_to_pt(val):
    """Convert half-points to points."""
    if val is None:
        return None
    try:
        return int(val) / 2.0
    except (ValueError, TypeError):
        return None


def _emu_to_px(emu):
    """Convert EMU to pixels (96 DPI)."""
    if emu is None:
        return None
    try:
        return round(int(emu) / 914400 * 96)
    except (ValueError, TypeError):
        return None


HIGHLIGHT_MAP = {
    "yellow": "#FFFF00", "green": "#00FF00", "cyan": "#00FFFF",
    "magenta": "#FF00FF", "blue": "#0000FF", "red": "#FF0000",
    "darkBlue": "#00008B", "darkCyan": "#008B8B", "darkGreen": "#006400",
    "darkMagenta": "#8B008B", "darkRed": "#8B0000", "darkYellow": "#808000",
    "darkGray": "#A9A9A9", "lightGray": "#D3D3D3", "black": "#000000",
    "white": "#FFFFFF",
}


def extract_run_style(run) -> RunStyle:
    """Extract formatting from a python-docx Run object."""
    rs = RunStyle()
    rpr = run._element.find(f"{{{W_NS}}}rPr")

    # Direct properties from python-docx
    if run.bold:
        rs.bold = True
    if run.italic:
        rs.italic = True
    if run.underline:
        rs.underline = True

    if rpr is not None:
        # Strikethrough
        strike = rpr.find(f"{{{W_NS}}}strike")
        if strike is not None:
            val = strike.get(f"{{{W_NS}}}val")
            rs.strikethrough = val != "false" and val != "0"

        dstrike = rpr.find(f"{{{W_NS}}}dstrike")
        if dstrike is not None:
            val = dstrike.get(f"{{{W_NS}}}val")
            if val != "false" and val != "0":
                rs.strikethrough = True

        # Superscript/subscript
        vert_align = _val(rpr, "vertAlign")
        if vert_align == "superscript":
            rs.superscript = True
        elif vert_align == "subscript":
            rs.subscript = True

        # Small caps / all caps
        sc = rpr.find(f"{{{W_NS}}}smallCaps")
        if sc is not None:
            val = sc.get(f"{{{W_NS}}}val")
            rs.small_caps = val != "false" and val != "0"

        caps = rpr.find(f"{{{W_NS}}}caps")
        if caps is not None:
            val = caps.get(f"{{{W_NS}}}val")
            rs.all_caps = val != "false" and val != "0"

        # Font
        rFonts = rpr.find(f"{{{W_NS}}}rFonts")
        if rFonts is not None:
            rs.font_name = (
                rFonts.get(f"{{{W_NS}}}ascii")
                or rFonts.get(f"{{{W_NS}}}hAnsi")
                or rFonts.get(f"{{{W_NS}}}cs")
            )

        # Size
        sz = rpr.find(f"{{{W_NS}}}sz")
        if sz is not None:
            rs.font_size = _half_pt_to_pt(sz.get(f"{{{W_NS}}}val"))

        # Color
        color_el = rpr.find(f"{{{W_NS}}}color")
        if color_el is not None:
            c = color_el.get(f"{{{W_NS}}}val")
            if c and c.lower() != "auto":
                rs.color = f"#{c}"

        # Highlight
        hl = rpr.find(f"{{{W_NS}}}highlight")
        if hl is not None:
            hl_val = hl.get(f"{{{W_NS}}}val")
            rs.highlight = HIGHLIGHT_MAP.get(hl_val)

        # Shading (background)
        shd = rpr.find(f"{{{W_NS}}}shd")
        if shd is not None:
            fill = shd.get(f"{{{W_NS}}}fill")
            if fill and fill.lower() not in ("auto", "ffffff"):
                rs.bg_color = f"#{fill}"

    return rs


def extract_para_style(para) -> ParaStyle:
    """Extract formatting from a python-docx Paragraph object."""
    ps = ParaStyle()
    ppr = para._element.find(f"{{{W_NS}}}pPr")

    # Alignment
    align = para.alignment
    if align == WD_ALIGN_PARAGRAPH.CENTER:
        ps.alignment = "center"
    elif align == WD_ALIGN_PARAGRAPH.RIGHT:
        ps.alignment = "right"
    elif align == WD_ALIGN_PARAGRAPH.JUSTIFY:
        ps.alignment = "justify"
    elif align == WD_ALIGN_PARAGRAPH.LEFT:
        ps.alignment = "left"

    if ppr is None:
        return ps

    # Indentation
    ind = ppr.find(f"{{{W_NS}}}ind")
    if ind is not None:
        ps.indent_left = _twips_to_pt(ind.get(f"{{{W_NS}}}left"))
        ps.indent_right = _twips_to_pt(ind.get(f"{{{W_NS}}}right"))
        ps.indent_first = _twips_to_pt(ind.get(f"{{{W_NS}}}firstLine"))
        ps.hanging = _twips_to_pt(ind.get(f"{{{W_NS}}}hanging"))

    # Spacing
    spacing = ppr.find(f"{{{W_NS}}}spacing")
    if spacing is not None:
        ps.space_before = _twips_to_pt(spacing.get(f"{{{W_NS}}}before"))
        ps.space_after = _twips_to_pt(spacing.get(f"{{{W_NS}}}after"))
        line = spacing.get(f"{{{W_NS}}}line")
        if line:
            ps.line_spacing = int(line) / 240.0  # line spacing multiplier
        ps.line_rule = spacing.get(f"{{{W_NS}}}lineRule")

    # Keep with next
    kn = ppr.find(f"{{{W_NS}}}keepNext")
    if kn is not None:
        ps.keep_next = True

    # Page break before
    pbb = ppr.find(f"{{{W_NS}}}pageBreakBefore")
    if pbb is not None:
        val = pbb.get(f"{{{W_NS}}}val")
        ps.page_break_before = val != "false" and val != "0"

    # Outline level (for headings)
    ol = ppr.find(f"{{{W_NS}}}outlineLvl")
    if ol is not None:
        try:
            ps.outline_level = int(ol.get(f"{{{W_NS}}}val"))
        except (ValueError, TypeError):
            pass

    # List info
    numPr = ppr.find(f"{{{W_NS}}}numPr")
    if numPr is not None:
        ps.is_list = True
        ilvl = numPr.find(f"{{{W_NS}}}ilvl")
        if ilvl is not None:
            try:
                ps.list_level = int(ilvl.get(f"{{{W_NS}}}val"))
            except (ValueError, TypeError):
                pass
        numId = numPr.find(f"{{{W_NS}}}numId")
        if numId is not None:
            ps.num_id = numId.get(f"{{{W_NS}}}val")

    # Tabs
    tabs_el = ppr.find(f"{{{W_NS}}}tabs")
    if tabs_el is not None:
        for tab in tabs_el.findall(f"{{{W_NS}}}tab"):
            pos = _twips_to_pt(tab.get(f"{{{W_NS}}}pos"))
            alignment = tab.get(f"{{{W_NS}}}val") or "left"
            leader = tab.get(f"{{{W_NS}}}leader")
            if pos is not None:
                ps.tabs.append((pos, alignment, leader))

    # Border bottom (for HR-like elements)
    pBdr = ppr.find(f"{{{W_NS}}}pBdr")
    if pBdr is not None:
        bottom = pBdr.find(f"{{{W_NS}}}bottom")
        if bottom is not None:
            val = bottom.get(f"{{{W_NS}}}val")
            if val and val != "none":
                ps.border_bottom = True

    # Background shading
    shd = ppr.find(f"{{{W_NS}}}shd")
    if shd is not None:
        fill = shd.get(f"{{{W_NS}}}fill")
        if fill and fill.lower() not in ("auto", "ffffff"):
            ps.bg_color = f"#{fill}"

    return ps


# ─── Image extraction ────────────────────────────────────────────────

def extract_images(doc: Document) -> dict:
    """Extract all images from the DOCX. Returns {rId: (content_type, bytes)}."""
    images = {}
    for rel_id, rel in doc.part.rels.items():
        if "image" in rel.reltype:
            try:
                blob = rel.target_part.blob
                ct = rel.target_part.content_type
                images[rel_id] = (ct, blob)
            except Exception:
                pass
    return images


def find_images_in_paragraph(para) -> list:
    """Find image references in a paragraph element. Returns list of (rId, width_px, height_px)."""
    results = []
    # Inline images
    for drawing in para._element.findall(f".//{{{WP_NS}}}inline"):
        extent = drawing.find(f"{{{WP_NS}}}extent")
        w = _emu_to_px(extent.get("cx")) if extent is not None else None
        h = _emu_to_px(extent.get("cy")) if extent is not None else None
        blip = drawing.find(f".//{{{A_NS}}}blip")
        if blip is not None:
            rId = blip.get(f"{{{R_NS}}}embed")
            if rId:
                results.append((rId, w, h))

    # Anchor images
    for drawing in para._element.findall(f".//{{{WP_NS}}}anchor"):
        extent = drawing.find(f"{{{WP_NS}}}extent")
        w = _emu_to_px(extent.get("cx")) if extent is not None else None
        h = _emu_to_px(extent.get("cy")) if extent is not None else None
        blip = drawing.find(f".//{{{A_NS}}}blip")
        if blip is not None:
            rId = blip.get(f"{{{R_NS}}}embed")
            if rId:
                results.append((rId, w, h))

    return results


def find_images_in_run(run) -> list:
    """Find image references in a specific run element."""
    results = []
    for drawing in run._element.findall(f".//{{{WP_NS}}}inline"):
        extent = drawing.find(f"{{{WP_NS}}}extent")
        w = _emu_to_px(extent.get("cx")) if extent is not None else None
        h = _emu_to_px(extent.get("cy")) if extent is not None else None
        blip = drawing.find(f".//{{{A_NS}}}blip")
        if blip is not None:
            rId = blip.get(f"{{{R_NS}}}embed")
            if rId:
                results.append((rId, w, h))
    for drawing in run._element.findall(f".//{{{WP_NS}}}anchor"):
        extent = drawing.find(f"{{{WP_NS}}}extent")
        w = _emu_to_px(extent.get("cx")) if extent is not None else None
        h = _emu_to_px(extent.get("cy")) if extent is not None else None
        blip = drawing.find(f".//{{{A_NS}}}blip")
        if blip is not None:
            rId = blip.get(f"{{{R_NS}}}embed")
            if rId:
                results.append((rId, w, h))
    return results


# ─── Footnote/Endnote extraction ─────────────────────────────────────

def extract_footnotes(doc: Document) -> dict:
    """Extract footnotes. Returns {id: text}."""
    footnotes = {}
    try:
        fn_part = doc.part.package.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
        )
        if fn_part is not None:
            root = ET.fromstring(fn_part.blob)
            for fn in root.findall(f"{{{W_NS}}}footnote"):
                fn_id = fn.get(f"{{{W_NS}}}id")
                if fn_id in ("0", "-1"):  # separator/continuation
                    continue
                texts = []
                for t in fn.iter(f"{{{W_NS}}}t"):
                    if t.text:
                        texts.append(t.text)
                footnotes[fn_id] = " ".join(texts)
    except Exception:
        pass
    return footnotes


def find_footnote_refs(run) -> list:
    """Find footnote references in a run."""
    refs = []
    for fr in run._element.findall(f"{{{W_NS}}}footnoteReference"):
        fid = fr.get(f"{{{W_NS}}}id")
        if fid and fid not in ("0", "-1"):
            refs.append(fid)
    return refs


# ─── Hyperlink extraction ────────────────────────────────────────────

def get_hyperlinks_in_para(para, doc_rels) -> dict:
    """Get hyperlinks in paragraph. Returns {rId: url}."""
    links = {}
    for hl in para._element.findall(f"{{{W_NS}}}hyperlink"):
        rId = hl.get(f"{{{R_NS}}}id")
        if rId and rId in doc_rels:
            rel = doc_rels[rId]
            if hasattr(rel, 'target_ref'):
                links[rId] = rel.target_ref
            elif hasattr(rel, '_target'):
                links[rId] = str(rel._target)
    return links


# ─── Table extraction ────────────────────────────────────────────────

def convert_table(table, doc, images_map, epub_images) -> str:
    """Convert a docx table to HTML."""
    html = '<table style="border-collapse:collapse;width:100%;margin:0.5em 0;">\n'

    for row in table.rows:
        html += "  <tr>\n"
        for cell in row.cells:
            # Cell properties
            cell_style = "border:1px solid #999;padding:4px 8px;vertical-align:top;"
            tc_pr = cell._element.find(f"{{{W_NS}}}tcPr")
            if tc_pr is not None:
                shd = tc_pr.find(f"{{{W_NS}}}shd")
                if shd is not None:
                    fill = shd.get(f"{{{W_NS}}}fill")
                    if fill and fill.lower() not in ("auto", "ffffff"):
                        cell_style += f"background-color:#{fill};"

                # Column span
                grid_span = tc_pr.find(f"{{{W_NS}}}gridSpan")
                colspan = ""
                if grid_span is not None:
                    span = grid_span.get(f"{{{W_NS}}}val")
                    if span and int(span) > 1:
                        colspan = f' colspan="{span}"'

                # Vertical merge
                vmerge = tc_pr.find(f"{{{W_NS}}}vMerge")
                if vmerge is not None:
                    val = vmerge.get(f"{{{W_NS}}}val")
                    if val is None:  # continuation cell
                        continue
            else:
                colspan = ""

            cell_html = ""
            for p in cell.paragraphs:
                cell_html += convert_paragraph_to_html(
                    p, doc, images_map, epub_images, {}
                )

            html += f'    <td style="{cell_style}"{colspan}>{cell_html}</td>\n'
        html += "  </tr>\n"

    html += "</table>\n"
    return html


# ─── CSS generation ──────────────────────────────────────────────────

def run_style_to_css(rs: RunStyle) -> str:
    """Convert RunStyle to inline CSS."""
    parts = []
    if rs.bold:
        parts.append("font-weight:bold")
    if rs.italic:
        parts.append("font-style:italic")
    if rs.underline:
        parts.append("text-decoration:underline")
    if rs.strikethrough:
        if rs.underline:
            parts.append("text-decoration:underline line-through")
        else:
            parts.append("text-decoration:line-through")
    if rs.font_name:
        parts.append(f"font-family:'{rs.font_name}',serif")
    if rs.font_size:
        parts.append(f"font-size:{rs.font_size}pt")
    if rs.color:
        parts.append(f"color:{rs.color}")
    if rs.highlight:
        parts.append(f"background-color:{rs.highlight}")
    elif rs.bg_color:
        parts.append(f"background-color:{rs.bg_color}")
    if rs.small_caps:
        parts.append("font-variant:small-caps")
    if rs.all_caps:
        parts.append("text-transform:uppercase")
    return ";".join(parts)


def para_style_to_css(ps: ParaStyle) -> str:
    """Convert ParaStyle to inline CSS."""
    parts = []
    if ps.alignment:
        parts.append(f"text-align:{ps.alignment}")
    if ps.indent_left and ps.indent_left > 0:
        parts.append(f"margin-left:{ps.indent_left}pt")
    if ps.indent_right and ps.indent_right > 0:
        parts.append(f"margin-right:{ps.indent_right}pt")
    if ps.indent_first and ps.indent_first > 0:
        parts.append(f"text-indent:{ps.indent_first}pt")
    if ps.hanging and ps.hanging > 0:
        parts.append(f"text-indent:-{ps.hanging}pt")
        if not ps.indent_left:
            parts.append(f"padding-left:{ps.hanging}pt")
    if ps.space_before is not None:
        parts.append(f"margin-top:{ps.space_before}pt")
    if ps.space_after is not None:
        parts.append(f"margin-bottom:{ps.space_after}pt")
    if ps.line_spacing and ps.line_rule != "exact":
        parts.append(f"line-height:{ps.line_spacing:.2f}")
    elif ps.line_spacing and ps.line_rule == "exact":
        parts.append(f"line-height:{ps.line_spacing * 12}pt")
    if ps.page_break_before:
        parts.append("page-break-before:always")
    if ps.border_bottom:
        parts.append("border-bottom:1px solid #000;padding-bottom:4pt")
    if ps.bg_color:
        parts.append(f"background-color:{ps.bg_color};padding:4pt")
    return ";".join(parts)


# ─── Tab handling ─────────────────────────────────────────────────────

def render_tab(ps: ParaStyle, tab_index: int) -> str:
    """Render a tab character as HTML with appropriate spacing."""
    if ps.tabs and tab_index < len(ps.tabs):
        pos, alignment, leader = ps.tabs[tab_index]
        leader_char = ""
        if leader == "dot":
            leader_char = "."
        elif leader == "hyphen":
            leader_char = "-"
        elif leader == "underscore":
            leader_char = "_"

        if leader_char:
            # Tab with leader: use a span that stretches
            return (
                f'<span style="display:inline-block;min-width:{pos}pt;'
                f'border-bottom:1px dotted #000;"></span>'
                if leader == "dot" else
                f'<span style="display:inline-block;min-width:{pos}pt;">'
                f'{"&nbsp;" * int(pos / 6)}</span>'
            )
        else:
            return f'<span style="display:inline-block;min-width:2em;"></span>'
    else:
        # Default tab: 0.5 inch = ~36pt
        return '<span style="display:inline-block;width:2em;"></span>'


# ─── Paragraph to HTML ───────────────────────────────────────────────

def convert_paragraph_to_html(
    para, doc, images_map, epub_images, footnotes,
    heading_level=None
) -> str:
    """Convert a single paragraph to HTML."""
    ps = extract_para_style(para)
    css = para_style_to_css(ps)

    # Determine heading level
    h_level = heading_level
    if h_level is None and para.style and para.style.name:
        style_name = para.style.name.lower()
        for i in range(1, 7):
            if style_name == f"heading {i}" or style_name == f"heading{i}":
                h_level = i
                break
        if ps.outline_level is not None and h_level is None:
            h_level = min(ps.outline_level + 1, 6)

    # Build inline HTML
    inline_html = ""
    tab_index = 0

    # Check for hyperlinks at element level
    doc_rels = doc.part.rels
    hyperlinks = {}
    for hl in para._element.findall(f"{{{W_NS}}}hyperlink"):
        rId = hl.get(f"{{{R_NS}}}id")
        anchor = hl.get(f"{{{W_NS}}}anchor")
        url = None
        if rId and rId in doc_rels:
            rel = doc_rels[rId]
            if hasattr(rel, 'target_ref'):
                url = rel.target_ref
            elif hasattr(rel, '_target'):
                url = str(rel._target)
        elif anchor:
            url = f"#{anchor}"
        if url:
            hyperlinks[id(hl)] = url

    # Process all child elements in order
    for child in para._element:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "r":  # Regular run
            inline_html += _process_run_element(
                child, para, doc, images_map, epub_images,
                footnotes, ps, tab_index
            )
            # Count tabs in this run
            tab_index += len(child.findall(f"{{{W_NS}}}tab"))

        elif tag == "hyperlink":
            # Process hyperlink
            url = hyperlinks.get(id(child), "#")
            link_html = ""
            for r in child.findall(f"{{{W_NS}}}r"):
                link_html += _process_run_element(
                    r, para, doc, images_map, epub_images,
                    footnotes, ps, tab_index
                )
                tab_index += len(r.findall(f"{{{W_NS}}}tab"))
            inline_html += f'<a href="{_escape_html(url)}">{link_html}</a>'

    # Empty paragraph → <br/>
    if not inline_html.strip():
        inline_html = "&#160;"

    # Wrap in tag
    if h_level and 1 <= h_level <= 6:
        tag = f"h{h_level}"
        style_attr = f' style="{css}"' if css else ""
        return f"<{tag}{style_attr}>{inline_html}</{tag}>\n"

    if ps.is_list:
        # Use div with list-like styling
        level_indent = 20 + ps.list_level * 20
        list_css = f"margin-left:{level_indent}pt;{css}"
        return f'<p style="{list_css}">{inline_html}</p>\n'

    style_attr = f' style="{css}"' if css else ""
    return f"<p{style_attr}>{inline_html}</p>\n"


def _process_run_element(run_el, para, doc, images_map, epub_images, footnotes, ps, tab_index):
    """Process a w:r XML element and return HTML."""
    html = ""

    # Extract run properties
    rpr = run_el.find(f"{{{W_NS}}}rPr")
    rs = RunStyle()

    if rpr is not None:
        # Bold
        b = rpr.find(f"{{{W_NS}}}b")
        if b is not None:
            val = b.get(f"{{{W_NS}}}val")
            rs.bold = val != "false" and val != "0"
        bCs = rpr.find(f"{{{W_NS}}}bCs")
        if bCs is not None and not rs.bold:
            val = bCs.get(f"{{{W_NS}}}val")
            rs.bold = val != "false" and val != "0"

        # Italic
        i = rpr.find(f"{{{W_NS}}}i")
        if i is not None:
            val = i.get(f"{{{W_NS}}}val")
            rs.italic = val != "false" and val != "0"
        iCs = rpr.find(f"{{{W_NS}}}iCs")
        if iCs is not None and not rs.italic:
            val = iCs.get(f"{{{W_NS}}}val")
            rs.italic = val != "false" and val != "0"

        # Underline
        u = rpr.find(f"{{{W_NS}}}u")
        if u is not None:
            val = u.get(f"{{{W_NS}}}val")
            rs.underline = val is not None and val != "none"

        # Strikethrough
        strike = rpr.find(f"{{{W_NS}}}strike")
        if strike is not None:
            val = strike.get(f"{{{W_NS}}}val")
            rs.strikethrough = val != "false" and val != "0"

        # Super/sub
        vert = _val(rpr, "vertAlign")
        if vert == "superscript":
            rs.superscript = True
        elif vert == "subscript":
            rs.subscript = True

        # Small caps
        sc = rpr.find(f"{{{W_NS}}}smallCaps")
        if sc is not None:
            val = sc.get(f"{{{W_NS}}}val")
            rs.small_caps = val != "false" and val != "0"

        caps = rpr.find(f"{{{W_NS}}}caps")
        if caps is not None:
            val = caps.get(f"{{{W_NS}}}val")
            rs.all_caps = val != "false" and val != "0"

        # Font
        rFonts = rpr.find(f"{{{W_NS}}}rFonts")
        if rFonts is not None:
            rs.font_name = (
                rFonts.get(f"{{{W_NS}}}ascii")
                or rFonts.get(f"{{{W_NS}}}hAnsi")
                or rFonts.get(f"{{{W_NS}}}cs")
            )

        # Size
        sz = rpr.find(f"{{{W_NS}}}sz")
        if sz is not None:
            rs.font_size = _half_pt_to_pt(sz.get(f"{{{W_NS}}}val"))

        # Color
        color_el = rpr.find(f"{{{W_NS}}}color")
        if color_el is not None:
            c = color_el.get(f"{{{W_NS}}}val")
            if c and c.lower() != "auto":
                rs.color = f"#{c}"

        # Highlight
        hl = rpr.find(f"{{{W_NS}}}highlight")
        if hl is not None:
            hl_val = hl.get(f"{{{W_NS}}}val")
            rs.highlight = HIGHLIGHT_MAP.get(hl_val)

        # Shading
        shd = rpr.find(f"{{{W_NS}}}shd")
        if shd is not None:
            fill = shd.get(f"{{{W_NS}}}fill")
            if fill and fill.lower() not in ("auto", "ffffff"):
                rs.bg_color = f"#{fill}"

    run_css = run_style_to_css(rs)
    current_tab = tab_index

    for elem in run_el:
        elem_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

        if elem_tag == "t":
            text = elem.text or ""
            text = _escape_html(text)
            if run_css:
                html += f'<span style="{run_css}">{text}</span>'
            else:
                html += text

        elif elem_tag == "tab":
            html += render_tab(ps, current_tab)
            current_tab += 1

        elif elem_tag == "br":
            br_type = elem.get(f"{{{W_NS}}}type")
            if br_type == "page":
                html += '<div style="page-break-after:always;"></div>'
            elif br_type == "column":
                html += "<br/>"
            else:
                html += "<br/>"

        elif elem_tag == "drawing" or elem_tag == "pict":
            # Image
            for drawing in [elem]:
                blips = drawing.findall(f".//{{{A_NS}}}blip")
                for blip in blips:
                    rId = blip.get(f"{{{R_NS}}}embed")
                    if rId and rId in images_map:
                        ct, blob = images_map[rId]
                        ext = ct.split("/")[-1].replace("jpeg", "jpg")
                        fname = f"images/img_{rId}.{ext}"

                        if fname not in epub_images:
                            epub_images[fname] = (ct, blob)

                        # Get dimensions
                        extent = drawing.find(f".//{{{WP_NS}}}extent")
                        w = _emu_to_px(extent.get("cx")) if extent is not None else None
                        h = _emu_to_px(extent.get("cy")) if extent is not None else None

                        style_parts = ["max-width:100%"]
                        if w:
                            style_parts.append(f"width:{w}px")
                        if h:
                            style_parts.append(f"height:{h}px")

                        html += f'<img src="{fname}" style="{";".join(style_parts)}" alt=""/>'

        elif elem_tag == "footnoteReference":
            fid = elem.get(f"{{{W_NS}}}id")
            if fid and fid in footnotes:
                fn_text = _escape_html(footnotes[fid])
                html += (
                    f'<sup><a href="#fn{fid}" id="fnref{fid}" '
                    f'epub:type="noteref">[{fid}]</a></sup>'
                )

        elif elem_tag == "sym":
            # Symbol character
            char = elem.get(f"{{{W_NS}}}char")
            font = elem.get(f"{{{W_NS}}}font")
            if char:
                try:
                    html += chr(int(char, 16))
                except (ValueError, OverflowError):
                    html += "&#x" + char + ";"

    # Handle superscript/subscript wrapping
    if rs.superscript:
        html = f"<sup>{html}</sup>"
    elif rs.subscript:
        html = f"<sub>{html}</sub>"

    return html


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─── Main conversion ─────────────────────────────────────────────────

def docx_to_epub(
    input_path: str,
    output_path: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    cover_path: Optional[str] = None,
    language: str = "ru",
    chapter_split: str = "heading1",
    css_override: Optional[str] = None,
) -> str:
    """
    Convert DOCX to EPUB with full formatting preservation.

    Args:
        input_path: Path to input .docx file
        output_path: Path for output .epub (default: same name as input)
        title: Book title (default: filename)
        author: Book author
        cover_path: Path to cover image
        language: Language code (default: 'ru')
        chapter_split: How to split chapters ('heading1', 'pagebreak', 'none')
        css_override: Custom CSS to use instead of generated

    Returns:
        Path to created .epub file
    """
    input_path = os.path.abspath(input_path)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"File not found: {input_path}")

    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + ".epub"

    # Load document
    doc = Document(input_path)

    # Extract metadata
    core_props = doc.core_properties
    if not title:
        title = core_props.title or Path(input_path).stem
    if not author:
        author = core_props.author or "Unknown"

    # Extract resources
    images_map = extract_images(doc)
    footnotes = extract_footnotes(doc)
    epub_images = {}  # fname -> (content_type, bytes)

    # ─── Build chapters ───────────────────────────────────────────
    chapters = []  # list of (title, html_content)
    current_title = title
    current_html = ""

    # Process body elements (paragraphs and tables)
    body = doc.element.body
    for element in body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag

        if tag == "p":
            # It's a paragraph
            from docx.text.paragraph import Paragraph
            para = Paragraph(element, doc)
            ps = extract_para_style(para)

            # Check for chapter split
            is_new_chapter = False
            h_level = None

            if para.style and para.style.name:
                style_name = para.style.name.lower()
                if chapter_split == "heading1" and (
                    style_name == "heading 1" or style_name == "heading1"
                ):
                    is_new_chapter = True
                    h_level = 1

            if chapter_split == "pagebreak" and ps.page_break_before:
                is_new_chapter = True

            if is_new_chapter and current_html.strip():
                chapters.append((current_title, current_html))
                current_title = para.text.strip() or f"Chapter {len(chapters) + 1}"
                current_html = ""

            current_html += convert_paragraph_to_html(
                para, doc, images_map, epub_images, footnotes,
                heading_level=h_level
            )

        elif tag == "tbl":
            # It's a table
            from docx.table import Table
            table = Table(element, doc)
            current_html += convert_table(table, doc, images_map, epub_images)

        elif tag == "sectPr":
            pass  # Section properties, skip

    # Add last chapter
    if current_html.strip():
        chapters.append((current_title, current_html))

    # If no chapters, make one
    if not chapters:
        chapters = [(title, "<p>Empty document</p>")]

    # ─── Default stylesheet ───────────────────────────────────────
    default_css = css_override or """
/* Base typography */
body {
    font-family: 'Georgia', 'Times New Roman', 'Noto Serif', serif;
    line-height: 1.5;
    margin: 1em;
    text-align: justify;
    orphans: 2;
    widows: 2;
    word-wrap: break-word;
    hyphens: auto;
    -webkit-hyphens: auto;
}

/* Headings */
h1 { font-size: 1.8em; margin: 1em 0 0.5em; text-align: left; page-break-after: avoid; }
h2 { font-size: 1.4em; margin: 0.8em 0 0.4em; text-align: left; page-break-after: avoid; }
h3 { font-size: 1.2em; margin: 0.6em 0 0.3em; text-align: left; page-break-after: avoid; }
h4 { font-size: 1.1em; margin: 0.5em 0 0.25em; text-align: left; page-break-after: avoid; }
h5 { font-size: 1em; margin: 0.4em 0 0.2em; text-align: left; font-weight: bold; }
h6 { font-size: 0.9em; margin: 0.4em 0 0.2em; text-align: left; font-weight: bold; }

/* Paragraphs */
p {
    margin: 0.3em 0;
    text-align: inherit;
}

/* Links */
a { color: #1a5276; text-decoration: underline; }

/* Images */
img { max-width: 100%; height: auto; }

/* Tables */
table { border-collapse: collapse; width: 100%; margin: 0.5em 0; }
td, th { border: 1px solid #999; padding: 4px 8px; vertical-align: top; }

/* Footnotes */
aside[epub|type="footnote"] {
    font-size: 0.85em;
    margin-top: 1em;
    padding-top: 0.5em;
    border-top: 1px solid #ccc;
}

/* Superscript/subscript */
sup { font-size: 0.75em; vertical-align: super; }
sub { font-size: 0.75em; vertical-align: sub; }
"""

    # ─── Create EPUB ──────────────────────────────────────────────
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)

    # Add CSS
    style = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=default_css.encode("utf-8"),
    )
    book.add_item(style)

    # Add cover
    if cover_path and os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        ext = os.path.splitext(cover_path)[1].lower()
        ct_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
        cover_ct = ct_map.get(ext, "image/jpeg")
        book.set_cover("cover" + ext, cover_data)

    # Add images
    for fname, (ct, blob) in epub_images.items():
        img_item = epub.EpubItem(
            uid=fname.replace("/", "_").replace(".", "_"),
            file_name=fname,
            media_type=ct,
            content=blob,
        )
        book.add_item(img_item)

    # Build footnotes section
    footnote_html = ""
    if footnotes:
        footnote_html = '<section epub:type="endnotes">\n'
        footnote_html += "<h2>Примечания</h2>\n"
        for fid, text in sorted(footnotes.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            footnote_html += (
                f'<aside epub:type="footnote" id="fn{fid}">'
                f'<p><a href="#fnref{fid}">[{fid}]</a> {_escape_html(text)}</p>'
                f"</aside>\n"
            )
        footnote_html += "</section>\n"

    # Create chapter files
    epub_chapters = []
    spine = ["nav"]
    toc = []

    for idx, (ch_title, ch_html) in enumerate(chapters):
        # Add footnotes to last chapter
        if idx == len(chapters) - 1 and footnote_html:
            ch_html += footnote_html

        xhtml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
    <title>{_escape_html(ch_title)}</title>
    <link rel="stylesheet" type="text/css" href="style/default.css"/>
</head>
<body>
{ch_html}
</body>
</html>"""

        chapter = epub.EpubHtml(
            title=ch_title,
            file_name=f"chapter_{idx:04d}.xhtml",
            lang=language,
        )
        chapter.content = xhtml_content.encode("utf-8")
        chapter.add_item(style)
        book.add_item(chapter)
        epub_chapters.append(chapter)
        spine.append(chapter)
        toc.append(epub.Link(f"chapter_{idx:04d}.xhtml", ch_title, f"ch{idx}"))

    # Set TOC and spine
    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Write
    epub.write_epub(output_path, book, {})
    return output_path


# ─── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert DOCX to EPUB with full formatting preservation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python docx2epub.py book.docx
    python docx2epub.py book.docx -o output.epub --title "Моя книга" --author "Автор"
    python docx2epub.py book.docx --cover cover.jpg --lang ru
    python docx2epub.py book.docx --split none  # no chapter splitting
    python docx2epub.py book.docx --css custom.css
        """,
    )
    parser.add_argument("input", help="Input .docx file")
    parser.add_argument("-o", "--output", help="Output .epub file")
    parser.add_argument("--title", help="Book title")
    parser.add_argument("--author", help="Book author")
    parser.add_argument("--cover", help="Cover image path")
    parser.add_argument("--lang", default="ru", help="Language code (default: ru)")
    parser.add_argument(
        "--split",
        choices=["heading1", "pagebreak", "none"],
        default="heading1",
        help="Chapter split method (default: heading1)",
    )
    parser.add_argument("--css", help="Custom CSS file path")

    args = parser.parse_args()

    css_override = None
    if args.css:
        with open(args.css, "r", encoding="utf-8") as f:
            css_override = f.read()

    try:
        output = docx_to_epub(
            args.input,
            output_path=args.output,
            title=args.title,
            author=args.author,
            cover_path=args.cover,
            language=args.lang,
            chapter_split=args.split,
            css_override=css_override,
        )
        print(f"✅ Converted successfully: {output}")
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
