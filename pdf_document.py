"""fitz (PyMuPDF) sarmalayıcısı: PDF açma/kaydetme, undo, stil analizi ve düzenleme.

Tüm koordinatlar PDF noktası (point) cinsindendir; ekran-piksel dönüşümü
görünüm tarafında yapılır.

"Stil eşleştirme" mantığı: tıklanan/seçilen yerdeki span'ın font adı, boyutu ve
rengi okunur. Yazarken önce PDF'e gömülü orijinal font çıkarılıp yeniden
kullanılmaya çalışılır (subset fontta gereken harfler eksikse vazgeçilir),
olmazsa font adına göre Windows'taki en yakın TTF seçilir, o da olmazsa Arial'e
düşülür.
"""
from __future__ import annotations

import logging
import os
import difflib
import hashlib
import math
import statistics
import tempfile
from dataclasses import dataclass, field

import fitz
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage,
                           QPainter, QBrush)

from ocr_engine import OcrLine

log = logging.getLogger("pdfeditor")

WIN_FONT_DIR = r"C:\Windows\Fonts"

# base-14 fontlarda Türkçe karakter yok; varsayılan olarak Arial gömülür
_DEFAULT_FONTS = ["arial.ttf", "segoeui.ttf", "calibri.ttf"]

# font adı anahtar kelimesi -> (normal, kalın, italik, kalın-italik) dosyaları
_WIN_FONT_MAP: dict[str, tuple[str | None, ...]] = {
    "arial":       ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "helvetica":   ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "times":       ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "courier":     ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "calibri":     ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "verdana":     ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "tahoma":      ("tahoma.ttf", "tahomabd.ttf", None, None),
    "georgia":     ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "segoe":       ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "cambria":     ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf"),
    "consolas":    ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    "trebuchet":   ("trebuc.ttf", "trebucbd.ttf", "trebucit.ttf", "trebucbi.ttf"),
    "palatino":    ("pala.ttf", "palab.ttf", "palai.ttf", "palabi.ttf"),
    "book antiqua": ("bkant.ttf", None, None, None),
    "garamond":    ("garamond.ttf", None, "garamdit.ttf", None),
    "century":     ("gothic.ttf", "gothicb.ttf", "gothici.ttf", "gothicbi.ttf"),
    "franklin":    ("framd.ttf", None, "framdit.ttf", None),
    "impact":      ("impact.ttf", None, None, None),
    "lucida":      ("lucon.ttf", None, None, None),
    "comic":       ("comic.ttf", "comicbd.ttf", "comici.ttf", "comicz.ttf"),
    "candara":     ("candara.ttf", "candarab.ttf", "candarai.ttf", "candaraz.ttf"),
    "corbel":      ("corbel.ttf", "corbelb.ttf", "corbeli.ttf", "corbelz.ttf"),
    "constantia":  ("constan.ttf", "constanb.ttf", "constani.ttf", "constanz.ttf"),
    "aptos":       ("aptos.ttf", "aptos-bold.ttf", None, None),
    "sylfaen":     ("sylfaen.ttf", None, None, None),
}

_OCR_FONT_CANDIDATES = (
    ("Times New Roman", "times.ttf"),
    ("Arial", "arial.ttf"),
    ("Calibri", "calibri.ttf"),
    ("Georgia", "georgia.ttf"),
    ("Segoe UI", "segoeui.ttf"),
)

_QT_FONT_FAMILIES = (
    ("times", "Times New Roman"),
    ("arial", "Arial"),
    ("helvetica", "Arial"),
    ("calibri", "Calibri"),
    ("georgia", "Georgia"),
    ("segoe", "Segoe UI"),
    ("courier", "Courier New"),
    ("verdana", "Verdana"),
    ("tahoma", "Tahoma"),
    ("cambria", "Cambria"),
    ("consolas", "Consolas"),
    ("trebuchet", "Trebuchet MS"),
    ("palatino", "Palatino Linotype"),
    ("book antiqua", "Book Antiqua"),
    ("garamond", "Garamond"),
    ("century", "Century Gothic"),
    ("franklin", "Franklin Gothic Medium"),
    ("impact", "Impact"),
    ("lucida", "Lucida Console"),
    ("comic", "Comic Sans MS"),
    ("candara", "Candara"),
    ("corbel", "Corbel"),
    ("constantia", "Constantia"),
    ("aptos", "Aptos"),
    ("sylfaen", "Sylfaen"),
)

_QT_APP_FONT_CACHE: dict[str, str] = {}

MAX_UNDO = 20

FLAG_ITALIC = 2
FLAG_BOLD = 16


def _default_fontfile() -> str | None:
    for name in _DEFAULT_FONTS:
        path = os.path.join(WIN_FONT_DIR, name)
        if os.path.exists(path):
            return path
    return None


def _int_to_rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


def _normalize_extracted_text(text: str) -> str:
    """Bazı gömülü Windows fontlarının NBSP/soft-hyphen eşlemesini düzeltir."""
    return text.replace("\xa0", " ").replace("\xad", "-")


class TextStyle:
    """Bir span'dan okunan stil: font adı, boyut, renk, kalın/italik."""

    def __init__(self, fontname: str = "", size: float = 11.0,
                 color: tuple[float, float, float] = (0, 0, 0), flags: int = 0):
        self.fontname = fontname
        self.size = size
        self.color = color
        self.flags = flags

    @property
    def bold(self) -> bool:
        return bool(self.flags & FLAG_BOLD) or "bold" in self.fontname.lower()

    @property
    def italic(self) -> bool:
        return bool(self.flags & FLAG_ITALIC) or any(
            k in self.fontname.lower() for k in ("italic", "oblique"))

    def describe(self) -> str:
        name = self.fontname.split("+")[-1] or "?"
        extras = []
        if self.bold:
            extras.append("Kalın")
        if self.italic:
            extras.append("İtalik")
        suffix = (" " + " ".join(extras)) if extras else ""
        r, g, b = (int(v * 255) for v in self.color)
        return f"{name}{suffix}, {self.size:.1f} pt, renk #{r:02X}{g:02X}{b:02X}"


@dataclass
class TextWord:
    rect: fitz.Rect
    text: str
    confidence: float = 100.0


@dataclass
class TextGlyph:
    rect: fitz.Rect
    text: str


@dataclass
class TextHit:
    """Doğrudan sayfa üstünde düzenlenecek tek satırın bütün bilgileri."""

    rect: fitz.Rect
    text: str
    style: TextStyle
    words: list[TextWord] = field(default_factory=list)
    glyphs: list[TextGlyph] = field(default_factory=list)
    source: str = "native"
    confidence: float = 100.0
    baseline: float | None = None
    container_rect: fitz.Rect | None = None
    block_rect: fitz.Rect | None = None
    background: tuple[int, int, int] = (255, 255, 255)
    align: int = 0
    in_table_cell: bool = False

    @property
    def layout_rect(self) -> fitz.Rect:
        return fitz.Rect(self.container_rect or self.rect)

    def cursor_index(self, x: float) -> int:
        """PDF x koordinatını satır içindeki en yakın imleç konumuna çevirir."""
        if not self.text:
            return 0
        if not self.words:
            ratio = (x - self.rect.x0) / max(self.rect.width, 0.1)
            return max(0, min(len(self.text), round(ratio * len(self.text))))
        search_from = 0
        for word in self.words:
            index = self.text.find(word.text, search_from)
            if index < 0:
                index = search_from
            if x <= word.rect.x1:
                ratio = (x - word.rect.x0) / max(word.rect.width, 0.1)
                offset = round(max(0.0, min(1.0, ratio)) * len(word.text))
                return max(0, min(len(self.text), index + offset))
            search_from = index + len(word.text)
        return len(self.text)


@dataclass
class TextBlockHit:
    """Birlikte düzenlenecek, aynı paragraf/metin bloğundaki satırlar."""

    lines: list[TextHit]
    clicked_line: int = 0

    @property
    def text(self) -> str:
        # Görsel satır sonları normal boşluktur. Belirgin biçimde kısa biten ve
        # ardından yeniden tam genişlikte başlayan satır ise gerçek paragraf sonu.
        if not self.lines:
            return ""
        widest = max(line.rect.width for line in self.lines)
        parts: list[str] = []
        for index, line in enumerate(self.lines):
            value = line.text.strip()
            if not value:
                continue
            parts.append(value)
            if index + 1 < len(self.lines):
                next_line = self.lines[index + 1]
                paragraph_end = (line.rect.width < widest * .68
                                 and next_line.rect.width > widest * .72)
                parts.append("\n" if paragraph_end else " ")
        return "".join(parts).rstrip()

    @property
    def style(self) -> TextStyle:
        return self.lines[self.clicked_line].style

    @property
    def source(self) -> str:
        sources = {line.source for line in self.lines}
        return sources.pop() if len(sources) == 1 else "mixed"

    @property
    def background(self) -> tuple[int, int, int]:
        return self.lines[self.clicked_line].background

    @property
    def align(self) -> int:
        return self.lines[self.clicked_line].align if len(self.lines) == 1 else 0

    @property
    def rect(self) -> fitz.Rect:
        result = fitz.Rect(self.lines[0].rect)
        for line in self.lines[1:]:
            result.include_rect(line.rect)
        return result

    @property
    def layout_rect(self) -> fitz.Rect:
        result = fitz.Rect(self.lines[0].layout_rect)
        for line in self.lines[1:]:
            result.include_rect(line.layout_rect)
        return result

    def cursor_index(self, x: float) -> int:
        line_index = max(0, min(len(self.lines) - 1, self.clicked_line))
        prefix_text = TextBlockHit(self.lines[:line_index], 0).text \
            if line_index else ""
        offset = len(prefix_text) + (1 if prefix_text else 0)
        return min(len(self.text), offset + self.lines[line_index].cursor_index(x))


@dataclass
class _UndoState:
    pdf_bytes: bytes
    dirty: bool
    ocr_lines: dict[int, list[TextHit]]
    ocr_complete: set[int]


class PdfDocument:
    def __init__(self) -> None:
        self.doc: fitz.Document | None = None
        self.path: str | None = None
        self.dirty = False
        self._undo_stack: list[_UndoState] = []
        self._default_font = _default_fontfile()
        self._font_seq = 0
        self._font_tempdir: tempfile.TemporaryDirectory | None = None
        self._font_file_cache: dict[str, str] = {}
        self._ocr_lines: dict[int, list[TextHit]] = {}
        self._source_ocr_lines: dict[int, list[TextHit]] = {}
        self._ocr_complete: set[int] = set()
        self._font_metric_cache: dict[str, fitz.Font] = {}
        self._scan_image_cache: dict[int, tuple[int, fitz.Rect] | None] = {}
        self._scan_qimage_cache: dict[int, tuple[int, fitz.Rect, QImage]] = {}
        self._source_scan_qimage_cache: dict[
            int, tuple[int, fitz.Rect, QImage]] = {}
        self._table_cell_cache: dict[int, list[fitz.Rect]] = {}
        self._segmented_word_glyph_cache: dict[tuple, list[TextGlyph]] = {}
        self._last_scan_glyph_coverage = 0.0
        self._last_scan_changed_text = ""
        self._render_cache: dict[tuple[int, float], fitz.Pixmap] = {}

    # ---------- yaşam döngüsü ----------

    def open(self, path: str) -> None:
        self._cleanup_font_cache()
        with open(path, "rb") as f:
            data = f.read()
        # stream'den açmak dosyayı kilitlemez; aynı dosyanın üzerine kayıt yapılabilir
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.path = path
        self.dirty = False
        self._undo_stack.clear()
        self._clear_analysis_cache()

    def save(self, path: str | None = None) -> None:
        assert self.doc is not None
        target = path or self.path
        assert target is not None
        data = self.doc.tobytes(garbage=3, deflate=True)
        with open(target, "wb") as f:
            f.write(data)
        self.path = target
        self.dirty = False

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        self.path = None
        self.dirty = False
        self._undo_stack.clear()
        self._cleanup_font_cache()
        self._clear_analysis_cache()

    @property
    def page_count(self) -> int:
        return self.doc.page_count if self.doc else 0

    def page_size(self, page_no: int) -> tuple[float, float]:
        assert self.doc is not None
        r = self.doc[page_no].rect
        return r.width, r.height

    def snapshot_bytes(self) -> bytes:
        """Arka plandaki OCR işçisinin güvenle açabileceği bağımsız belge kopyası."""
        assert self.doc is not None
        return self.doc.tobytes()

    def _clear_analysis_cache(self) -> None:
        self._ocr_lines.clear()
        self._source_ocr_lines.clear()
        self._ocr_complete.clear()
        self._scan_image_cache.clear()
        self._scan_qimage_cache.clear()
        self._source_scan_qimage_cache.clear()
        self._table_cell_cache.clear()
        self._segmented_word_glyph_cache.clear()
        self._render_cache.clear()

    def _invalidate_page_analysis(self, page_no: int,
                                  affected: fitz.Rect | None = None) -> None:
        """Düzenleme sonrası eski OCR isabetlerini güvenle biçimde temizler."""
        if affected is None:
            self._ocr_lines.pop(page_no, None)
            self._ocr_complete.discard(page_no)
        elif page_no in self._ocr_lines:
            self._ocr_lines[page_no] = [
                hit for hit in self._ocr_lines[page_no]
                if (hit.rect & affected).is_empty
            ]
        self._scan_image_cache.pop(page_no, None)
        self._table_cell_cache.pop(page_no, None)
        self.invalidate_render_cache(page_no)

    def _full_page_scan_image(self, page_no: int
                              ) -> tuple[int, fitz.Rect] | None:
        """Sayfayı kaplayan ana tarama görselini bulur."""
        if page_no in self._scan_image_cache:
            return self._scan_image_cache[page_no]
        assert self.doc is not None
        page = self.doc[page_no]
        page_area = max(page.rect.get_area(), 1.0)
        best: tuple[int, fitz.Rect] | None = None
        best_area = 0.0
        for image in page.get_images(full=True):
            xref = int(image[0])
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                continue
            for rect_like in rects:
                rect = fitz.Rect(rect_like)
                area = (rect & page.rect).get_area()
                if area > best_area:
                    best_area = area
                    best = (xref, rect)
        if best_area < page_area * 0.72:
            best = None
        self._scan_image_cache[page_no] = best
        return best

    def page_needs_ocr(self, page_no: int) -> bool:
        """Tam sayfa taraması varsa yerleşik birkaç ek metne rağmen OCR gerekir."""
        assert self.doc is not None
        if self._full_page_scan_image(page_no) is not None:
            return True
        return not bool(self.doc[page_no].get_text("text").strip())

    def ocr_ready(self, page_no: int) -> bool:
        return page_no in self._ocr_complete

    @staticmethod
    def _clone_text_hit(hit: TextHit) -> TextHit:
        """OCR sahnesi ile degismez kaynak bankasi arasinda nesne paylasmaz."""
        style = TextStyle(hit.style.fontname, hit.style.size,
                          tuple(hit.style.color), hit.style.flags)
        return TextHit(
            rect=fitz.Rect(hit.rect), text=hit.text, style=style,
            words=[TextWord(fitz.Rect(word.rect), word.text, word.confidence)
                   for word in hit.words],
            glyphs=[TextGlyph(fitz.Rect(glyph.rect), glyph.text)
                    for glyph in hit.glyphs],
            source=hit.source, confidence=hit.confidence,
            baseline=hit.baseline,
            container_rect=(fitz.Rect(hit.container_rect)
                            if hit.container_rect is not None else None),
            block_rect=(fitz.Rect(hit.block_rect)
                        if hit.block_rect is not None else None),
            background=tuple(hit.background), align=hit.align)

    def set_ocr_lines(self, page_no: int, lines: list[OcrLine]) -> None:
        """Arka plan OCR sonucunu stil ve sayfa bağlamıyla düzenlenebilir yapar."""
        assert self.doc is not None
        page = self.doc[page_no]
        analysis_pixmap = page.get_pixmap(matrix=fitz.Matrix(2.25, 2.25),
                                          alpha=False, colorspace=fitz.csRGB)
        converted: list[TextHit] = []
        for line in lines:
            if not line.text.strip():
                continue
            words = [TextWord(fitz.Rect(word.rect), word.text, word.confidence)
                     for word in line.words]
            glyphs = [TextGlyph(fitz.Rect(glyph.rect), glyph.text)
                      for glyph in line.glyphs]
            style = self._infer_ocr_style(page_no, fitz.Rect(line.rect), words,
                                          analysis_pixmap)
            rect = fitz.Rect(line.rect)
            converted.append(TextHit(
                rect=rect,
                text=line.text,
                style=style,
                words=words,
                glyphs=glyphs,
                source="ocr",
                confidence=line.confidence,
                baseline=rect.y1 - max(.5, rect.height * .10),
                block_rect=fitz.Rect(line.block_rect),
                background=self._sample_background(page_no, rect,
                                                   analysis_pixmap),
            ))
        # Kısa tablo etiketlerinde birkaç harfin metriği tek başına yanıltıcı
        # olabilir. Belgenin uzun gövde satırlarından çıkan baskın aileyi kısa
        # satırlara uygula; boyut, kalınlık ve renk yine satırdan korunur.
        family_weights: dict[str, int] = {}
        for hit in converted:
            if len(hit.text) >= 45:
                family_weights[hit.style.fontname] = (
                    family_weights.get(hit.style.fontname, 0) + len(hit.text))
        if family_weights:
            dominant = max(family_weights, key=family_weights.get)
            dominant_weight = family_weights[dominant]
            if dominant_weight >= 120:
                for hit in converted:
                    if len(hit.text) <= 32:
                        hit.style.fontname = dominant
        if page_no not in self._source_ocr_lines:
            self._source_ocr_lines[page_no] = [
                self._clone_text_hit(hit) for hit in converted]
        self._ocr_lines[page_no] = converted
        self._ocr_complete.add(page_no)

    def ocr_line_count(self, page_no: int) -> int:
        return len(self._ocr_lines.get(page_no, ()))

    # ---------- undo ----------

    def _capture_undo_state(self) -> _UndoState:
        assert self.doc is not None
        return _UndoState(
            pdf_bytes=self.doc.tobytes(), dirty=self.dirty,
            ocr_lines={page_no: [self._clone_text_hit(hit) for hit in lines]
                       for page_no, lines in self._ocr_lines.items()},
            ocr_complete=set(self._ocr_complete))

    def _snapshot(self) -> None:
        assert self.doc is not None
        self._undo_stack.append(self._capture_undo_state())
        if len(self._undo_stack) > MAX_UNDO:
            self._undo_stack.pop(0)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def undo(self) -> None:
        if not self._undo_stack:
            return
        state = self._undo_stack.pop()
        self._restore_undo_state(state)

    def _restore_undo_state(self, state: _UndoState) -> None:
        assert self.doc is not None
        self.doc.close()
        self.doc = fitz.open(stream=state.pdf_bytes, filetype="pdf")
        self.dirty = state.dirty
        self._ocr_lines = {
            page_no: [self._clone_text_hit(hit) for hit in lines]
            for page_no, lines in state.ocr_lines.items()
        }
        self._ocr_complete = set(state.ocr_complete)
        self._scan_image_cache.clear()
        self._scan_qimage_cache.clear()
        self._table_cell_cache.clear()
        self._segmented_word_glyph_cache.clear()
        self._render_cache.clear()

    # ---------- render / okuma ----------

    def render_page(self, page_no: int, zoom: float) -> fitz.Pixmap:
        assert self.doc is not None
        key = (page_no, round(zoom, 6))
        cached = self._render_cache.get(key)
        if cached is not None:
            return cached
        page = self.doc[page_no]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        if len(self._render_cache) >= 64:
            self._render_cache.clear()
        self._render_cache[key] = pix
        return pix

    def invalidate_render_cache(self, page_no: int | None = None) -> None:
        if page_no is None:
            self._render_cache.clear()
            return
        self._render_cache = {
            key: pix for key, pix in self._render_cache.items()
            if key[0] != page_no
        }

    def _iter_spans(self, page_no: int):
        assert self.doc is not None
        info = self.doc[page_no].get_text("dict")
        for block in info.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    yield span

    def text_in_rect(self, page_no: int, rect: fitz.Rect) -> str:
        """Dikdörtgen içindeki metni döndürür."""
        assert self.doc is not None
        info = self.doc[page_no].get_text("dict", clip=rect)
        parts: list[str] = []
        for block in info.get("blocks", []):
            for line in block.get("lines", []):
                line_text = _normalize_extracted_text(
                    "".join(s["text"] for s in line.get("spans", [])))
                if line_text.strip():
                    parts.append(line_text)
        if not parts and page_no in self._ocr_lines:
            parts.extend(hit.text for hit in self._ocr_lines[page_no]
                         if not (hit.rect & rect).is_empty)
        return "\n".join(parts)

    # ---------- stil analizi ----------

    def line_at_point(self, page_no: int, point: fitz.Point
                      ) -> TextHit | None:
        """Tıklanan kelimeyi değil, ait olduğu satırın tamamını döndürür."""
        native = self._native_line_at_point(page_no, point)
        if native is not None:
            return self._decorate_hit(page_no, point, native)

        best: TextHit | None = None
        best_dist = float("inf")
        for hit in self._ocr_lines.get(page_no, ()):
            pad_y = max(3.0, hit.rect.height * .42)
            padded = fitz.Rect(hit.rect.x0 - 3, hit.rect.y0 - pad_y,
                               hit.rect.x1 + 3, hit.rect.y1 + pad_y)
            if not padded.contains(point):
                continue
            dy = abs((hit.rect.y0 + hit.rect.y1) / 2 - point.y)
            if dy < best_dist:
                best, best_dist = hit, dy
        if best is not None:
            return self._decorate_hit(page_no, point, best)
        return None

    @staticmethod
    def _same_text_block(first: TextHit, second: TextHit) -> bool:
        """OCR'ın geniş bloklarını stil ve satır geometrisiyle güvenle daraltır."""
        if first.block_rect is None or second.block_rect is None:
            return False
        if max(abs(a - b) for a, b in zip(first.block_rect, second.block_rect)) > 1.5:
            return False
        height_ratio = second.rect.height / max(first.rect.height, .1)
        if not .72 <= height_ratio <= 1.34:
            return False
        # Tarama gürültüsü aynı Times gövde satırını bazen Georgia/Arial diye
        # sınıflandırabilir. Blok birliği için aile adına değil, gerçek satır
        # yüksekliği ile kalın/italik yapısına güvenilir.
        if first.style.bold != second.style.bold or first.style.italic != second.style.italic:
            return False
        overlap = max(0.0, min(first.rect.x1, second.rect.x1)
                      - max(first.rect.x0, second.rect.x0))
        min_width = max(.1, min(first.rect.width, second.rect.width))
        starts_together = abs(first.rect.x0 - second.rect.x0) <= max(
            14.0, first.rect.height * 1.7)
        return overlap >= min_width * .48 or starts_together

    def _native_block_lines(self, page_no: int, block_rect: fitz.Rect,
                            clicked_y: float) -> tuple[list[TextHit], int]:
        """Vektör PDF'teki aynı metin bloğunun bütün doğal satırlarını çıkarır."""
        assert self.doc is not None
        info = self.doc[page_no].get_text("dict")
        result: list[TextHit] = []
        for block in info.get("blocks", []):
            if block.get("type") != 0:
                continue
            bb_block = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
            if max(abs(a - b) for a, b in zip(bb_block, block_rect)) > 1.5:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = _normalize_extracted_text(
                    "".join(span.get("text", "") for span in spans))
                if not text.strip():
                    continue
                rect = fitz.Rect(line["bbox"])
                chosen = spans[0]
                baseline = None
                origin = chosen.get("origin")
                if origin and len(origin) > 1:
                    baseline = float(origin[1])
                hit = TextHit(
                    rect=rect, text=text, style=self._span_style(chosen),
                    words=self._words_for_line(page_no, rect, spans), source="native",
                    baseline=baseline, block_rect=fitz.Rect(bb_block),
                    background=self._sample_background(page_no, rect),
                )
                center = fitz.Point((rect.x0 + rect.x1) / 2,
                                    (rect.y0 + rect.y1) / 2)
                result.append(self._decorate_hit(page_no, center, hit))
            break
        result.sort(key=lambda item: (item.rect.y0, item.rect.x0))
        clicked = min(range(len(result)),
                      key=lambda index: abs((result[index].rect.y0
                                             + result[index].rect.y1) / 2
                                            - clicked_y)) if result else 0
        return result, clicked

    def text_block_at_point(self, page_no: int,
                            point: fitz.Point) -> TextBlockHit | None:
        """Tıklanan satırın ait olduğu paragrafı tek bir düzenleme akışı yapar."""
        anchor = self.line_at_point(page_no, point)
        if anchor is None:
            return None
        # Tablo hücreleri paragraf değildir; komşu satır/hücrelerle birleşmez.
        if self.cell_at_point(page_no, point) is not None:
            return TextBlockHit([anchor], 0)

        if anchor.source == "native" and anchor.block_rect is not None:
            lines, clicked = self._native_block_lines(
                page_no, anchor.block_rect, point.y)
            if lines:
                return TextBlockHit(lines, clicked)

        if anchor.source != "ocr" or anchor.block_rect is None:
            return TextBlockHit([anchor], 0)

        candidates = [line for line in self._ocr_lines.get(page_no, ())
                      if self._same_text_block(anchor, line)]
        if not candidates:
            return TextBlockHit([anchor], 0)
        candidates.sort(key=lambda item: (item.rect.y0, item.rect.x0))
        anchor_index = min(
            range(len(candidates)),
            key=lambda index: abs((candidates[index].rect.y0
                                   + candidates[index].rect.y1) / 2 - point.y))

        # Aynı Tesseract bloğunda yan yana tablo sütunları da bulunabilir. Yalnız
        # tıklanan satırla dikey olarak kesintisiz devam eden komşuları al.
        start = end = anchor_index
        while start > 0:
            previous, current = candidates[start - 1], candidates[start]
            gap = current.rect.y0 - previous.rect.y1
            if gap > max(6.0, (previous.rect.height + current.rect.height) * .48):
                break
            start -= 1
        while end + 1 < len(candidates):
            current, following = candidates[end], candidates[end + 1]
            gap = following.rect.y0 - current.rect.y1
            if gap > max(6.0, (current.rect.height + following.rect.height) * .48):
                break
            end += 1

        lines: list[TextHit] = []
        for line in candidates[start:end + 1]:
            center = fitz.Point((line.rect.x0 + line.rect.x1) / 2,
                                (line.rect.y0 + line.rect.y1) / 2)
            lines.append(self._decorate_hit(page_no, center, line))
        return TextBlockHit(lines, anchor_index - start)

    def _native_line_at_point(self, page_no: int,
                              point: fitz.Point) -> TextHit | None:
        assert self.doc is not None
        info = self.doc[page_no].get_text("dict")
        for block in info.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                bb = fitz.Rect(line["bbox"])
                pad_y = max(2.5, bb.height * .32)
                pad = fitz.Rect(bb.x0 - 3, bb.y0 - pad_y,
                                bb.x1 + 3, bb.y1 + pad_y)
                if not pad.contains(point):
                    continue
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = _normalize_extracted_text(
                    "".join(s["text"] for s in spans))
                # stil, tıklanan x konumundaki span'dan alınır
                chosen = spans[0]
                for s in spans:
                    sb = fitz.Rect(s["bbox"])
                    if sb.x0 - 1 <= point.x <= sb.x1 + 1:
                        chosen = s
                        break
                baseline = None
                origin = chosen.get("origin")
                if origin and len(origin) > 1:
                    baseline = float(origin[1])
                words = self._words_for_line(page_no, bb, spans)
                return TextHit(
                    rect=bb,
                    text=text,
                    style=self._span_style(chosen),
                    words=words,
                    source="native",
                    baseline=baseline,
                    block_rect=fitz.Rect(block.get("bbox", bb)),
                    background=self._sample_background(page_no, bb),
                )
        return None

    def _words_for_line(self, page_no: int, rect: fitz.Rect,
                        spans: list[dict]) -> list[TextWord]:
        """Satırın kendi span'larından saf ve komşu satırlara taşmayan kelimeleri üretir."""
        return self._words_from_spans(spans)

    @staticmethod
    def _words_from_spans(spans: list[dict]) -> list[TextWord]:
        words: list[TextWord] = []
        for span in spans:
            text = _normalize_extracted_text(span.get("text", ""))
            rect = fitz.Rect(span.get("bbox", (0, 0, 0, 0)))
            if not text:
                continue
            tokens = text.split()
            if not tokens:
                continue

            def _weight(t: str) -> float:
                return sum(1.5 if c in "WMwm%@#" else (0.4 if c in "ijl|!.,:;'`I" else 1.0)
                           for c in t)

            total_weight = max(sum(_weight(token) for token in tokens), 0.1)
            cursor = rect.x0
            gap = rect.width * .18 / max(len(tokens) - 1, 1) if len(tokens) > 1 else 0
            usable = max(0.0, rect.width - gap * (len(tokens) - 1))
            for token in tokens:
                width = usable * _weight(token) / total_weight
                words.append(TextWord(fitz.Rect(cursor, rect.y0,
                                                cursor + width, rect.y1), token))
                cursor += width + gap
        return words

    def _decorate_hit(self, page_no: int, point: fitz.Point,
        hit: TextHit) -> TextHit:
        cell = self.cell_at_point(page_no, point)
        if cell is not None:
            # Satır bbox'ı hücre çizgisine birkaç piksel taşabildiğinden merkez
            # kontrolü kullanılır.
            center = fitz.Point((hit.rect.x0 + hit.rect.x1) / 2,
                                (hit.rect.y0 + hit.rect.y1) / 2)
            if not cell.contains(center):
                cell = None
        if cell is not None:
            hit.in_table_cell = True
            inset = min(2.2, cell.width * .03, cell.height * .12)
            hit.container_rect = fitz.Rect(cell.x0 + inset, cell.y0 + inset,
                                           cell.x1 - inset, cell.y1 - inset)
            left_gap = hit.rect.x0 - hit.container_rect.x0
            right_gap = hit.container_rect.x1 - hit.rect.x1
            if left_gap > 2 and right_gap > 2 and abs(left_gap - right_gap) < max(
                    4, hit.container_rect.width * .13):
                hit.align = 1
            elif right_gap < max(2, left_gap * .28):
                hit.align = 2
            else:
                hit.align = 0
        else:
            hit.in_table_cell = False
            hit.container_rect = fitz.Rect(hit.rect.x0, hit.rect.y0 - .6,
                                           hit.rect.x1, hit.rect.y1 + .8)
            block = hit.block_rect
            if block is not None and block.width > hit.rect.width * 1.12:
                left_gap = hit.rect.x0 - block.x0
                right_gap = block.x1 - hit.rect.x1
                if abs(left_gap - right_gap) < max(5, block.width * .08):
                    hit.container_rect = fitz.Rect(block.x0, hit.rect.y0 - .6,
                                                   block.x1, hit.rect.y1 + .8)
                    hit.align = 1
        return hit

    def blank_cell_at_point(self, page_no: int,
                            point: fitz.Point) -> TextHit | None:
        """Boş tablo hücresine tıklanınca da doğrudan yazım alanı oluşturur."""
        cell = self.cell_at_point(page_no, point)
        neighbor: TextHit | None = None
        if cell is None and self._full_page_scan_image(page_no) is not None:
            candidates = []
            for hit in self._ocr_lines.get(page_no, ()):
                center_y = (hit.rect.y0 + hit.rect.y1) / 2
                vertical = abs(center_y - point.y)
                horizontal = point.x - hit.rect.x1
                if 0 < horizontal < 125 and vertical < max(8, hit.rect.height * 1.1):
                    candidates.append((vertical * 5 + horizontal, hit))
            if candidates:
                neighbor = min(candidates, key=lambda item: item[0])[1]
                cell = self._blank_scan_cell_from_neighbor(
                    page_no, point, neighbor)
        if cell is None:
            return None
        style = neighbor.style if neighbor is not None else self.style_at_point(
            page_no, point)
        inset = min(2.2, cell.width * .03, cell.height * .12)
        container = fitz.Rect(cell.x0 + inset, cell.y0 + inset,
                              cell.x1 - inset, cell.y1 - inset)
        return TextHit(rect=fitz.Rect(point.x, point.y, point.x, point.y),
                       text="", style=style, source="blank-cell",
                       baseline=None, container_rect=container,
                       background=self._sample_background(page_no, container))

    def style_at_point(self, page_no: int, point: fitz.Point) -> TextStyle:
        """Tıklanan noktaya en yakın span'ın stilini döndürür (satır önceliğiyle)."""
        best, best_dist = None, float("inf")
        for span in self._iter_spans(page_no):
            bb = fitz.Rect(span["bbox"])
            dx = max(bb.x0 - point.x, point.x - bb.x1, 0.0)
            dy = max(bb.y0 - point.y, point.y - bb.y1, 0.0)
            # dikey uzaklığı ağır cezalandır ki aynı satırdaki span kazansın
            dist = dx * dx + dy * dy * 16
            if dist < best_dist:
                best_dist, best = dist, span
        if best is not None and best_dist < 36:
            return self._span_style(best)
        ocr_best: TextHit | None = None
        ocr_dist = float("inf")
        for hit in self._ocr_lines.get(page_no, ()):
            bb = hit.rect
            dx = max(bb.x0 - point.x, point.x - bb.x1, 0.0)
            dy = max(bb.y0 - point.y, point.y - bb.y1, 0.0)
            dist = dx * dx + dy * dy * 16
            if dist < ocr_dist:
                ocr_dist, ocr_best = dist, hit
        return ocr_best.style if ocr_best is not None else self._span_style(best)

    def style_in_rect(self, page_no: int, rect: fitz.Rect) -> TextStyle:
        """Seçilen alanla en çok kesişen span'ın stilini döndürür."""
        best, best_area = None, 0.0
        for span in self._iter_spans(page_no):
            inter = fitz.Rect(span["bbox"]) & rect
            area = inter.get_area() if not inter.is_empty else 0.0
            if area > best_area:
                best_area, best = area, span
        if best is not None:
            return self._span_style(best)
        ocr_best, ocr_area = None, 0.0
        for hit in self._ocr_lines.get(page_no, ()):
            inter = hit.rect & rect
            area = inter.get_area() if not inter.is_empty else 0.0
            if area > ocr_area:
                ocr_best, ocr_area = hit, area
        return ocr_best.style if ocr_best is not None else TextStyle()

    def _span_style(self, span: dict | None) -> TextStyle:
        if span is None:
            return TextStyle()
        return TextStyle(fontname=span.get("font", ""),
                         size=float(span.get("size", 11.0)),
                         color=_int_to_rgb(int(span.get("color", 0))),
                         flags=int(span.get("flags", 0)))

    def _sample_background(self, page_no: int, rect: fitz.Rect,
                           source_pixmap: fitz.Pixmap | None = None
                           ) -> tuple[int, int, int]:
        """Metnin çevresindeki kâğıt rengini bulur; editör kutusu görünmez olur."""
        assert self.doc is not None
        page = self.doc[page_no]
        margin = max(2.5, min(7.0, rect.height * .65))
        clip = fitz.Rect(rect.x0 - margin, rect.y0 - margin,
                         rect.x1 + margin, rect.y1 + margin) & page.rect
        if clip.is_empty:
            return 255, 255, 255
        if source_pixmap is None:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip,
                                  alpha=False, colorspace=fitz.csRGB)
            offset_x = offset_y = 0
            scale_x = pix.width / max(clip.width, .1)
            scale_y = pix.height / max(clip.height, .1)
            x_start, y_start, x_end, y_end = 0, 0, pix.width, pix.height
        else:
            pix = source_pixmap
            scale_x = pix.width / max(page.rect.width, .1)
            scale_y = pix.height / max(page.rect.height, .1)
            offset_x, offset_y = page.rect.x0, page.rect.y0
            x_start = max(0, math.floor((clip.x0 - offset_x) * scale_x))
            y_start = max(0, math.floor((clip.y0 - offset_y) * scale_y))
            x_end = min(pix.width, math.ceil((clip.x1 - offset_x) * scale_x))
            y_end = min(pix.height, math.ceil((clip.y1 - offset_y) * scale_y))
        data = pix.samples
        channels = pix.n
        region_width = max(1, x_end - x_start)
        region_height = max(1, y_end - y_start)
        edge = max(2, round(min(region_width, region_height) * .16))
        red: list[int] = []
        green: list[int] = []
        blue: list[int] = []
        step = max(1, round(max(region_width, region_height) / 240))
        for y in range(y_start, y_end, step):
            for x in range(x_start, x_end, step):
                if not (x < x_start + edge or x >= x_end - edge
                        or y < y_start + edge or y >= y_end - edge):
                    continue
                index = (y * pix.width + x) * channels
                r, g, b = data[index], data[index + 1], data[index + 2]
                if r * .299 + g * .587 + b * .114 >= 155:
                    red.append(r); green.append(g); blue.append(b)
        if not red:
            return 255, 255, 255
        return (round(statistics.median(red)),
                round(statistics.median(green)),
                round(statistics.median(blue)))

    def _infer_ocr_style(self, page_no: int, rect: fitz.Rect,
                         words: list[TextWord],
                         source_pixmap: fitz.Pixmap | None = None) -> TextStyle:
        """OCR geometrisi ve tarama piksellerinden font ailesi/boyut/renk çıkarır."""
        assert self.doc is not None
        page = self.doc[page_no]
        if source_pixmap is None:
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect,
                                  alpha=False, colorspace=fitz.csRGB)
            x0, y0, x1, y1 = 0, 0, pix.width, pix.height
        else:
            pix = source_pixmap
            scale_x = pix.width / max(page.rect.width, .1)
            scale_y = pix.height / max(page.rect.height, .1)
            x0 = max(0, math.floor((rect.x0 - page.rect.x0) * scale_x))
            y0 = max(0, math.floor((rect.y0 - page.rect.y0) * scale_y))
            x1 = min(pix.width, math.ceil((rect.x1 - page.rect.x0) * scale_x))
            y1 = min(pix.height, math.ceil((rect.y1 - page.rect.y0) * scale_y))
        data = pix.samples
        channels = pix.n
        dark_colors: list[tuple[int, int, int]] = []
        dark_count = 0
        for y in range(y0, y1):
            row = y * pix.width * channels
            for x in range(x0, x1):
                index = row + x * channels
                r, g, b = data[index], data[index + 1], data[index + 2]
                luminance = r * .299 + g * .587 + b * .114
                if luminance < 150:
                    dark_count += 1
                    dark_colors.append((r, g, b))
        density = dark_count / max((x1 - x0) * (y1 - y0), 1)
        if dark_colors:
            color = tuple(statistics.median(values) / 255 for values in zip(
                *dark_colors))
        else:
            color = (0.0, 0.0, 0.0)

        target_size = max(5.0, rect.height * .94)
        best_family = "Times New Roman"
        best_size = target_size
        best_score = float("inf")
        usable_words = [word for word in words
                        if len(word.text) >= 2 and word.rect.width > 1]
        for family, filename in _OCR_FONT_CANDIDATES:
            path = os.path.join(WIN_FONT_DIR, filename)
            if not os.path.isfile(path):
                continue
            try:
                font = self._font_metric_cache.get(path)
                if font is None:
                    font = fitz.Font(fontfile=path)
                    self._font_metric_cache[path] = font
                sizes = []
                for word in usable_words:
                    unit_width = font.text_length(word.text, fontsize=1)
                    if unit_width > .01:
                        sizes.append(word.rect.width / unit_width)
                if not sizes:
                    continue
                median_size = statistics.median(sizes)
                spread = (statistics.median(
                    [abs(value - median_size) for value in sizes])
                    / max(median_size, .1))
                # Tarafsız font seçimi: punto yakınlığı ve kelime genişliklerinin tutarlılığı (düşük varyans)
                score = abs(median_size - target_size) / target_size + spread * .35
                if score < best_score:
                    best_score = score
                    best_family = family
                    best_size = median_size
            except Exception:
                continue

        best_size = max(rect.height * .68,
                        min(rect.height * 1.18, best_size))
        # Sıkı OCR kutusunda kalın yazı belirgin biçimde daha çok koyu piksel taşır.
        flags = FLAG_BOLD if density > .235 else 0
        return TextStyle(fontname=best_family, size=max(4.0, best_size),
                         color=color, flags=flags)

    @staticmethod
    def _dark_pixel(data: bytes, width: int, channels: int,
                    x: int, y: int, threshold: int = 202) -> bool:
        index = (y * width + x) * channels
        r, g, b = data[index], data[index + 1], data[index + 2]
        return r * .299 + g * .587 + b * .114 < threshold

    @staticmethod
    def _projection_clusters(values: list[tuple[int, float]],
                             threshold: float) -> list[float]:
        clusters: list[list[int]] = []
        for position, score in values:
            if score < threshold:
                continue
            if not clusters or position - clusters[-1][-1] > 2:
                clusters.append([])
            clusters[-1].append(position)
        return [sum(cluster) / len(cluster) for cluster in clusters if cluster]

    def _vector_table_cells(self, page_no: int) -> list[fitz.Rect]:
        if page_no in self._table_cell_cache:
            return self._table_cell_cache[page_no]
        assert self.doc is not None
        cells: list[fitz.Rect] = []
        page = self.doc[page_no]
        try:
            finder = page.find_tables()
            for table in finder.tables:
                for cell in table.cells:
                    if cell is not None:
                        rect = fitz.Rect(cell)
                        if rect.width > 6 and rect.height > 5:
                            cells.append(rect)
        except Exception:
            pass
        self._table_cell_cache[page_no] = cells
        return cells

    def _raster_cell_at_point(self, page_no: int,
                              point: fitz.Point) -> fitz.Rect | None:
        """Taranmış tablodaki en yakın dört kesintisiz çizgiyi hücre sayar."""
        assert self.doc is not None
        page = self.doc[page_no]
        # Geniş kolonlar ve çok satırlı hücreler için genişletilmiş arama alanı
        search = fitz.Rect(point.x - 360, point.y - 120,
                           point.x + 360, point.y + 120) & page.rect
        if search.width < 20 or search.height < 12:
            return None
        scale = 2.5
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=search,
                              alpha=False, colorspace=fitz.csRGB)
        data, channels = pix.samples, pix.n
        local_x = int((point.x - search.x0) * scale)
        local_y = int((point.y - search.y0) * scale)
        y_radius = max(35, int(28 * scale))
        y0, y1 = max(0, local_y - y_radius), min(pix.height - 1,
                                                  local_y + y_radius)
        vertical_scores = []
        for x in range(1, pix.width - 1):
            dark = 0
            count = 0
            for y in range(y0, y1 + 1, 2):
                count += 1
                if any(self._dark_pixel(data, pix.width, channels, xx, y)
                       for xx in (x - 1, x, x + 1)):
                    dark += 1
            vertical_scores.append((x, dark / max(count, 1)))
        verticals = self._projection_clusters(vertical_scores, .58)
        lefts = [x for x in verticals if x < local_x - 4]
        rights = [x for x in verticals if x > local_x + 4]
        if not lefts or not rights:
            return None
        left, right = max(lefts), min(rights)
        if right - left < 10 * scale or right - left > 520 * scale:
            return None

        horizontal_scores = []
        x0 = max(0, round(left)); x1 = min(pix.width - 1, round(right))
        for y in range(1, pix.height - 1):
            dark = 0
            count = 0
            for x in range(x0, x1 + 1, 2):
                count += 1
                if any(self._dark_pixel(data, pix.width, channels, x, yy)
                       for yy in (y - 1, y, y + 1)):
                    dark += 1
            horizontal_scores.append((y, dark / max(count, 1)))
        horizontals = self._projection_clusters(horizontal_scores, .48)
        tops = [y for y in horizontals if y < local_y - 3]
        bottoms = [y for y in horizontals if y > local_y + 3]
        if not tops or not bottoms:
            return None
        top, bottom = max(tops), min(bottoms)
        if bottom - top < 6 * scale or bottom - top > 240 * scale:
            return None
        return fitz.Rect(search.x0 + left / scale,
                         search.y0 + top / scale,
                         search.x0 + right / scale,
                         search.y0 + bottom / scale)

    def _blank_scan_cell_from_neighbor(self, page_no: int, point: fitz.Point,
                                       neighbor: TextHit) -> fitz.Rect | None:
        """Solundaki OCR etiketi ve sağdaki çizgiden boş değer hücresini çıkarır."""
        assert self.doc is not None
        page = self.doc[page_no]
        if point.x <= neighbor.rect.x1 or point.x - neighbor.rect.x1 > 350:
            return None
        clip = fitz.Rect(neighbor.rect.x1, point.y - 60,
                         min(page.rect.x1, point.x + 240), point.y + 60) & page.rect
        if clip.width < 12 or clip.height < 12:
            return None
        scale = 3.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                              alpha=False, colorspace=fitz.csRGB)
        data, channels = pix.samples, pix.n
        local_point_x = int((point.x - clip.x0) * scale)
        vertical_scores = []
        for x in range(1, pix.width - 1):
            dark = 0
            count = 0
            for y in range(0, pix.height, 2):
                count += 1
                if any(self._dark_pixel(data, pix.width, channels, xx, y, 224)
                       for xx in (x - 1, x, x + 1)):
                    dark += 1
            vertical_scores.append((x, dark / max(count, 1)))
        verticals = self._projection_clusters(vertical_scores, .48)
        right_candidates = [x for x in verticals if x > local_point_x + 3]
        if not right_candidates:
            return None
        right_px = min(right_candidates)
        right = clip.x0 + right_px / scale
        left = neighbor.rect.x1 + max(2.5, neighbor.rect.height * .3)
        if right - left < 12 or not left < point.x < right:
            return None

        x0 = max(0, round((left - clip.x0) * scale))
        x1 = min(pix.width - 1, round(right_px))
        horizontal_scores = []
        for y in range(1, pix.height - 1):
            dark = 0
            count = 0
            for x in range(x0, x1 + 1, 2):
                count += 1
                if any(self._dark_pixel(data, pix.width, channels, x, yy, 230)
                       for yy in (y - 1, y, y + 1)):
                    dark += 1
            horizontal_scores.append((y, dark / max(count, 1)))
        horizontals = self._projection_clusters(horizontal_scores, .36)
        local_y = (point.y - clip.y0) * scale
        tops = [y for y in horizontals if y < local_y - 3]
        bottoms = [y for y in horizontals if y > local_y + 3]
        if tops and bottoms:
            top_px = max(tops)
            bottom_px = min(bottoms)
            top = clip.y0 + top_px / scale
            bottom = clip.y0 + bottom_px / scale

            # Değer hücrelerinde sol dikey çizgi olmayabiliyor; yatay satırın
            # başladığı x, gerçek hücre başlangıcıdır.
            starts = []
            for boundary_y in (round(top_px), round(bottom_px)):
                for x in range(0, x1):
                    run = 0
                    for xx in range(x, min(x + 8, x1 + 1)):
                        if any(self._dark_pixel(data, pix.width, channels, xx, yy, 232)
                                for yy in range(max(0, boundary_y - 1),
                                                min(pix.height, boundary_y + 2))):
                            run += 1
                    if run >= 5:
                        starts.append(clip.x0 + x / scale)
                        break
            if starts:
                left = max(left, max(starts) + 1.0)
        else:
            top = neighbor.rect.y0 - max(2.5, neighbor.rect.height * .55)
            bottom = neighbor.rect.y1 + max(3.5, neighbor.rect.height * .65)
        if bottom - top < 6 or bottom - top > 180:
            return None
        if not left < point.x < right:
            return None
        return fitz.Rect(left, top, right, bottom)

    def cell_at_point(self, page_no: int,
                      point: fitz.Point) -> fitz.Rect | None:
        if self._full_page_scan_image(page_no) is not None:
            return self._raster_cell_at_point(page_no, point)
        for cell in self._vector_table_cells(page_no):
            if cell.contains(point):
                return fitz.Rect(cell)
        return None

    # ---------- font çözümleme ----------

    def _embedded_font_buffer(self, page_no: int, fontname: str
                              ) -> tuple[bytes, str] | None:
        """Span'ın kullandığı gömülü fontu PDF'ten çıkarır (ttf/otf ise)."""
        assert self.doc is not None
        target = fontname.split("+")[-1].lower()
        if not target:
            return None
        for entry in self.doc.get_page_fonts(page_no, full=True):
            xref, basefont = entry[0], entry[3]
            if basefont.split("+")[-1].lower() != target:
                continue
            try:
                _, ext, _, buffer = self.doc.extract_font(xref)
            except Exception:
                continue
            if buffer and ext in ("ttf", "otf"):
                return bytes(buffer), ext
        return None

    def _cleanup_font_cache(self) -> None:
        """Bu belge icin diske yazilan gecici gomulu fontlari temizler."""
        if self._font_tempdir is not None:
            self._font_tempdir.cleanup()
            self._font_tempdir = None
        self._font_file_cache.clear()

    def _font_file_from_buffer(self, buffer: bytes, extension: str) -> str:
        """Gomulu fontu ``insert_textbox`` icin gecici dosyaya yazar.

        PyMuPDF'nin textbox API'si fontbuffer kabul etmez. Yalnizca belgeye
        kaydedilmis bir font takma adi kullanmak da redaction sayfa kaynaklarini
        yeniden yazdiginda ``need font file or buffer`` hatasina yol acar.
        """
        digest = hashlib.sha256(buffer).hexdigest()
        cached = self._font_file_cache.get(digest)
        if cached and os.path.exists(cached):
            return cached
        if self._font_tempdir is None:
            self._font_tempdir = tempfile.TemporaryDirectory(
                prefix="pdf-editor-fonts-")
        safe_ext = extension.lower() if extension.lower() in ("ttf", "otf") else "ttf"
        path = os.path.join(self._font_tempdir.name, f"{digest}.{safe_ext}")
        with open(path, "wb") as font_file:
            font_file.write(buffer)
        self._font_file_cache[digest] = path
        return path

    def _windows_fontfile(self, style: TextStyle) -> str | None:
        clean = style.fontname.split("+")[-1].lower()
        variant = (1 if style.bold else 0) + (2 if style.italic else 0)
        for key, files in _WIN_FONT_MAP.items():
            if key in clean:
                for idx in (variant, 0):  # varyant dosyası yoksa normale düş
                    name = files[idx]
                    if name:
                        path = os.path.join(WIN_FONT_DIR, name)
                        if os.path.exists(path):
                            return path
        return None

    def _font_kwargs_for(self, page: fitz.Page, page_no: int,
                         style: TextStyle | None, text: str) -> dict:
        """Yazım için font argümanlarını hazırlar; gerekirse fontu sayfaya gömer.

        Adobe benzeri davranış: önce PDF'teki özgün gömülü fontu kullanmaya çalışır.
        Subset fontta eksik karakter varsa hemen vazgeçmez; gömülü fontu önce
        genişleterek kullanmayı dener; başarısız olursa Windows'taki aynı aile
        fontunu alır; en son varsayıla düşer.
        """
        if style is not None:
            path = self._windows_fontfile(style)
            if path:
                self._font_seq += 1
                return {"fontname": f"WF{self._font_seq}", "fontfile": path}
            embedded = self._embedded_font_buffer(page_no, style.fontname)
            if embedded:
                buf, ext = embedded
                try:
                    font = fitz.Font(fontbuffer=buf)
                    chars = {c for c in text if not c.isspace()}
                    missing = [c for c in chars if not font.has_glyph(ord(c))]
                    if not missing:
                        self._font_seq += 1
                        name = f"EF{self._font_seq}"
                        path = self._font_file_from_buffer(buf, ext)
                        return {"fontname": name, "fontfile": path}
                    log.info(
                        "font %s subset eksik (%s harf); sistem/varsayılan font deneniyor",
                        style.fontname, "".join(missing))
                except Exception:
                    pass
            if self._default_font and (style.bold or style.italic):
                fake = TextStyle(style.fontname, flags=style.flags)
                path = self._windows_fontfile(fake)
                if path:
                    self._font_seq += 1
                    return {"fontname": f"WF{self._font_seq}", "fontfile": path}
        if self._default_font:
            self._font_seq += 1
            return {"fontname": f"DF{self._font_seq}", "fontfile": self._default_font}
        return {"fontname": "helv"}

    # ---------- düzenleme ----------

    @staticmethod
    def _qimage_bytes(image: QImage, image_format: str,
                      quality: int = -1) -> bytes:
        output = QByteArray()
        buffer = QBuffer(output)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError("Görüntü belleği açılamadı.")
        if not image.save(buffer, image_format, quality):
            buffer.close()
            raise RuntimeError("Görüntü PNG olarak kodlanamadı.")
        buffer.close()
        return bytes(output)

    @classmethod
    def _qimage_png_bytes(cls, image: QImage) -> bytes:
        return cls._qimage_bytes(image, "PNG")

    @staticmethod
    def _image_memory(image: QImage) -> memoryview:
        memory = image.bits()
        try:
            memory.setsize(image.sizeInBytes())
        except AttributeError:
            pass
        return memory

    def _full_page_scan_qimage(self, page_no: int
                               ) -> tuple[int, fitz.Rect, QImage] | None:
        """Ana tarama görselini kayıpsız çalışma kopyasıyla döndürür."""
        assert self.doc is not None
        scan = self._full_page_scan_image(page_no)
        if scan is None:
            return None
        xref, image_rect = scan
        cached = self._scan_qimage_cache.get(page_no)
        if cached is not None and cached[0] == xref:
            return cached[0], fitz.Rect(cached[1]), cached[2]
        extracted = self.doc.extract_image(xref)
        image = QImage.fromData(extracted["image"])
        if image.isNull():
            return None
        image = image.convertToFormat(QImage.Format.Format_RGB888)
        result = (xref, fitz.Rect(image_rect), image)
        
        self._scan_qimage_cache[page_no] = result
        if len(self._scan_qimage_cache) > 10:
            self._scan_qimage_cache.pop(next(iter(self._scan_qimage_cache)))
            
        if page_no not in self._source_scan_qimage_cache:
            self._source_scan_qimage_cache[page_no] = (
                xref, fitz.Rect(image_rect), image.copy())
            if len(self._source_scan_qimage_cache) > 10:
                self._source_scan_qimage_cache.pop(next(iter(self._source_scan_qimage_cache)))
        return result

    def _source_scan_qimage(self, page_no: int
                            ) -> tuple[int, fitz.Rect, QImage] | None:
        """Ilk acilan taramanin degismez piksel kaynagini dondurur."""
        current = self._full_page_scan_qimage(page_no)
        if current is None:
            return None
        source = self._source_scan_qimage_cache.get(page_no)
        if source is None:
            xref, image_rect, image = current
            source = (xref, fitz.Rect(image_rect), image.copy())
            self._source_scan_qimage_cache[page_no] = source
        return source[0], fitz.Rect(source[1]), source[2]

    def _replace_scan_image_in_place(
            self, page_no: int, xref: int, image_rect: fitz.Rect,
            image: QImage) -> None:
        """Tam sayfa taramasini ayni PDF image xref'i uzerinde yeniler.

        Onceki uygulama her tuslamada yeni bir PNG overlay ekliyordu. Hit kutusu
        bir sonraki OCR turunda birkac piksel kayinca eski overlay parcalari
        gorunur kaliyor, metin ust uste biniyor ve ilk duzenleme geri siziyordu.
        Burada sayfada tek gorunur tarama katmani kalir.
        """
        assert self.doc is not None
        rgb = (image if image.format() == QImage.Format.Format_RGB888
               else image.convertToFormat(QImage.Format.Format_RGB888))
        encoded = self._qimage_png_bytes(rgb)
        shared_elsewhere = False
        for other_no in range(len(self.doc)):
            if other_no == page_no:
                continue
            try:
                if any(int(info[0]) == xref
                       for info in self.doc[other_no].get_images(full=True)):
                    shared_elsewhere = True
                    break
            except Exception:
                continue
        if shared_elsewhere:
            # Image xref'leri belge geneline aittir. Nadir de olsa ayni tarama
            # birden cok sayfada paylasiliyorsa replace_image diger sayfayi da
            # degistirir. Bu durumda butun sayfayi kaplayan tek opak sonuc ekle;
            # onceki gorunum tamamen ortulur ve baska sayfaya dokunulmaz.
            self.doc[page_no].insert_image(
                image_rect, stream=encoded, overlay=True,
                keep_proportion=False)
        else:
            # Page.replace_image() hedef sayfaya gecici bir image operatoru
            # ekleyip sonra temizlemeye calisir; ardisik ikinci cagrida kurulu
            # PyMuPDF surumu bu operatorlerden birini gorunur birakabiliyor.
            # Yeni stream'i gecici bir sayfada olusturup nesneyi dogrudan mevcut
            # xref'e kopyalamak hedef sayfanin content stream'ine hic dokunmaz.
            scratch_no = len(self.doc)
            scratch = self.doc.new_page(width=1, height=1)
            try:
                replacement_xref = scratch.insert_image(
                    scratch.rect, stream=encoded, overlay=True,
                    keep_proportion=False)
                self.doc.xref_copy(replacement_xref, xref)
            finally:
                self.doc.delete_page(scratch_no)
        self._scan_image_cache[page_no] = (xref, fitz.Rect(image_rect))
        self._scan_qimage_cache[page_no] = (
            xref, fitz.Rect(image_rect), rgb.copy())

    def _repair_qimage_region(self, image: QImage, image_rect: fitz.Rect,
                              rect: fitz.Rect) -> bool:
        """Bir çalışma görselindeki alanı çevre kâğıt dokusuyla onarır."""
        intersection = rect & image_rect
        if intersection.is_empty:
            return False
        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        # Birkaç tarama pikseli taşırmak, harf kenarındaki antialias izini siler.
        pad_x = max(2, round(scale_x * .75))
        pad_y = max(2, round(scale_y * .55))
        x0 = max(0, math.floor((intersection.x0 - image_rect.x0) * scale_x) - pad_x)
        y0 = max(0, math.floor((intersection.y0 - image_rect.y0) * scale_y) - pad_y)
        x1 = min(image.width(), math.ceil((intersection.x1 - image_rect.x0) * scale_x) + pad_x)
        y1 = min(image.height(), math.ceil((intersection.y1 - image_rect.y0) * scale_y) + pad_y)
        if x1 <= x0 or y1 <= y0:
            return False

        memory = self._image_memory(image)
        stride = image.bytesPerLine()

        def pixel(x: int, y: int) -> tuple[int, int, int]:
            index = y * stride + x * 3
            return memory[index], memory[index + 1], memory[index + 2]

        def bright(value: tuple[int, int, int]) -> bool:
            return value[0] * .299 + value[1] * .587 + value[2] * .114 > 165

        perimeter: list[tuple[int, int, int]] = []
        sample_margin = max(4, round(max(scale_x, scale_y) * 3))
        for x in range(max(0, x0 - sample_margin),
                       min(image.width(), x1 + sample_margin), 2):
            for y in (max(0, y0 - sample_margin),
                      min(image.height() - 1, y1 + sample_margin - 1)):
                value = pixel(x, y)
                if bright(value):
                    perimeter.append(value)
        for y in range(max(0, y0 - sample_margin),
                       min(image.height(), y1 + sample_margin), 2):
            for x in (max(0, x0 - sample_margin),
                      min(image.width() - 1, x1 + sample_margin - 1)):
                value = pixel(x, y)
                if bright(value):
                    perimeter.append(value)
        if perimeter:
            base = tuple(round(statistics.median(values))
                         for values in zip(*perimeter))
        else:
            base = (250, 250, 248)

        search_depth = max(8, round(max(scale_x, scale_y) * 5))

        def band_color(side: str) -> tuple[int, int, int]:
            samples: list[tuple[int, int, int]] = []
            if side in ("top", "bottom"):
                start = max(0, y0 - search_depth) if side == "top" else y1
                end = y0 if side == "top" else min(image.height(), y1 + search_depth)
                for y in range(start, end, 2):
                    for x in range(x0, x1, 3):
                        value = pixel(x, y)
                        if bright(value):
                            samples.append(value)
            else:
                start = max(0, x0 - search_depth) if side == "left" else x1
                end = x0 if side == "left" else min(image.width(), x1 + search_depth)
                for x in range(start, end, 2):
                    for y in range(y0, y1, 3):
                        value = pixel(x, y)
                        if bright(value):
                            samples.append(value)
            if not samples:
                return base
            return tuple(round(statistics.median(values))
                         for values in zip(*samples))

        top_color = band_color("top")
        bottom_color = band_color("bottom")
        left_color = band_color("left")
        right_color = band_color("right")

        width = x1 - x0
        height = y1 - y0

        from PySide6.QtGui import QPainter, QLinearGradient, QColor
        from PySide6.QtCore import QPointF, QRect
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        
        # Dikey geçiş: C++ tarafında anında render edilir
        grad = QLinearGradient(QPointF(x0, y0), QPointF(x0, y1))
        
        # Renkleri harmanla (ağırlıklı ortalama)
        r0 = max(0, min(255, int(top_color[0] * .7 + left_color[0] * .3)))
        g0 = max(0, min(255, int(top_color[1] * .7 + left_color[1] * .3)))
        b0 = max(0, min(255, int(top_color[2] * .7 + left_color[2] * .3)))
        
        r1 = max(0, min(255, int(bottom_color[0] * .7 + right_color[0] * .3)))
        g1 = max(0, min(255, int(bottom_color[1] * .7 + right_color[1] * .3)))
        b1 = max(0, min(255, int(bottom_color[2] * .7 + right_color[2] * .3)))
        
        grad.setColorAt(0.0, QColor(r0, g0, b0))
        grad.setColorAt(1.0, QColor(r1, g1, b1))
        
        painter.fillRect(QRect(x0, y0, width, height), grad)
        painter.end()

        return True

    def editor_background_patch(self, page_no: int, hit,
                                source_image: QImage,
                                pixel_rect: tuple[int, int, int, int]
                                ) -> QImage | None:
        """Canlı editöre, sayfanın kendi dokusundan metinsiz bir zemin verir.

        Düz CSS rengi taranmış kâğıtta beyaz bir dikdörtgen gibi görünür. Bu
        önizleme kaynağın bir kopyasında yalnız özgün metin mürekkebini çevre
        dokusuyla onarır; tablo çizgileri ve editör sınırındaki gerçek pikseller
        aynen kalır. PDF belgesinde hiçbir değişiklik yapılmaz.
        """
        if self.doc is None or source_image.isNull():
            return None
        if not (0 <= page_no < len(self.doc)):
            return None
        x, y, width, height = pixel_rect
        if width <= 0 or height <= 0:
            return None

        image = source_image.convertToFormat(QImage.Format.Format_RGB888)
        page_rect = fitz.Rect(self.doc[page_no].rect)
        lines = list(getattr(hit, "lines", ())) or [hit]
        repaired = False
        for line in lines:
            if not getattr(line, "text", "").strip():
                continue
            line_rect = fitz.Rect(line.rect)
            if self._repair_qimage_region(image, page_rect, line_rect):
                repaired = True
        if not repaired and any(getattr(line, "text", "").strip()
                                for line in lines):
            return None

        # QWidget geometrisi yuvarlama nedeniyle sayfa kenarını bir piksel
        # aşabilse de her zaman istenen ölçüde bir patch döndür.
        patch = QImage(width, height, QImage.Format.Format_RGB888)
        br, bg, bb = getattr(hit, "background", (255, 255, 255))
        patch.fill(QColor(br, bg, bb))
        painter = QPainter(patch)
        painter.drawImage(QPointF(-x, -y), image)
        painter.end()
        return patch

    def _erase_scan_region(self, page_no: int, rect: fitz.Rect) -> bool:
        """Tarama üzerindeki alanı çevre dokusuyla doldurur; beyaz kutu bırakmaz."""
        assert self.doc is not None
        scan = self._full_page_scan_qimage(page_no)
        if scan is None:
            return False
        xref, image_rect, image = scan
        if not self._repair_qimage_region(image, image_rect, rect):
            return False

        # Yeniden JPEG kodlamak degismeyen harflerde dahi kumlanma/halo uretir.
        # Metin duzenleme ile ayni tek-katmanli kayipsiz yolu kullan.
        self._replace_scan_image_in_place(page_no, xref, image_rect, image)
        return True

    def _remove_content(self, page_no: int, rect: fitz.Rect,
                        remove_graphics: bool = False) -> None:
        """Vektör ya da tarama içeriğini sayfa zeminiyle uyumlu biçimde kaldırır."""
        assert self.doc is not None
        if rect.is_empty or rect.get_area() < .05:
            return
        scan = self._full_page_scan_image(page_no)
        background_xref = scan[0] if scan is not None else None
        page = self.doc[page_no]
        overlay_xrefs: list[int] = []
        if background_xref is not None:
            for image in list(page.get_images(full=True)):
                xref = int(image[0])
                if xref == background_xref:
                    continue
                try:
                    rects = page.get_image_rects(xref)
                except Exception:
                    continue
                if any(not (image_rect := fitz.Rect(item)).is_empty
                       and (image_rect & rect).get_area()
                       >= image_rect.get_area() * .55 for item in rects):
                    overlay_xrefs.append(xref)

        # Taşınmış bir üst katman yeniden taşınıyor/siliniyorsa alttaki orijinal
        # taramayı koru. İlk kez seçilen tarama içeriğinde ise eski yeri onar.
        if overlay_xrefs:
            for xref in overlay_xrefs:
                try:
                    page.delete_image(xref)
                except Exception:
                    pass
            scan_erased = True
        else:
            scan_erased = self._erase_scan_region(page_no, rect)
            page = self.doc[page_no]
        page.add_redact_annot(rect, fill=None, cross_out=False)
        # Metin düzenlerken graphics=0 (çerçeveler ve tablo çizgileri korunur).
        # Kullanıcı "Sil" aracıyla alanı seçtiğinde remove_graphics=True ise vektörler de silinir.
        page.apply_redactions(images=0 if scan_erased else 2,
                              graphics=1 if remove_graphics else 0, text=0)

    def _rollback_edit(self, was_dirty: bool) -> None:
        snapshot = self._undo_stack.pop()
        self._restore_undo_state(snapshot)
        self.dirty = was_dirty

    def _font_text_width(self, kwargs: dict, text: str, fontsize: float) -> float:
        path = kwargs.get("fontfile")
        try:
            if path:
                font = self._font_metric_cache.get(path)
                if font is None:
                    font = fitz.Font(fontfile=path)
                    self._font_metric_cache[path] = font
            else:
                font = fitz.Font(kwargs.get("fontname", "helv"))
            return font.text_length(text, fontsize=fontsize)
        except Exception:
            return max(1.0, len(text) * fontsize * .55)

    def _insert_fitted_line(self, page: fitz.Page, rect: fitz.Rect, text: str,
                            style: TextStyle, kwargs: dict,
                            baseline: float | None, align: int,
                            requested_size: float) -> float:
        """Tek satırı kutuya mümkün olduğunca orijinal boyutunu koruyarak yerleştirir.

        Adobe benzeri davranış: önce orijinal puntoyla dener, taşarsa çok kademeli
        ve korumacı küçültür. Orijinal metin zaten kutuya sığdığı için, aynı font
        varsa yeniden sığmasının beklenir.
        """
        size = float(requested_size)
        available = max(rect.width, 1.0)
        max_shrink = .92  # en fazla %8 küçült (korumacı)

        def fits(current_size: float) -> bool:
            return self._font_text_width(kwargs, text, current_size) <= available + .35

        if not fits(size):
            floor = size * max_shrink
            step = max(.05, size * .012)
            candidate = size - step
            while candidate >= floor - .01:
                if fits(candidate):
                    size = candidate
                    break
                candidate -= step
            else:
                measured = self._font_text_width(kwargs, text, floor)
                if measured > .1:
                    ratio = available / measured
                    size = max(4.0, min(size, floor * max(.88, ratio)))
                else:
                    size = max(4.0, floor)

        width = self._font_text_width(kwargs, text, size)

        if baseline is None or baseline <= rect.y0 or baseline >= rect.y1 + size * .5:
            # Taban çizgisi bilinmiyorsa sezgisel hesapla
            baseline = rect.y0 + (rect.height + size * .78) / 2
            # Sadece sezgisel hesaplama durumunda sınırla
            baseline = min(rect.y1 - max(.3, size * .06),
                           max(rect.y0 + size * .70, baseline))
        # Orijinal taban çizgisi PDF span origin'den geldiyse dokunma —
        # Adobe benzeri piksel-hassas konumlandırma sağlar.
        if align == 1:
            x = rect.x0 + max(0.0, (available - width) / 2)
        elif align == 2:
            x = rect.x1 - width
        else:
            x = rect.x0
        page.insert_text(fitz.Point(x, baseline), text, fontsize=size,
                         color=style.color, overlay=True, **kwargs)
        return size

    def delete_area(self, page_no: int, rect: fitz.Rect) -> None:
        assert self.doc is not None
        was_dirty = self.dirty
        self._snapshot()
        try:
            self._remove_content(page_no, rect, remove_graphics=True)
            self.dirty = True
            self._invalidate_page_analysis(page_no, rect)
        except Exception:
            self._rollback_edit(was_dirty)
            raise

    def _insertion_rect(self, page_no: int, point: fitz.Point,
                        size: float) -> fitz.Rect:
        """Yazı ekleme için hücre/satır/sayfa sınırlarından taşmayan alanı bulur."""
        assert self.doc is not None
        page_rect = self.doc[page_no].rect
        margin = max(8.0, min(24.0, page_rect.width * .025))
        cell = self.cell_at_point(page_no, point)
        if cell is not None:
            inset = min(2.2, cell.width * .03, cell.height * .12)
            bounds = fitz.Rect(cell.x0 + inset, cell.y0 + inset,
                               cell.x1 - inset, cell.y1 - inset)
            left = max(bounds.x0, min(point.x, bounds.x1 - 8.0))
            return fitz.Rect(left, bounds.y0, bounds.x1, bounds.y1)

        line = self.line_at_point(page_no, point)
        right = page_rect.x1 - margin
        if line is not None:
            right = min(right, max(line.rect.x1,
                                   line.block_rect.x1
                                   if line.block_rect is not None
                                   else line.layout_rect.x1))
        left = max(page_rect.x0 + margin, min(point.x, right - 12.0))
        top = max(page_rect.y0 + margin, point.y - size * .88)
        bottom = page_rect.y1 - margin
        return fitz.Rect(left, top, right, bottom)

    def insert_text(self, page_no: int, point: fitz.Point, text: str,
                    style: TextStyle | None = None,
                    fontsize: float | None = None,
                    color: tuple[float, float, float] | None = None) -> tuple[float, fitz.Rect]:
        """Metni sağ sınıra göre sarar; gerekirse kutuya sığacak kadar korumacı küçültür."""
        assert self.doc is not None
        requested = fontsize if fontsize is not None else (style.size if style else 11.0)
        col = color if color is not None else (style.color if style else (0, 0, 0))
        effective_style = style or TextStyle(size=requested, color=col)
        if color is not None:
            effective_style = TextStyle(effective_style.fontname,
                                        effective_style.size, col,
                                        effective_style.flags)
        was_dirty = self.dirty
        self._snapshot()
        try:
            page = self.doc[page_no]
            kwargs = self._font_kwargs_for(page, page_no, effective_style, text)
            bounds = self._insertion_rect(page_no, point, requested)
            if bounds.width < 8 or bounds.height < 4:
                raise ValueError("Yazının yerleşeceği kullanılabilir alan çok dar.")

            # Kısa tek satırlık metinlerde genişliği gereksiz yere sayfa sonuna kadar açma
            est_width = self._font_text_width(kwargs, text, requested)
            if "\n" not in text and est_width < bounds.width:
                bounds = fitz.Rect(bounds.x0, bounds.y0,
                                   min(bounds.x1, bounds.x0 + est_width + 14.0),
                                   bounds.y1)

            minimum = max(4.0, float(requested) * .88)
            size = max(minimum, float(requested))
            committed = False
            shrink_step = max(.08, float(requested) * .018)
            inserted_rect = bounds
            while size >= minimum - .01:
                shape = page.new_shape()
                spare = shape.insert_textbox(
                    bounds, text, fontsize=size, color=col,
                    align=fitz.TEXT_ALIGN_LEFT, lineheight=1.15, **kwargs)
                if spare >= -0.01:
                    shape.commit(overlay=True)
                    committed = True
                    used_height = max(size * 1.25, bounds.height - max(0.0, spare))
                    inserted_rect = fitz.Rect(bounds.x0, bounds.y0, bounds.x1,
                                              min(bounds.y1, bounds.y0 + used_height + 4.0))
                    break
                next_size = max(minimum, size - shrink_step)
                if abs(next_size - size) < .01:
                    break
                size = next_size
            if not committed:
                raise ValueError("Metin, sayfa veya hücre sınırları içine sığmıyor.")
            self.dirty = True
            self._invalidate_page_analysis(page_no, inserted_rect)
            return size, inserted_rect
        except Exception:
            self._rollback_edit(was_dirty)
            raise

    @staticmethod
    def _scan_font_family(style: TextStyle) -> str:
        clean = style.fontname.split("+")[-1].lower()
        for key, family in _QT_FONT_FAMILIES:
            if key in clean:
                return family
        if any(k in clean for k in ("sans", "gothic", "helv")):
            return "Arial"
        if "serif" in clean or "roman" in clean:
            return "Times New Roman"
        if "mono" in clean or "code" in clean:
            return "Courier New"
        return "Arial"

    def _scan_edit_font(self, style: TextStyle, reference: TextWord | None,
                        scale_x: float, scale_y: float) -> QFont:
        """OCR stilini taramanın gerçek piksel ölçeğinde kalibre eder."""
        family = self._scan_font_family(style)
        fontfile = self._windows_fontfile(style)
        if fontfile:
            registered = _QT_APP_FONT_CACHE.get(fontfile)
            if registered is None:
                font_id = QFontDatabase.addApplicationFont(fontfile)
                families = (QFontDatabase.applicationFontFamilies(font_id)
                            if font_id >= 0 else [])
                registered = families[0] if families else family
                _QT_APP_FONT_CACHE[fontfile] = registered
            family = registered
        font = QFont(family)
        font.setBold(style.bold)
        font.setItalic(style.italic)
        font.setPixelSize(max(5, round(style.size * scale_y)))

        if reference is not None and reference.text:
            metrics = QFontMetricsF(font)
            font_height = metrics.height()
            target_height = max(2.0, reference.rect.height * scale_y)
            if font_height > .5:
                adjusted = round(font.pixelSize() * target_height / font_height)
                font.setPixelSize(max(5, min(round(target_height * 1.5), adjusted)))
            metrics = QFontMetricsF(font)
            measured_width = metrics.horizontalAdvance(reference.text)
            target_width = max(2.0, reference.rect.width * scale_x)
            if measured_width > .5:
                stretch = round(100 * target_width / measured_width)
                font.setStretch(max(72, min(132, stretch)))
        return font

    def _transparent_glyph_sprite(
            self, image: QImage, pixel_rect: tuple[int, int, int, int],
            background: tuple[int, int, int],
            foreground: tuple[float, float, float]
            ) -> tuple[QImage, int, int] | None:
        """Kaynak taramadaki tek harfi şeffaf, gerçek-piksel glifine çevirir."""
        x0, y0, x1, y1 = pixel_rect
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(image.width(), x1); y1 = min(image.height(), y1)
        if x1 <= x0 or y1 <= y0:
            return None
        sprite = image.copy(x0, y0, x1 - x0, y1 - y0).convertToFormat(
            QImage.Format.Format_RGBA8888)
        memory = self._image_memory(sprite)
        stride = sprite.bytesPerLine()
        br, bg, bb = background
        fr, fg, fb = (max(0, min(255, round(value * 255)))
                      for value in foreground)
        background_luma = br * .299 + bg * .587 + bb * .114
        foreground_luma = fr * .299 + fg * .587 + fb * .114
        contrast = max(32.0, background_luma - foreground_luma)
        ink_left, ink_top = sprite.width(), sprite.height()
        ink_right = ink_bottom = -1
        ink_pixels = 0

        for y in range(sprite.height()):
            for x in range(sprite.width()):
                index = y * stride + x * 4
                pr, pg, pb = (memory[index], memory[index + 1],
                              memory[index + 2])
                observed_luma = pr * .299 + pg * .587 + pb * .114
                alpha = (background_luma - observed_luma) / contrast
                if alpha <= .025:
                    memory[index + 3] = 0
                    continue
                # Sürekli alfa: kenar antialiasını 8 bit hassasiyetle koru.
                # Eski binari yaklaşım (0 veya 255) harf kenarlarını testere
                # dişi gibi yapıyordu; şimdi yumuşak geçiş korunuyor.
                clamped = max(0.0, min(1.0, alpha))
                memory[index + 3] = round(clamped * 255)
                if clamped >= .12:
                    ink_pixels += 1
                    ink_left = min(ink_left, x); ink_right = max(ink_right, x)
                    ink_top = min(ink_top, y); ink_bottom = max(ink_bottom, y)

        if ink_pixels < 4 or ink_right < ink_left or ink_bottom < ink_top:
            return None
        pad = 1
        left = max(0, ink_left - pad); top = max(0, ink_top - pad)
        right = min(sprite.width() - 1, ink_right + pad)
        bottom = min(sprite.height() - 1, ink_bottom + pad)
        return (sprite.copy(left, top, right - left + 1, bottom - top + 1),
                left, top)

    def _source_glyph_draws(
            self, page_no: int, hit: TextHit, text: str, target_font: QFont,
            image: QImage, image_rect: fitz.Rect
            ) -> tuple[list[tuple[QImage | None, float, float, str]], float]:
        """Kelime içi görüntü segmentleriyle gerçek harf piksellerini klonlar."""
        needed = {character for character in text if not character.isspace()}
        if not needed:
            return [], 1.0
        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        target_family = self._scan_font_family(hit.style)
        candidates: dict[str, list[tuple[
            float, tuple[int, int, int, int], TextHit, float]]] = {
            character: [] for character in needed
        }

        # Aday glifleri duzenlenmis sahneden degil, belge ilk acildiginda
        # alinan degismez OCR bankasindan kes. Aksi halde ikinci duzenlemede
        # sentetik ``85`` glifi orijinal ``77`` koordinatlariyla eslesiyordu.
        source_lines = self._source_ocr_lines.get(page_no)
        if source_lines:
            target_rect = hit.rect

            def source_rank(line: TextHit) -> tuple[float, float, float]:
                same_row = abs(line.rect.y0 - target_rect.y0) <= max(
                    1.5, target_rect.height * .35)
                same_block = (line.block_rect is not None
                              and hit.block_rect is not None
                              and max(abs(a - b) for a, b in zip(
                                  line.block_rect, hit.block_rect)) <= 1.5)
                style_penalty = 0.0
                if self._scan_font_family(line.style) != target_family:
                    style_penalty += 2.0
                if line.style.bold != hit.style.bold:
                    style_penalty += 1.0
                return (0.0 if same_row else (1.0 if same_block else 2.0),
                        style_penalty,
                        abs(line.rect.y0 - target_rect.y0))

            # Tum sayfadaki yuzlerce kelimeyi her tuslamada yeniden segmente
            # etmek hem gereksiz hem de buyuk taramalarda UI'yi donduruyordu.
            # Ayni satir/blok ve en yakin ayni stil satirlari glif bankasi icin
            # yeterlidir; eksik glifte renderer zaten kontrollu fallback yapar.
            source_lines = sorted(source_lines, key=source_rank)[:24]
        else:
            source_lines = (hit.lines if isinstance(hit, TextBlockHit)
                            else [hit])
        for source_line in source_lines:
            if not source_line.words or source_line.baseline is None:
                continue
            height_ratio = source_line.rect.height / max(hit.rect.height, .1)
            # Ölçekleme gerçek tarama piksellerini değiştirir. Bu yüzden yalnız
            # hedef satırla pratikte aynı puntoya ait glifler aday kabul edilir.
            if not .70 <= height_ratio <= 1.30:
                continue
            same_family = (self._scan_font_family(source_line.style)
                           == target_family)
            base_score = abs(1.0 - height_ratio) * 260.0
            if not same_family:
                base_score += 420.0
            if source_line.style.bold != hit.style.bold:
                base_score += 220.0
            same_row = (abs(source_line.rect.y0 - hit.rect.y0)
                        <= max(1.5, hit.rect.height * .35))
            if source_line is hit or same_row:
                base_score -= 900.0
            base_score += abs(source_line.rect.y0 - hit.rect.y0) * .08

            baseline_px = ((source_line.baseline - image_rect.y0) * scale_y)
            source_glyphs: list[TextGlyph] = []
            for source_word in source_line.words:
                if not any(character in needed for character in source_word.text):
                    continue
                word_glyphs = self._glyphs_for_word(
                    source_line, source_word, page_no, image, image_rect)
                if word_glyphs is not None:
                    source_glyphs.extend(word_glyphs)
            for glyph in source_glyphs:
                character = glyph.text
                if character not in needed:
                    continue
                gx0, gy0, gx1, gy1 = self._image_rect_for_pdf(
                    glyph.rect, image_rect, image)
                if gx1 <= gx0 or gy1 <= gy0:
                    continue
                # Tesseract karakter kutusu glifin gerçek mürekkep sınırını zaten
                # kapsar. Komşu harften alınan 1-2 piksel, özellikle tekrar
                # düzenlemede nokta / çizgi izi oluşturduğu için dış pay ekleme.
                pad = 0
                pixel_rect = (gx0 - pad, gy0 - pad, gx1 + pad, gy1 + pad)
                glyph_height_ratio = glyph.rect.height / max(hit.rect.height, .1)
                score = base_score + abs(glyph_height_ratio - .86) * 16.0
                candidates[character].append(
                    (score, pixel_rect, source_line, baseline_px))

        for values in candidates.values():
            values.sort(key=lambda item: item[0])

        target_metrics = QFontMetricsF(target_font)
        usages: dict[str, int] = {}
        draws: list[tuple[QImage | None, float, float, str]] = []
        found = 0
        nonspace_count = sum(1 for character in text
                             if not character.isspace())
        for index, character in enumerate(text):
            if character.isspace():
                continue
            choices = candidates.get(character, ())
            sprite = None
            if choices:
                use_index = usages.get(character, 0) % min(3, len(choices))
                usages[character] = usages.get(character, 0) + 1
                # En iyi aday sorunluysa sıradaki iki adayı da dene.
                trial_order = list(range(use_index, min(len(choices), use_index + 3)))
                trial_order += list(range(0, min(use_index, 3)))
                for candidate_index in trial_order:
                    (_score, pixel_rect, source_line,
                     source_baseline_px) = choices[candidate_index]
                    sprite_info = self._transparent_glyph_sprite(
                        image, pixel_rect, source_line.background,
                        source_line.style.color)
                    if sprite_info is not None:
                        sprite, trim_left, trim_top = sprite_info
                        break
            if sprite is None:
                # Kaynak glif bulunamadı — bu karakter font fallback ile çizilecek.
                # Ama bulunan glifleri atmıyoruz, kısmi kaplama ile devam ediyoruz.
                bounds = target_metrics.tightBoundingRect(character)
                origin_x = target_metrics.horizontalAdvance(text[:index])
                # None sprite = font fallback gerekli işareti
                draws.append((None, origin_x, 0.0, character))
                continue

            bounds = target_metrics.tightBoundingRect(character)
            origin_x = target_metrics.horizontalAdvance(text[:index])
            # Dikey konum kaynak glifin kendi gerçek taban çizgisi uzaklığıyla
            # taşınır; boyutlandırma yapılmaz, harf pikselleri aynen kalır.
            source_sprite_top = pixel_rect[1] + trim_top
            source_ascent = source_baseline_px - source_sprite_top
            draws.append((sprite, origin_x + bounds.x(), -source_ascent, character))
            found += 1
        return draws, found / max(nonspace_count, 1)

    @staticmethod
    def _image_rect_for_pdf(rect: fitz.Rect, image_rect: fitz.Rect,
                            image: QImage) -> tuple[int, int, int, int]:
        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        x0 = max(0, math.floor((rect.x0 - image_rect.x0) * scale_x))
        y0 = max(0, math.floor((rect.y0 - image_rect.y0) * scale_y))
        x1 = min(image.width(), math.ceil((rect.x1 - image_rect.x0) * scale_x))
        y1 = min(image.height(), math.ceil((rect.y1 - image_rect.y0) * scale_y))
        return x0, y0, x1, y1

    def _segment_word_glyphs(
            self, page_no: int, hit: TextHit, word: TextWord,
            image: QImage, image_rect: fitz.Rect) -> list[TextGlyph] | None:
        """Kelime görüntüsünü font metrikleri ve mürekkep vadileriyle harflere böler.

        Tesseract LSTM sembol kutuları fiziksel gliften bir karakter sağa/sola
        kayabildiği için kaynak piksel klonlamasında doğrudan kullanılamaz.
        Kelime kutusu ve tanınmış metin güvenilir kaldığından, karakter sınırları
        gerçek görüntüdeki düşük-mürekkep sütunlarına oturtulur.
        """
        if not word.text or any(character.isspace() for character in word.text):
            return None
        key = (page_no, round(word.rect.x0, 3), round(word.rect.y0, 3),
               round(word.rect.x1, 3), round(word.rect.y1, 3), word.text,
               image.width(), image.height())
        cached = self._segmented_word_glyph_cache.get(key)
        if cached is not None:
            return [TextGlyph(fitz.Rect(item.rect), item.text)
                    for item in cached]

        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        wx0, wy0, wx1, wy1 = self._image_rect_for_pdf(
            word.rect, image_rect, image)
        wx0 = max(0, wx0 - 1); wy0 = max(0, wy0 - 1)
        wx1 = min(image.width(), wx1 + 1); wy1 = min(image.height(), wy1 + 1)
        if wx1 - wx0 < len(word.text) or wy1 <= wy0:
            return None

        font = self._scan_edit_font(hit.style, word, scale_x, scale_y)
        metrics = QFontMetricsF(font)
        cumulative = [metrics.horizontalAdvance(word.text[:index])
                      for index in range(len(word.text) + 1)]
        total_advance = cumulative[-1]
        if total_advance <= .5:
            return None

        rgb = image if image.format() == QImage.Format.Format_RGB888 \
            else image.convertToFormat(QImage.Format.Format_RGB888)
        memory = self._image_memory(rgb)
        stride = rgb.bytesPerLine()
        background_luma = (hit.background[0] * .299
                           + hit.background[1] * .587
                           + hit.background[2] * .114)
        foreground_luma = sum(hit.style.color) / 3 * 255
        ink_threshold = max(12.0,
                            (background_luma - foreground_luma) * .075)

        projection: list[float] = []
        for x in range(wx0, wx1):
            strength = 0.0
            for y in range(wy0, wy1):
                index = y * stride + x * 3
                luma = (memory[index] * .299 + memory[index + 1] * .587
                        + memory[index + 2] * .114)
                darkness = background_luma - luma
                if darkness >= ink_threshold:
                    strength += min(1.0, darkness / max(ink_threshold * 3, 1))
            projection.append(strength)

        boundaries = [wx0]
        width = wx1 - wx0
        average_width = width / len(word.text)
        for index in range(1, len(word.text)):
            expected = wx0 + width * cumulative[index] / total_advance
            radius = max(2, min(5, round(average_width * .24)))
            lower = max(boundaries[-1] + 1, round(expected) - radius)
            upper = min(wx1 - (len(word.text) - index),
                        round(expected) + radius)
            if lower > upper:
                boundary = max(boundaries[-1] + 1,
                               min(wx1 - (len(word.text) - index),
                                   round(expected)))
            else:
                def boundary_cost(candidate: int) -> float:
                    local = candidate - wx0
                    ink = projection[local]
                    if local > 0:
                        ink += projection[local - 1] * .55
                    if local + 1 < len(projection):
                        ink += projection[local + 1] * .55
                    return ink + abs(candidate - expected) * .34

                boundary = min(range(lower, upper + 1), key=boundary_cost)
            boundaries.append(boundary)
        boundaries.append(wx1)

        glyphs: list[TextGlyph] = []
        for index, character in enumerate(word.text):
            cell_left, cell_right = boundaries[index], boundaries[index + 1]
            ink_points: list[tuple[int, int]] = []
            for x in range(cell_left, cell_right):
                for y in range(wy0, wy1):
                    offset = y * stride + x * 3
                    luma = (memory[offset] * .299
                            + memory[offset + 1] * .587
                            + memory[offset + 2] * .114)
                    if background_luma - luma >= ink_threshold:
                        ink_points.append((x, y))
            if not ink_points:
                return None
            gx0 = max(cell_left, min(point[0] for point in ink_points) - 1)
            gx1 = min(cell_right, max(point[0] for point in ink_points) + 2)
            gy0 = max(wy0, min(point[1] for point in ink_points) - 1)
            gy1 = min(wy1, max(point[1] for point in ink_points) + 2)
            rect = fitz.Rect(
                image_rect.x0 + gx0 / scale_x,
                image_rect.y0 + gy0 / scale_y,
                image_rect.x0 + gx1 / scale_x,
                image_rect.y0 + gy1 / scale_y,
            )
            glyphs.append(TextGlyph(rect, character))

        self._segmented_word_glyph_cache[key] = [
            TextGlyph(fitz.Rect(item.rect), item.text) for item in glyphs]
        return glyphs

    def _glyphs_for_word(
            self, hit: TextHit, word: TextWord, page_no: int | None = None,
            image: QImage | None = None,
            image_rect: fitz.Rect | None = None) -> list[TextGlyph] | None:
        """Bir kelimenin güvenli ve sıralı karakter kutularını döndürür."""
        if page_no is not None and image is not None and image_rect is not None:
            segmented = self._segment_word_glyphs(
                page_no, hit, word, image, image_rect)
            if segmented is not None:
                return segmented
        tolerance = max(.35, word.rect.height * .06)
        glyphs = []
        for glyph in hit.glyphs:
            center_x = (glyph.rect.x0 + glyph.rect.x1) / 2
            vertical = (glyph.rect & word.rect).height
            if (word.rect.x0 - tolerance <= center_x
                    <= word.rect.x1 + tolerance
                    and vertical >= min(glyph.rect.height,
                                        word.rect.height) * .35):
                glyphs.append(glyph)
        glyphs.sort(key=lambda item: (item.rect.x0, item.rect.y0))
        if (len(glyphs) != len(word.text)
                or any(len(glyph.text) != 1 for glyph in glyphs)
                or "".join(glyph.text for glyph in glyphs) != word.text):
            return None
        return glyphs

    def _expand_character_change(
            self, page_no: int, hit: TextHit, old_words: list[TextWord],
            old_tokens: list[str], new_tokens: list[str], image: QImage,
            image_rect: fitz.Rect,
            ) -> tuple[list[TextWord], list[str], list[str], TextWord | None]:
        """Tek kelimelik düzenlemeyi ortak karakter önek/soneklerine ayırır.

        Böylece kelimenin değişmeyen harfleri yeniden çizilmez; kaynak tarama
        pikselleri olarak kalır. Yalnız gerçekten eklenen/değişen karakter dizisi
        glif bankasından üretilir.
        """
        token_prefix = 0
        while (token_prefix < len(old_tokens)
               and token_prefix < len(new_tokens)
               and old_tokens[token_prefix] == new_tokens[token_prefix]):
            token_prefix += 1
        token_suffix = 0
        while (token_suffix < len(old_tokens) - token_prefix
               and token_suffix < len(new_tokens) - token_prefix
               and old_tokens[-1 - token_suffix]
               == new_tokens[-1 - token_suffix]):
            token_suffix += 1
        if (len(old_tokens) - token_prefix - token_suffix != 1
                or len(new_tokens) - token_prefix - token_suffix != 1):
            return old_words, old_tokens, new_tokens, None

        old_word = old_words[token_prefix]
        old_value = old_tokens[token_prefix]
        new_value = new_tokens[token_prefix]
        glyphs = self._glyphs_for_word(
            hit, old_word, page_no, image, image_rect)
        if glyphs is None:
            return old_words, old_tokens, new_tokens, None

        character_prefix = 0
        while (character_prefix < len(old_value)
               and character_prefix < len(new_value)
               and old_value[character_prefix] == new_value[character_prefix]):
            character_prefix += 1
        character_suffix = 0
        while (character_suffix < len(old_value) - character_prefix
               and character_suffix < len(new_value) - character_prefix
               and old_value[-1 - character_suffix]
               == new_value[-1 - character_suffix]):
            character_suffix += 1
        if character_prefix == 0 and character_suffix == 0:
            return old_words, old_tokens, new_tokens, None

        old_middle_end = len(old_value) - character_suffix
        new_middle_end = len(new_value) - character_suffix
        old_middle = old_value[character_prefix:old_middle_end]
        new_middle = new_value[character_prefix:new_middle_end]
        prefix_text = old_value[:character_prefix]
        suffix_text = (old_value[old_middle_end:]
                       if character_suffix else "")

        positive_gaps = [right.rect.x0 - left.rect.x1
                         for left, right in zip(glyphs, glyphs[1:])
                         if right.rect.x0 - left.rect.x1 > .04]
        character_gap = (statistics.median(positive_gaps)
                         if positive_gaps else max(.12,
                                                   old_word.rect.height * .025))

        def segment(start: int, end: int, text: str) -> TextWord:
            if start < end:
                x0 = glyphs[start].rect.x0
                x1 = glyphs[end - 1].rect.x1
            elif start <= 0:
                x0 = x1 = old_word.rect.x0
            elif start >= len(glyphs):
                x0 = x1 = old_word.rect.x1 + character_gap
            else:
                x0 = x1 = ((glyphs[start - 1].rect.x1
                            + glyphs[start].rect.x0) / 2)
            return TextWord(
                fitz.Rect(x0, old_word.rect.y0, x1, old_word.rect.y1),
                text, old_word.confidence)

        expanded_words = list(old_words[:token_prefix])
        expanded_old = list(old_tokens[:token_prefix])
        expanded_new = list(new_tokens[:token_prefix])
        if prefix_text:
            prefix_segment = segment(0, character_prefix, prefix_text)
            expanded_words.append(prefix_segment)
            expanded_old.append(prefix_text)
            expanded_new.append(prefix_text)

        expanded_words.append(segment(character_prefix, old_middle_end,
                                      old_middle))
        expanded_old.append(old_middle)
        expanded_new.append(new_middle)

        if suffix_text:
            suffix_segment = segment(old_middle_end, len(old_value), suffix_text)
            expanded_words.append(suffix_segment)
            expanded_old.append(suffix_text)
            expanded_new.append(suffix_text)

        expanded_words.extend(old_words[token_prefix + 1:])
        expanded_old.extend(old_tokens[token_prefix + 1:])
        expanded_new.extend(new_tokens[token_prefix + 1:])
        return expanded_words, expanded_old, expanded_new, old_word

    def _estimated_scan_words(self, page_no: int, hit: TextHit,
                              new_text: str, image: QImage,
                              image_rect: fitz.Rect) -> list[TextWord]:
        """Son duzenlemenin kelime geometrisini kararlı sahne icin hesaplar."""
        tokens = new_text.split()
        if not tokens:
            return []
        old_words = list(hit.words)
        if (len(tokens) == len(old_words)
                and all(len(token) == len(word.text)
                        for token, word in zip(tokens, old_words))):
            # Skor, tarih, belge numarasi gibi esit karakterli degisimlerde
            # renderer satiri yerinden oynatmaz. Semantik modeli yeniden
            # ortalamak ikinci editin silme kutusunu yanlis koordinata tasiyip
            # `%` / parantez parcalari birakiyordu.
            return [TextWord(fitz.Rect(word.rect), token, word.confidence)
                    for token, word in zip(tokens, old_words)]
        scale_x = image.width() / max(image_rect.width, .1)
        scale_y = image.height() / max(image_rect.height, .1)
        reference = max(old_words, key=lambda word: len(word.text), default=None)
        font = self._scan_edit_font(hit.style, reference, scale_x, scale_y)
        metrics = QFontMetricsF(font)

        calibration: list[float] = []
        for word in old_words:
            measured = metrics.horizontalAdvance(word.text) / scale_x
            if measured > .1:
                calibration.append(word.rect.width / measured)
        width_scale = (statistics.median(calibration)
                       if calibration else 1.0)
        width_scale = max(.68, min(1.42, width_scale))

        widths: list[float] = []
        for index, token in enumerate(tokens):
            if (index < len(old_words)
                    and len(token) == len(old_words[index].text)):
                # Es uzunluktaki skor / tarih / numara degisimlerinde kaynak
                # kelime kutusunu sabit tutmak caret ve ikinci edit koordinatini
                # en kararlı bicimde korur.
                widths.append(old_words[index].rect.width)
            else:
                widths.append(max(.5, metrics.horizontalAdvance(token)
                                  / scale_x * width_scale))
        gaps = [right.rect.x0 - left.rect.x1
                for left, right in zip(old_words, old_words[1:])
                if right.rect.x0 > left.rect.x1]
        gap = (statistics.median(gaps) if gaps
               else max(.8, hit.rect.height * .22))
        total = sum(widths) + gap * max(0, len(widths) - 1)
        layout = hit.layout_rect
        if hit.align == 1:
            x = layout.x0 + max(0.0, (layout.width - total) / 2)
        elif hit.align == 2:
            x = layout.x1 - total
        else:
            x = old_words[0].rect.x0 if old_words else hit.rect.x0
        words: list[TextWord] = []
        confidence = (statistics.median(word.confidence for word in old_words)
                      if old_words else hit.confidence)
        for token, width in zip(tokens, widths):
            words.append(TextWord(
                fitz.Rect(x, hit.rect.y0, x + width, hit.rect.y1),
                token, confidence))
            x += width + gap
        return words

    def _commit_current_ocr_line(
            self, page_no: int, hit: TextHit, new_text: str,
            image: QImage, image_rect: fitz.Rect,
            rendered_words: list[TextWord] | None = None) -> None:
        """Tek satirin guncel OCR sahnesini yeniden OCR yapmadan gunceller."""
        current = self._ocr_lines.setdefault(page_no, [])
        if not new_text.strip():
            self._ocr_lines[page_no] = [item for item in current if item is not hit]
            self._ocr_complete.add(page_no)
            return
        words = rendered_words or self._estimated_scan_words(
            page_no, hit, new_text, image, image_rect)
        hit.text = new_text
        hit.words = [TextWord(fitz.Rect(word.rect), word.text, word.confidence)
                     for word in words]
        # Guncel kelimenin karakter kutulari bir sonraki editte mevcut goruntuden
        # yeniden segmentlenir; eski OCR glyph etiketleri artik kullanilmaz.
        hit.glyphs = []
        if words:
            hit.rect = fitz.Rect(words[0].rect)
            for word in words[1:]:
                hit.rect.include_rect(word.rect)
        if not any(item is hit for item in current):
            current.append(hit)
            current.sort(key=lambda item: (item.rect.y0, item.rect.x0))
        self._ocr_complete.add(page_no)

    def _replace_scan_text_hit(self, page_no: int, hit: TextHit,
                               new_text: str) -> float | None:
        """Taranmış satırda yalnız değişen parçayı üretir.

        Eşit kalan önek ve sonek kelimeleri ana tarama görselinden piksel olarak
        kopyalanır. Böylece bir kelime değiştiğinde satırın geri kalanının fontu,
        kalınlığı, tarama yumuşaklığı ve rengi yeniden üretilmez; birebir korunur.
        Sonek, yeni parçanın genişliğine göre doğal metin akışı gibi kaydırılır.
        """
        assert self.doc is not None
        scan = self._full_page_scan_qimage(page_no)
        if scan is None or not hit.words:
            raise ValueError("Taranmış satırın piksel kaynağı bulunamadı.")
        base_xref, image_rect, base_image = scan
        source_scan = self._source_scan_qimage(page_no)
        if source_scan is None:
            raise ValueError("Taranmış satırın değişmez piksel kaynağı bulunamadı.")
        _source_xref, _source_rect, source_image = source_scan
        # Her revizyon bir onceki guncel tek goruntuden baslar. Yeni glifler ise
        # asla bu sentetik goruntuden degil, yukaridaki degismez kaynaktan gelir.
        image = base_image.copy()
        old_words = list(hit.words)
        old_tokens = [word.text for word in old_words]
        new_tokens = new_text.split()
        old_words, old_tokens, new_tokens, character_reference = (
            self._expand_character_change(
                page_no, hit, old_words, old_tokens, new_tokens,
                base_image, image_rect))

        prefix_count = 0
        while (prefix_count < len(old_tokens)
               and prefix_count < len(new_tokens)
               and old_tokens[prefix_count] == new_tokens[prefix_count]):
            prefix_count += 1
        suffix_count = 0
        while (suffix_count < len(old_tokens) - prefix_count
               and suffix_count < len(new_tokens) - prefix_count
               and old_tokens[-1 - suffix_count]
               == new_tokens[-1 - suffix_count]):
            suffix_count += 1

        old_change_end = len(old_words) - suffix_count
        new_change_end = len(new_tokens) - suffix_count
        changed_words = old_words[prefix_count:old_change_end]
        changed_text = " ".join(new_tokens[prefix_count:new_change_end])
        self._last_scan_changed_text = changed_text
        prefix_word = old_words[prefix_count - 1] if prefix_count else None
        suffix_index = len(old_words) - suffix_count
        suffix_word = old_words[suffix_index] if suffix_count else None

        gaps = [max(0.0, right.rect.x0 - left.rect.x1)
                for left, right in zip(old_words, old_words[1:])]
        positive_gaps = [gap for gap in gaps
                         if .15 <= gap <= max(18.0, hit.rect.height * 2.5)]
        typical_gap = (statistics.median(positive_gaps) if positive_gaps
                       else max(1.2, hit.rect.height * .24))
        left_gap = typical_gap
        if prefix_word is not None and changed_words:
            left_gap = max(.4, changed_words[0].rect.x0 - prefix_word.rect.x1)
        right_gap = typical_gap
        if changed_words and suffix_word is not None:
            right_gap = max(.4, suffix_word.rect.x0
                            - changed_words[-1].rect.x1)
        collapse_gap = statistics.median([left_gap, right_gap])

        if changed_words:
            new_x = changed_words[0].rect.x0
        elif prefix_word is not None:
            new_x = prefix_word.rect.x1 + collapse_gap
        elif suffix_word is not None:
            new_x = hit.rect.x0
        else:
            new_x = hit.rect.x0

        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        reference_pool = changed_words or ([prefix_word] if prefix_word else []) \
            or ([suffix_word] if suffix_word else []) or old_words
        reference = character_reference or max(
            reference_pool, key=lambda word: len(word.text), default=None)
        font = self._scan_edit_font(hit.style, reference, scale_x, scale_y)
        metrics = QFontMetricsF(font)
        new_width = metrics.horizontalAdvance(changed_text) / scale_x \
            if changed_text else 0.0

        suffix_width = (hit.rect.x1 - suffix_word.rect.x0
                        if suffix_word is not None else 0.0)
        layout = hit.layout_rect
        strict_container = (hit.align != 0
                            or abs(layout.x0 - hit.rect.x0) > 1.2
                            or layout.width > hit.rect.width * 1.12)
        if strict_container:
            right_limit = layout.x1
        else:
            page_right = self.doc[page_no].rect.x1 - 6.0
            right_limit = min(page_right,
                              max(layout.x1,
                                  hit.rect.x1 + max(18.0,
                                                    hit.rect.height * 3.0)))
        available_for_change = (right_limit - new_x - suffix_width
                                - (right_gap if suffix_word else 0.0))
        if changed_text and 1.0 < available_for_change < new_width:
            current_stretch = max(1, font.stretch())
            fitted_stretch = round(current_stretch
                                   * available_for_change / new_width)
            font.setStretch(max(58, fitted_stretch))
            metrics = QFontMetricsF(font)
            new_width = metrics.horizontalAdvance(changed_text) / scale_x

        # Tek parçalık ortalı / sağa yaslı hücre metninde hücre hizasını koru.
        if prefix_word is None and suffix_word is None and changed_text:
            if hit.align == 1:
                new_x = layout.x0 + max(0.0, (layout.width - new_width) / 2)
            elif hit.align == 2:
                new_x = layout.x1 - new_width

        glyph_draws: list[tuple[QImage | None, float, float, str]] = []
        glyph_coverage = 0.0
        if changed_text:
            glyph_draws, glyph_coverage = self._source_glyph_draws(
                page_no, hit, changed_text, font, source_image, image_rect)
        self._last_scan_glyph_coverage = glyph_coverage
        use_source_glyphs = bool(glyph_draws) and glyph_coverage >= .50
        if use_source_glyphs:
            # TTF metriği yalnız yerleşim iskeletidir. Sonraki kelimeyi gerçek
            # klonlanmış piksellerin sağ kenarından sonra başlat; glif taşması
            # kelime aralığını yiyemez.
            real_sprites = [(sprite, offset_x) for sprite, offset_x, _offset_y, _char
                            in glyph_draws if sprite is not None]
            if real_sprites:
                glyph_right_px = max(round(offset_x) + sprite.width()
                                     for sprite, offset_x in real_sprites)
                new_width = max(new_width, glyph_right_px / scale_x)
        log.info("scan edit glyph coverage=%.3f source_pixels=%s text=%r",
                 glyph_coverage, use_source_glyphs, changed_text)

        if suffix_word is not None:
            if changed_text:
                suffix_target_x = new_x + new_width + right_gap
            elif prefix_word is not None:
                suffix_target_x = prefix_word.rect.x1 + collapse_gap
            else:
                suffix_target_x = hit.rect.x0
        else:
            suffix_target_x = 0.0

        pad_x = min(.75, hit.rect.height * .07)
        pad_y = min(.60, hit.rect.height * .055)
        erase_rect = fitz.Rect(hit.rect.x0 - pad_x, hit.rect.y0 - pad_y,
                               hit.rect.x1 + pad_x, hit.rect.y1 + pad_y)
        erase_rect &= image_rect
        if erase_rect.is_empty:
            raise ValueError("Düzenlenecek tarama satırı görüntü dışında.")

        prefix_copy: tuple[QImage, int, int] | None = None
        if prefix_word is not None:
            next_x = (old_words[prefix_count].rect.x0
                      if prefix_count < len(old_words) else erase_rect.x1)
            prefix_cut = min(erase_rect.x1,
                             (prefix_word.rect.x1 + next_x) / 2)
            prefix_rect = fitz.Rect(erase_rect.x0, erase_rect.y0,
                                    prefix_cut, erase_rect.y1)
            px0, py0, px1, py1 = self._image_rect_for_pdf(
                prefix_rect, image_rect, image)
            if px1 > px0 and py1 > py0:
                prefix_copy = (image.copy(px0, py0, px1 - px0, py1 - py0),
                               px0, py0)

        suffix_copy: tuple[QImage, int, int, float] | None = None
        if suffix_word is not None:
            previous_x = (old_words[suffix_index - 1].rect.x1
                          if suffix_index > 0 else erase_rect.x0)
            suffix_cut = max(erase_rect.x0,
                             (previous_x + suffix_word.rect.x0) / 2)
            suffix_rect = fitz.Rect(suffix_cut, erase_rect.y0,
                                    erase_rect.x1, erase_rect.y1)
            sx0, sy0, sx1, sy1 = self._image_rect_for_pdf(
                suffix_rect, image_rect, image)
            if sx1 > sx0 and sy1 > sy0:
                suffix_copy = (
                    image.copy(sx0, sy0, sx1 - sx0, sy1 - sy0),
                    sx0, sy0, suffix_word.rect.x0 - suffix_cut)

        page = self.doc[page_no]
        had_vector_text = bool(page.get_text("text", clip=erase_rect).strip())
        vector_clip = erase_rect
        if changed_text:
            vector_clip = fitz.Rect(new_x, hit.rect.y0, new_x + new_width, hit.rect.y1)
            vector_clip = fitz.Rect(vector_clip.x0 - .4, vector_clip.y0 - .25,
                                    vector_clip.x1 + .4, vector_clip.y1 + .25)
            vector_clip &= image_rect
        if not self._repair_qimage_region(image, image_rect, erase_rect):
            raise RuntimeError("Tarama satırının zemini onarılamadı.")

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if prefix_copy is not None:
            prefix_image, destination_x, destination_y = prefix_copy
            painter.drawImage(QPointF(destination_x, destination_y),
                              prefix_image)
        if suffix_copy is not None:
            suffix_image, _source_x, destination_y, source_offset = suffix_copy
            target_crop_x = suffix_target_x - source_offset
            destination_x = (target_crop_x - image_rect.x0) * scale_x
            painter.drawImage(QPointF(round(destination_x), round(destination_y)),
                              suffix_image)
        if changed_text:
            baseline = hit.baseline
            if baseline is None:
                baseline = hit.rect.y1 - max(.5, hit.rect.height * .10)
            text_x_px = (new_x - image_rect.x0) * scale_x
            baseline_px = (baseline - image_rect.y0) * scale_y
            if use_source_glyphs:
                # Hibrit çizim: kaynak pikseli bulunan glifler aynen klonlanır,
                # bulunamayanlar font fallback ile karakter karakter çizilir.
                painter.setFont(font)
                r, g, b = (max(0, min(255, round(value * 255)))
                           for value in hit.style.color)
                painter.setPen(QColor(r, g, b))
                for sprite, offset_x, offset_y, char in glyph_draws:
                    if sprite is not None:
                        painter.drawImage(
                            QPointF(round(text_x_px + offset_x),
                                    round(baseline_px + offset_y)), sprite)
                    else:
                        # Font fallback: bu karakteri sistem fontuyla çiz
                        painter.drawText(
                            QPointF(text_x_px + offset_x, baseline_px), char)
            else:
                painter.setFont(font)
                r, g, b = (max(0, min(255, round(value * 255)))
                           for value in hit.style.color)
                painter.setPen(QColor(r, g, b))
                painter.drawText(QPointF(text_x_px, baseline_px), changed_text)
        painter.end()

        page = self.doc[page_no]
        self._replace_scan_image_in_place(
            page_no, base_xref, image_rect, image)
        if had_vector_text and not vector_clip.is_empty:
            try:
                page.add_redact_annot(vector_clip, fill=None, cross_out=False)
                page.apply_redactions(images=0, graphics=0, text=0)
            except Exception as e:
                log.warning("vector redaction on scan page failed, ignoring: %s", e)
            finally:
                try:
                    self._replace_scan_image_in_place(
                        page_no, base_xref, image_rect, image)
                except Exception as e:
                    log.warning("scan in-place update after redaction failed: %s", e)
        self.dirty = True
        self._segmented_word_glyph_cache.clear()
        self._commit_current_ocr_line(
            page_no, hit, new_text, image, image_rect)
        return font.pixelSize() / scale_y if changed_text else None

    def _block_text_metrics(self, page_no: int, block: TextBlockHit):
        """Ham metin ölçer ve her özgün satırın ayrı genişlik ölçeğini döndürür."""
        assert self.doc is not None
        scan = (self._full_page_scan_qimage(page_no)
                if block.source == "ocr" else None)
        if scan is not None:
            _xref, image_rect, image = scan
            scale_x = image.width() / image_rect.width
            scale_y = image.height() / image_rect.height
            words = [word for line in block.lines for word in line.words
                     if word.text]
            reference = max(words, key=lambda word: len(word.text), default=None)
            font = self._scan_edit_font(block.style, reference, scale_x, scale_y)
            metrics = QFontMetricsF(font)

            def raw_width(value: str) -> float:
                return metrics.horizontalAdvance(value) / scale_x
        else:
            page = self.doc[page_no]
            kwargs = self._font_kwargs_for(page, page_no, block.style, block.text)

            def raw_width(value: str) -> float:
                return self._font_text_width(kwargs, value, block.style.size)

        ratios: list[float] = []
        for line in block.lines:
            measured = raw_width(line.text)
            factor = line.rect.width / measured if measured > .5 else 1.0
            ratios.append(max(.72, min(1.38, factor)))
        return raw_width, ratios

    def _wrap_text_block(self, page_no: int, block: TextBlockHit,
                         text: str) -> list[str]:
        """Metni gercek kaynak kelime genislikleriyle paragrafta sarar."""
        measure, _line_scales = self._block_text_metrics(page_no, block)
        common_right = max(line.layout_rect.x1 for line in block.lines)
        common_left = statistics.median(line.layout_rect.x0
                                        for line in block.lines)

        def capacity(index: int) -> float:
            left = (block.lines[index].layout_rect.x0
                    if index < len(block.lines) else common_left)
            return max(8.0, common_right - left)

        old_records = [(line, word) for line in block.lines
                       for word in line.words if word.text]
        old_tokens = [word.text for _line, word in old_records]
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        new_tokens = [token for paragraph in normalized.split("\n")
                      for token in paragraph.split()]
        mapped_widths: dict[int, float] = {}
        matcher = difflib.SequenceMatcher(
            None, old_tokens, new_tokens, autojunk=False)
        for tag, old_start, old_end, new_start, _new_end in matcher.get_opcodes():
            if tag != "equal":
                continue
            for offset in range(old_end - old_start):
                mapped_widths[new_start + offset] = (
                    old_records[old_start + offset][1].rect.width)

        calibrations = []
        for _line, word in old_records:
            raw = measure(word.text)
            if raw > .1:
                calibrations.append(word.rect.width / raw)
        metric_scale = (statistics.median(calibrations)
                        if calibrations else 1.0)
        metric_scale = max(.68, min(1.42, metric_scale))
        positive_gaps = [right.rect.x0 - left.rect.x1
                         for line in block.lines
                         for left, right in zip(line.words, line.words[1:])
                         if right.rect.x0 > left.rect.x1]
        typical_gap = (statistics.median(positive_gaps) if positive_gaps
                       else max(1.0, block.lines[0].rect.height * .22))

        # Sadece kelime degistirildiyse once mevcut satir kirilimlarini koru.
        # Justified bir paragrafta uzayan kelime satira hala sigiyorsa Adobe de
        # diger 13 satiri reflow etmez; yalniz o satirin kelime araliklari biraz
        # daralir. Bu ayni zamanda degismeyen satir piksellerini birebir korur.
        old_line_counts = [len([word for word in line.words if word.text])
                           for line in block.lines]
        if (sum(old_line_counts) == len(new_tokens)
                and normalized.count("\n") == block.text.count("\n")):
            preserved: list[str] = []
            cursor = 0
            fits = True
            for line_index, count in enumerate(old_line_counts):
                line_tokens = new_tokens[cursor:cursor + count]
                widths = [mapped_widths.get(
                    cursor + offset,
                    max(.5, measure(token) * metric_scale))
                    for offset, token in enumerate(line_tokens)]
                natural = sum(widths) + .65 * max(0, len(widths) - 1)
                if natural > capacity(line_index) + .35:
                    fits = False
                    break
                preserved.append(" ".join(line_tokens))
                cursor += count
            if fits and cursor == len(new_tokens):
                return preserved

        wrapped: list[str] = []
        hard_lines = normalized.split("\n")
        token_cursor = 0
        for hard_index, hard_line in enumerate(hard_lines):
            words = hard_line.split()
            if not words:
                if 0 < hard_index < len(hard_lines) - 1:
                    wrapped.append("")
                continue
            current = ""
            current_width = 0.0
            is_single_line = len(block.lines) == 1
            is_table_cell = any(getattr(line, "in_table_cell", False) for line in block.lines)
            for word in words:
                word_width = mapped_widths.get(
                    token_cursor, max(.5, measure(word) * metric_scale))
                token_cursor += 1
                line_index = len(wrapped)
                candidate_width = (word_width if not current else
                                   current_width + typical_gap + word_width)
                if (current and not (is_single_line or is_table_cell)
                        and candidate_width > capacity(line_index) + .35):
                    wrapped.append(current)
                    current = word
                    current_width = word_width
                else:
                    current = word if not current else current + " " + word
                    current_width = candidate_width
            if current:
                wrapped.append(current)
        while wrapped and not wrapped[-1]:
            wrapped.pop()
        return wrapped or [""]

    def _synthetic_block_line(self, page_no: int, block: TextBlockHit,
                              index: int) -> TextHit:
        """Paragraf uzarsa aynı satır aralığı/stille yeni bir boş hedef üretir."""
        assert self.doc is not None
        lines = block.lines
        centers = [(line.rect.y0 + line.rect.y1) / 2 for line in lines]
        steps = [right - left for left, right in zip(centers, centers[1:])
                 if right - left > 1.0]
        step = statistics.median(steps) if steps else lines[-1].rect.height * 1.28
        height = statistics.median(line.rect.height for line in lines)
        common_left = statistics.median(line.layout_rect.x0 for line in lines)
        common_right = max(line.layout_rect.x1 for line in lines)
        y0 = lines[-1].rect.y0 + step * (index - len(lines) + 1)
        rect = fitz.Rect(common_left, y0, common_right, y0 + height)
        baseline_offsets = [line.baseline - line.rect.y0
                            for line in lines if line.baseline is not None]
        baseline_offset = (statistics.median(baseline_offsets)
                           if baseline_offsets else height * .9)
        baseline = y0 + baseline_offset
        template_word = max(
            (word for line in lines for word in line.words if word.text),
            key=lambda word: len(word.text), default=None)
        words: list[TextWord] = []
        seed_text = ""
        if template_word is not None:
            seed_text = template_word.text
            seed_width = min(rect.width, template_word.rect.width)
            words = [TextWord(fitz.Rect(rect.x0, rect.y0,
                                        rect.x0 + seed_width, rect.y1),
                              seed_text, template_word.confidence)]
        return TextHit(
            rect=rect, text=seed_text, style=lines[-1].style, words=words,
            source=lines[-1].source, baseline=baseline,
            container_rect=fitz.Rect(rect), block_rect=fitz.Rect(block.rect),
            background=self._sample_background(page_no, rect), align=0)

    def _block_bottom_limit(self, page_no: int, block: TextBlockHit) -> float:
        assert self.doc is not None
        limit = self.doc[page_no].rect.y1 - 6.0
        if block.source == "ocr":
            block_ids = {id(line) for line in block.lines}
            following = [line.rect.y0 for line in self._ocr_lines.get(page_no, ())
                         if id(line) not in block_ids
                         and line.rect.y0 > block.rect.y1 + 1.0]
            if following:
                limit = min(limit, min(following) - 2.0)
        return limit

    def _scan_word_sprite(self, image: QImage, image_rect: fitz.Rect,
                          line: TextHit, word: TextWord
                          ) -> tuple[QImage, float, float] | None:
        """Özgün bir kelimeyi yeniden fontlamadan şeffaf piksel parçası yapar."""
        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        x0, y0, x1, y1 = self._image_rect_for_pdf(word.rect, image_rect, image)
        sprite_info = self._transparent_glyph_sprite(
            image, (x0, y0, x1, y1), line.background, line.style.color)
        if sprite_info is None:
            return None
        sprite, trim_left, trim_top = sprite_info
        baseline = line.baseline
        if baseline is None:
            baseline = line.rect.y1 - max(.5, line.rect.height * .10)
        source_top = y0 + trim_top
        ascent = (baseline - image_rect.y0) * scale_y - source_top
        left_bearing = trim_left / scale_x
        return sprite, left_bearing, ascent

    def _replace_scan_text_block(self, page_no: int, block: TextBlockHit,
                                 wrapped: list[str], flow_text: str
                                 ) -> float | None:
        """Paragraf akışını değişmeyen kelimeleri özgün piksel olarak taşıyarak kurar."""
        assert self.doc is not None
        scan = self._full_page_scan_qimage(page_no)
        if scan is None:
            raise ValueError("Taranmış paragrafın piksel kaynağı bulunamadı.")
        base_xref, image_rect, base_image = scan
        scale_x = base_image.width() / image_rect.width
        scale_y = base_image.height() / image_rect.height
        source_scan = self._source_scan_qimage(page_no)
        if source_scan is None:
            raise ValueError("Taranmış paragrafın değişmez kaynağı bulunamadı.")
        _source_xref, _source_rect, glyph_source_image = source_scan

        targets = list(block.lines)
        for index in range(len(targets), len(wrapped)):
            target = self._synthetic_block_line(page_no, block, index)
            if target.rect.y1 > self._block_bottom_limit(page_no, block):
                raise ValueError(
                    "Yeni metin paragrafın altındaki içeriğe taşıyor. "
                    "Metni kısaltın veya nesne alanını büyütün.")
            targets.append(target)

        affected = [index for index, target in enumerate(targets)
                    if (wrapped[index] if index < len(wrapped) else "")
                    != (block.lines[index].text
                        if index < len(block.lines) else "")]
        if not affected:
            return None

        source_image = base_image

        old_records: list[tuple[TextHit, TextWord]] = [
            (line, word) for line in block.lines for word in line.words
            if word.text]
        old_tokens = [word.text for _line, word in old_records]
        new_tokens = [token for line in wrapped for token in line.split()]
        mapped: dict[int, tuple[TextHit, TextWord]] = {}
        matcher = difflib.SequenceMatcher(
            None, old_tokens, new_tokens, autojunk=False)
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag != "equal":
                continue
            for offset in range(old_end - old_start):
                mapped[new_start + offset] = old_records[old_start + offset]

        positive_gaps = [
            right.rect.x0 - left.rect.x1
            for line in block.lines
            for left, right in zip(line.words, line.words[1:])
            if .15 < right.rect.x0 - left.rect.x1 < line.rect.height * 2.5]
        typical_gap = (statistics.median(positive_gaps) if positive_gaps
                       else max(1.2, block.lines[0].rect.height * .24))
        common_right = max(line.layout_rect.x1 for line in block.lines)

        paragraph_ends: set[int] = set()
        token_total = 0
        for paragraph in flow_text.replace("\r\n", "\n").replace(
                "\r", "\n").split("\n"):
            token_total += len(paragraph.split())
            paragraph_ends.add(token_total)

        image = source_image.copy()
        patch_rect: fitz.Rect | None = None
        
        # Pass 1: Prepare render items and calculate bounds
        token_cursor = 0
        used_sizes: list[float] = []
        lines_data = []
        
        for line_index, line_text in enumerate(wrapped):
            tokens = line_text.split()
            global_indices = list(range(token_cursor, token_cursor + len(tokens)))
            token_cursor += len(tokens)
            if line_index not in affected:
                continue
            
            target = targets[line_index]
            baseline = target.baseline
            if baseline is None:
                baseline = target.rect.y1 - max(.5, target.rect.height * .10)
            reference = max(target.words, key=lambda word: len(word.text),
                            default=max((word for _line, word in old_records),
                                        key=lambda word: len(word.text),
                                        default=None))
            font = self._scan_edit_font(
                block.style, reference, scale_x, scale_y)
            metrics = QFontMetricsF(font)
            used_sizes.append(font.pixelSize() / scale_y)

            render_items = []
            widths: list[float] = []
            for token, global_index in zip(tokens, global_indices):
                source = mapped.get(global_index)
                if source is not None:
                    source_line, source_word = source
                    sprite = self._scan_word_sprite(
                        source_image, image_rect, source_line, source_word)
                    if sprite is not None:
                        render_items.append(("word", sprite, None))
                        widths.append(source_word.rect.width)
                        continue
                glyphs, coverage = self._source_glyph_draws(
                    page_no, target, token, font,
                    glyph_source_image, image_rect)
                glyph_width = 0.0
                if glyphs:
                    real_sprites = [(sprite, offset_x) for sprite, offset_x, _offset_y, _char
                                    in glyphs if sprite is not None]
                    if real_sprites:
                        glyph_width = max(offset_x + sprite.width()
                                          for sprite, offset_x in real_sprites) / scale_x
                width = max(metrics.horizontalAdvance(token) / scale_x,
                            glyph_width)
                render_items.append(("glyph", glyphs, token))
                widths.append(width)
                self._last_scan_glyph_coverage = coverage

            available = max(1.0, common_right - target.rect.x0)
            last_global = global_indices[-1] + 1 if global_indices else token_cursor
            is_paragraph_last = (not tokens or last_global in paragraph_ends)
            if len(tokens) > 1 and not is_paragraph_last:
                justified = (available - sum(widths)) / (len(tokens) - 1)
                # Yanlis OCR/sarma nedeniyle iki kelimeyi sayfanin iki ucuna
                # firlatacak bosluklara izin verme. Normal justify araligi yine
                # korunur, aykiri durumda kaynak kelime araligina donulur.
                max_gap = max(typical_gap * 2.8, target.rect.height * .72)
                gap = max(.65, min(justified, max_gap))
            else:
                gap = typical_gap

            lines_data.append((line_index, target, tokens, render_items, widths, gap, font, baseline))

        # Pass 2: Erase backgrounds
        for data in lines_data:
            target = data[1]
            widths = data[4]
            gap = data[5]
            total_width = sum(widths) + gap * max(0, len(widths) - 1)
            target_right = target.rect.x0 + total_width
            erase_right = max(common_right, target_right)
            erase = fitz.Rect(target.rect.x0 - .75, target.rect.y0 - .6,
                              erase_right + .75, target.rect.y1 + .6) & image_rect
            if not erase.is_empty:
                if not self._repair_qimage_region(image, image_rect, erase):
                    pass
                if patch_rect is None:
                    patch_rect = fitz.Rect(erase)
                else:
                    patch_rect.include_rect(erase)

        # Pass 3: Draw items
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        
        rendered_words_by_line: dict[int, list[TextWord]] = {}
        for data in lines_data:
            line_index, target, tokens, render_items, widths, gap, font, baseline = data
            baseline_px = (baseline - image_rect.y0) * scale_y
            x = target.rect.x0
            rendered_words: list[TextWord] = []
            for token, width, item in zip(tokens, widths, render_items):
                kind, payload, fallback_text = item
                x_px = (x - image_rect.x0) * scale_x
                if kind == "word":
                    sprite, left_bearing, ascent = payload
                    painter.drawImage(
                        QPointF(round(x_px + left_bearing * scale_x),
                                round(baseline_px - ascent)), sprite)
                elif payload:
                    painter.setFont(font)
                    r, g, b = (max(0, min(255, round(value * 255)))
                               for value in block.style.color)
                    painter.setPen(QColor(r, g, b))
                    for sprite, offset_x, offset_y, char in payload:
                        if sprite is not None:
                            painter.drawImage(
                                QPointF(round(x_px + offset_x),
                                        round(baseline_px + offset_y)), sprite)
                        else:
                            painter.drawText(
                                QPointF(x_px + offset_x, baseline_px), char)
                else:
                    painter.setFont(font)
                    r, g, b = (max(0, min(255, round(value * 255)))
                               for value in block.style.color)
                    painter.setPen(QColor(r, g, b))
                    painter.drawText(QPointF(x_px, baseline_px), fallback_text)
                rendered_words.append(TextWord(
                    fitz.Rect(x, target.rect.y0, x + width, target.rect.y1),
                    token, target.confidence))
                x += width + gap
            rendered_words_by_line[line_index] = rendered_words
        painter.end()

        if patch_rect is None:
            return None
        self._replace_scan_image_in_place(
            page_no, base_xref, image_rect, image)
        self.dirty = True
        self._segmented_word_glyph_cache.clear()

        old_lines = list(block.lines)
        for index, target in enumerate(targets):
            replacement = wrapped[index] if index < len(wrapped) else ""
            words = rendered_words_by_line.get(index)
            if words is None and replacement == target.text:
                words = [TextWord(fitz.Rect(word.rect), word.text,
                                  word.confidence) for word in target.words]
            self._commit_current_ocr_line(
                page_no, target, replacement, image, image_rect, words)
        kept_ids = {id(target) for target in targets[:len(wrapped)]}
        self._ocr_lines[page_no] = [
            line for line in self._ocr_lines.get(page_no, [])
            if id(line) in kept_ids or all(line is not old for old in old_lines)]
        new_block_lines = [target for target in targets[:len(wrapped)]
                           if target.text.strip()]
        if new_block_lines:
            new_block_rect = fitz.Rect(new_block_lines[0].rect)
            for target in new_block_lines[1:]:
                new_block_rect.include_rect(target.rect)
            for target in new_block_lines:
                target.block_rect = fitz.Rect(new_block_rect)
        self._ocr_lines[page_no].sort(
            key=lambda item: (item.rect.y0, item.rect.x0))
        self._ocr_complete.add(page_no)
        return statistics.median(used_sizes) if used_sizes else None

    def replace_text_block(self, page_no: int, block: TextBlockHit,
                           new_text: str) -> float | None:
        """Paragrafı atomik olarak yeniden sarar ve satır satır kaynak stilinde işler."""
        assert self.doc is not None
        if new_text == block.text:
            return None
        wrapped = self._wrap_text_block(page_no, block, new_text)
        targets: list[TextHit] = list(block.lines)
        for index in range(len(targets), len(wrapped)):
            target = self._synthetic_block_line(page_no, block, index)
            if target.rect.y1 > self._block_bottom_limit(page_no, block):
                raise ValueError(
                    "Yeni metin paragrafın altındaki içeriğe taşıyor. "
                    "Metni kısaltın veya nesne alanını büyütün.")
            targets.append(target)

        original = self._capture_undo_state()
        undo_start = len(self._undo_stack)
        used_sizes: list[float] = []
        scan_edit = (block.source == "ocr"
                     and self._full_page_scan_image(page_no) is not None)
        try:
            if scan_edit:
                size = self._replace_scan_text_block(
                    page_no, block, wrapped, new_text)
                if size is not None:
                    used_sizes.append(size)
            else:
                page = self.doc[page_no]
                # 1. Aşama: Orijinal bloktaki TÜM satırları tek seferde topluca temizle (batch redaction).
                # Ardışık satır satır silme/yazma döngüsü PyMuPDF içerik akışını bozar ve komşu satırları ezer.
                # Toplu silme sayesinde eski metin tamamen silinir, yeni metin temiz alana yazılır.
                for orig_line in block.lines:
                    text_height = max(orig_line.rect.height, 1.0)
                    pad_x = min(1.2, text_height * .08)
                    pad_y = min(1.0, text_height * .08)
                    erase_rect = fitz.Rect(orig_line.rect.x0 - pad_x,
                                           orig_line.rect.y0 - pad_y,
                                           orig_line.rect.x1 + pad_x,
                                           orig_line.rect.y1 + pad_y)
                    page.add_redact_annot(erase_rect, fill=None, cross_out=False)
                page.apply_redactions(images=2, graphics=0, text=0)
                page = self.doc[page_no]

                # 2. Aşama: Yeni sarılmış satırları temiz alana yerleştir
                for index in range(len(wrapped)):
                    replacement = wrapped[index]
                    if not replacement.strip():
                        continue
                    target = targets[index]
                    target_rect = fitz.Rect(target.layout_rect or target.rect)
                    effective_style = target.style
                    effective_size = float(effective_style.size)
                    kwargs = self._font_kwargs_for(page, page_no, effective_style, replacement)
                    size = self._insert_fitted_line(
                        page, target_rect, replacement, effective_style, kwargs,
                        target.baseline, target.align, effective_size)
                    if size is not None:
                        used_sizes.append(size)

            # Alt çağrıların oluşturduğu ayrı undo kayıtlarını tek paragraf işlemi
            # altında birleştir; Ctrl+Z bütün akışı tek seferde geri alsın.
            del self._undo_stack[undo_start:]
            self._undo_stack.append(original)
            if len(self._undo_stack) > MAX_UNDO:
                self._undo_stack.pop(0)
            self.dirty = True
            if not scan_edit:
                self._invalidate_page_analysis(page_no)
            return statistics.median(used_sizes) if used_sizes else None
        except Exception:
            del self._undo_stack[undo_start:]
            self._restore_undo_state(original)
            raise

    def replace_text_hit(self, page_no: int, hit: TextHit,
                         new_text: str) -> float | None:
        """Kaynağa göre en az bozucu satır değiştirme yolunu seçer."""
        assert self.doc is not None
        if (hit.source == "ocr" and hit.words
                and self._full_page_scan_image(page_no) is not None):
            was_dirty = self.dirty
            undo_size = len(self._undo_stack)
            self._snapshot()
            try:
                return self._replace_scan_text_hit(page_no, hit, new_text)
            except Exception:
                if len(self._undo_stack) > undo_size:
                    self._rollback_edit(was_dirty)
                raise

        if hit.source == "blank-cell":
            erase_rect = fitz.Rect(hit.rect.x0, hit.rect.y0,
                                   hit.rect.x0, hit.rect.y0)
        else:
            text_height = max(hit.rect.height, 1.0)
            pad_x = min(1.2, text_height * .08)
            pad_y = min(1.0, text_height * .08)
            erase_rect = fitz.Rect(hit.rect.x0 - pad_x,
                                   hit.rect.y0 - pad_y,
                                   hit.rect.x1 + pad_x,
                                   hit.rect.y1 + pad_y)
        return self.replace_text(
            page_no, erase_rect, new_text, style=hit.style,
            layout_rect=hit.layout_rect, baseline=hit.baseline,
            align=hit.align)

    def replace_text(self, page_no: int, rect: fitz.Rect, new_text: str,
                     style: TextStyle | None = None,
                     fontsize: float | None = None,
                     color: tuple[float, float, float] | None = None,
                     layout_rect: fitz.Rect | None = None,
                     baseline: float | None = None,
                     align: int = 0) -> float | None:
        """Eski satırı kaldırır ve yeni satırı analiz edilen alana birebir yerleştirir.

        Stil koruması öncelikli: kullanıcı tarafından açıkça fontsize / color
        verilmemişse orijinal style'daki değerleri olduğu gibi korur. Redaction
        yalnızca etkilenen bölgeye uygulanır, komşu satırlara dokunulmaz.
        """
        assert self.doc is not None
        if style is None:
            style = self.style_in_rect(page_no, rect)
        effective_size = float(fontsize) if fontsize is not None else float(style.size)
        effective_style = style
        if color is not None:
            effective_style = TextStyle(style.fontname, style.size, color, style.flags)
            effective_size = float(style.size)
        target_rect = fitz.Rect(layout_rect or rect)
        was_dirty = self.dirty
        self._snapshot()
        try:
            page = self.doc[page_no]
            kwargs = self._font_kwargs_for(page, page_no, effective_style, new_text) \
                if new_text.strip() else {}
            if not rect.is_empty and rect.get_area() >= .05:
                self._remove_content(page_no, rect)
                page = self.doc[page_no]
            used_size = None
            if new_text.strip():
                log.debug("replace: erase=%s layout=%s size=%s font=%s file=%s",
                          rect, target_rect, effective_size,
                          kwargs.get("fontname"), kwargs.get("fontfile"))
                used_size = self._insert_fitted_line(
                    page, target_rect, new_text, effective_style, kwargs,
                    baseline, align, effective_size)
            self.dirty = True
            self._invalidate_page_analysis(page_no, rect)
            return used_size
        except Exception:
            self._rollback_edit(was_dirty)
            raise

    def _transparent_region_png(self, pixmap: fitz.Pixmap,
                                background: tuple[int, int, int]) -> bytes:
        """Taşınan bölgenin kâğıt zeminini saydamlaştırır."""
        image = QImage.fromData(pixmap.tobytes("png"))
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        memory = self._image_memory(image)
        stride = image.bytesPerLine()
        br, bg, bb = background
        feather = max(5.0, min(image.width(), image.height()) * .045)
        for y in range(image.height()):
            for x in range(image.width()):
                index = y * stride + x * 4
                r, g, b = memory[index], memory[index + 1], memory[index + 2]
                distance = max(abs(r - br), abs(g - bg), abs(b - bb))
                content_alpha = max(0, min(255, (distance - 7) * 9))
                edge_distance = min(x, y, image.width() - 1 - x,
                                    image.height() - 1 - y)
                paper_alpha = round(255 * min(1.0, edge_distance / feather))
                alpha = max(content_alpha, paper_alpha)
                memory[index + 3] = alpha
        return self._qimage_png_bytes(image)

    def _overlay_image_for_rect(self, page_no: int, rect: fitz.Rect
                                ) -> tuple[int, fitz.Rect, bytes] | None:
        """Daha önce taşınmış şeffaf nesneyi, alttaki sayfadan ayrı bulur."""
        assert self.doc is not None
        scan = self._full_page_scan_image(page_no)
        background_xref = scan[0] if scan is not None else None
        page = self.doc[page_no]
        best = None
        best_pixels = 0
        for image in page.get_images(full=True):
            xref = int(image[0])
            smask = int(image[1])
            if xref == background_xref:
                continue
            try:
                rects = page.get_image_rects(xref)
                pixmap = fitz.Pixmap(self.doc, xref)
                if smask > 0:
                    mask = fitz.Pixmap(self.doc, smask)
                    pixmap = fitz.Pixmap(pixmap, mask)
                if pixmap.colorspace not in (fitz.csRGB, fitz.csGRAY) or pixmap.n >= 5:
                    pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
            except Exception:
                continue
            pixels = pixmap.width * pixmap.height
            if pixels <= best_pixels or pixels <= 4:
                continue
            for item in rects:
                image_rect = fitz.Rect(item)
                if image_rect.is_empty:
                    continue
                overlap = (image_rect & rect).get_area()
                if overlap >= min(image_rect.get_area(), rect.get_area()) * .72:
                    best = (xref, image_rect, pixmap.tobytes("png"))
                    best_pixels = pixels
                    break
        return best

    def move_region(self, page_no: int, source_rect: fitz.Rect,
                    target_rect: fitz.Rect) -> None:
        """Seçili görünümü aynı sayfada taşır/ölçekler ve eski yeri dokuyla onarır."""
        assert self.doc is not None
        source = fitz.Rect(source_rect) & self.doc[page_no].rect
        target = fitz.Rect(target_rect) & self.doc[page_no].rect
        if source.is_empty or target.is_empty:
            raise ValueError("Taşınacak alan geçersiz.")
        was_dirty = self.dirty
        self._snapshot()
        try:
            page = self.doc[page_no]
            overlay = self._overlay_image_for_rect(page_no, source)
            if overlay is not None:
                content_png = overlay[2]
            else:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=source,
                                         alpha=False, colorspace=fitz.csRGB)
                background = self._sample_background(page_no, source)
                content_png = self._transparent_region_png(pixmap, background)
            self._remove_content(page_no, source)
            page = self.doc[page_no]
            page.insert_image(target, stream=content_png, overlay=True,
                              keep_proportion=False)
            self.dirty = True
            self._invalidate_page_analysis(page_no)
        except Exception:
            self._rollback_edit(was_dirty)
            raise

    def insert_image(self, page_no: int, rect: fitz.Rect,
                     image_path_or_bytes: str | bytes) -> fitz.Rect:
        """Sayfaya yeni bir görsel ekler ve geri alma kaydı oluşturur."""
        assert self.doc is not None
        if not (0 <= page_no < len(self.doc)):
            raise ValueError(f"Geçersiz sayfa: {page_no}")
        page = self.doc[page_no]
        target = fitz.Rect(rect) & page.rect
        if target.is_empty:
            raise ValueError("Görsel alanı geçersiz.")
        was_dirty = self.dirty
        self._snapshot()
        try:
            if isinstance(image_path_or_bytes, str):
                page.insert_image(target, filename=image_path_or_bytes,
                                  overlay=True, keep_proportion=True)
            else:
                page.insert_image(target, stream=image_path_or_bytes,
                                  overlay=True, keep_proportion=True)
            self.dirty = True
            self._invalidate_page_analysis(page_no)
            return target
        except Exception:
            self._rollback_edit(was_dirty)
            raise
