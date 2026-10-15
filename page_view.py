"""Sürekli kaydırmalı (Adobe tarzı) PDF görünümü.

Tüm sayfalar alt alta dizilir; mouse tekerleğiyle doğal kaydırma yapılır.
Sayfalar tembel (lazy) render edilir: sadece görünür bölgeye yakın sayfalar
piksel olarak üretilir, uzaktakiler bellekten bırakılır.
"""
from __future__ import annotations

import logging
import statistics

import fitz
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal

log = logging.getLogger("pdfeditor")
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                           QPainter, QPen, QPixmap, QTextBlockFormat,
                           QTextCursor, QTextOption)
from PySide6.QtWidgets import (QLabel, QLineEdit, QScrollArea, QTextEdit,
                               QVBoxLayout, QWidget)

PAGE_GAP = 14          # sayfalar arası boşluk (px)
RENDER_MARGIN = 1.0    # görünür alanın kaç viewport yüksekliği çevresi render edilsin
DROP_MARGIN = 3.0      # bu mesafenin ötesindeki pixmap'ler bellekten bırakılır

# PDF font adı -> sistemdeki Qt font ailesi (inline editörün görünümü için)
_FAMILY_MAP = [
    ("times", "Times New Roman"), ("arial", "Arial"), ("helvetica", "Arial"),
    ("courier", "Courier New"), ("calibri", "Calibri"), ("verdana", "Verdana"),
    ("georgia", "Georgia"), ("tahoma", "Tahoma"), ("segoe", "Segoe UI"),
    ("cambria", "Cambria"), ("consolas", "Consolas"), ("trebuchet", "Trebuchet MS"),
    ("palatino", "Palatino Linotype"), ("book antiqua", "Book Antiqua"),
    ("garamond", "Garamond"), ("century", "Century Gothic"),
    ("franklin", "Franklin Gothic Medium"), ("impact", "Impact"),
    ("lucida", "Lucida Console"), ("comic", "Comic Sans MS"),
    ("candara", "Candara"), ("corbel", "Corbel"), ("constantia", "Constantia"),
    ("aptos", "Aptos"), ("sylfaen", "Sylfaen"),
]


def _qfont_for_style(style, zoom: float) -> QFont:
    """TextStyle'ı, sayfadaki görünümle aynı boyutta bir QFont'a çevirir."""
    family = "Arial"
    clean = style.fontname.split("+")[-1].lower()
    for key, fam in _FAMILY_MAP:
        if key in clean:
            family = fam
            break
    font = QFont(family)
    font.setPixelSize(max(8, round(style.size * zoom)))
    font.setBold(style.bold)
    font.setItalic(style.italic)
    return font


def _qfont_for_hit(hit, zoom: float) -> QFont:
    """Editör fontunu OCR satırının gerçek mürekkep yüksekliği/genişliğine uyarlar."""
    font = _qfont_for_style(hit.style, zoom)
    lines = list(getattr(hit, "lines", [hit]))
    samples = [line for line in lines if line.text.strip()]
    if not samples:
        return font

    representative = max(samples, key=lambda line: len(line.text))
    metrics = QFontMetricsF(font)
    target_height = statistics.median(line.rect.height * zoom for line in samples)
    font_height = metrics.height()
    if font_height > .5:
        # tightBoundingRect yerine genel font yüksekliğini kullan; böylece "ac" gibi
        # ascender/descender içermeyen kelimeler devasa boyutlara şişirilmez.
        adjusted = round(font.pixelSize() * target_height / font_height)
        font.setPixelSize(max(6, min(round(target_height * 1.7), adjusted)))

    metrics = QFontMetricsF(font)
    ratios = []
    for line in samples:
        # Paragraf satırlarının çoğu iki yana yaslıdır. Satırın bütün genişliğini
        # harf genişliğine uydurmak, büyük kelime aralarını yanlışlıkla gliflere
        # yükleyip canlı fontu gereğinden fazla genişletiyordu. Tek tek kelime
        # mürekkep kutuları gerçek yatay ölçeği daha güvenilir verir.
        for word in getattr(line, "words", ()):
            if len(word.text.strip(".,;:()[]{}'\"")) < 3:
                continue
            measured = metrics.horizontalAdvance(word.text)
            if measured > .5 and word.rect.width > .5:
                ratio = word.rect.width * zoom / measured
                if .55 <= ratio <= 1.55:
                    ratios.append(ratio)
    if ratios:
        font.setStretch(max(72, min(132, round(100 * statistics.median(ratios)))))
    return font


class InlineEditor(QLineEdit):
    """Satırın kendi yerinde açılan, çerçevesiz doğal metin düzenleyici.

    İlk açılışta metin ve zemin saydam tutulur. Böylece kullanıcı daha hiçbir
    şey değiştirmeden alttaki gerçek PDF satırı farklı bir sistem fontuyla
    yeniden çizilmez. İlk klavye değişikliğinde bütün satır tek akış halinde
    canlı düzenleme görünümüne geçer.
    """

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent, text: str, font: QFont, color_css: str,
                 background_css: str, cursor_index: int, align: int,
                 preserve_source: bool = True) -> None:
        super().__init__(parent)
        self._done = False
        self._source_pristine = bool(text) and preserve_source
        self._background_mask = None
        self._live_style = (
            "QLineEdit { background: %s; color: %s; border: none; padding: 0;"
            " selection-background-color: #8f6a47;"
            " selection-color: #ffffff; }" % (background_css, color_css)
        )
        self._textured_live_style = (
            "QLineEdit { background: transparent; color: %s; border: none;"
            " padding: 0; selection-background-color: #8f6a47;"
            " selection-color: #ffffff; }" % color_css
        )
        self._source_style = (
            "QLineEdit { background: transparent; color: rgba(0, 0, 0, 0);"
            " border: none; padding: 0;"
            " selection-background-color: transparent;"
            " selection-color: transparent; }"
        )
        self.setText(text)
        self.setFont(font)
        self.setFrame(False)
        self.setTextMargins(0, 0, 0, 0)
        alignment = {1: Qt.AlignmentFlag.AlignHCenter,
                     2: Qt.AlignmentFlag.AlignRight}.get(
                         align, Qt.AlignmentFlag.AlignLeft)
        self.setAlignment(alignment | Qt.AlignmentFlag.AlignVCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet(self._source_style if self._source_pristine
                           else self._live_style)
        self.setCursorPosition(max(0, min(len(text), cursor_index)))
        self.textEdited.connect(self._start_live_editing)

    @property
    def source_pixels_preserved(self) -> bool:
        """True iken satırı editör değil, alttaki PDF görüntüsü çiziyordur."""
        return self._source_pristine

    def set_background_mask(self, mask: QLabel) -> None:
        self._background_mask = mask

    def _start_live_editing(self, _text: str) -> None:
        if not self._source_pristine:
            return
        self._source_pristine = False
        if self._background_mask is not None:
            self._background_mask.show()
            self._background_mask.raise_()
            self.raise_()
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
            self.setAutoFillBackground(False)
            self.setStyleSheet(self._textured_live_style)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.setStyleSheet(self._live_style)
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            self.setAutoFillBackground(True)
        self.update()

    def _text_x(self, index: int) -> float:
        """Saydam önizlemede tek satırlık seçim katmanının yaklaşık x'i."""
        metrics = QFontMetricsF(self.font())
        full_width = metrics.horizontalAdvance(self.text())
        alignment = self.alignment()
        if alignment & Qt.AlignmentFlag.AlignRight:
            origin = max(0.0, self.width() - full_width)
        elif alignment & Qt.AlignmentFlag.AlignHCenter:
            origin = max(0.0, (self.width() - full_width) / 2.0)
        else:
            origin = 0.0
        return origin + metrics.horizontalAdvance(self.text()[:max(0, index)])

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if not self._source_pristine:
            return
        painter = QPainter(self)
        metrics = QFontMetricsF(self.font())
        text_height = min(self.height() - 2, max(4, round(metrics.height())))
        top = max(1, round((self.height() - text_height) / 2))
        bottom = min(self.height() - 1, top + text_height)

        selection_start = self.selectionStart()
        if selection_start >= 0 and self.selectedText():
            left = round(self._text_x(selection_start))
            right = round(self._text_x(selection_start + len(self.selectedText())))
            painter.fillRect(min(left, right), top, max(1, abs(right - left)),
                             max(1, bottom - top), QColor(143, 106, 71, 58))

        painter.end()

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(commit=True)
            return
        if ev.key() == Qt.Key.Key_Escape:
            self._finish(commit=False)
            return
        super().keyPressEvent(ev)

    def focusOutEvent(self, ev) -> None:
        super().focusOutEvent(ev)
        self._finish(commit=True)

    def _finish(self, commit: bool) -> None:
        if self._done:
            log.debug("editor _finish: zaten kapali, yoksayildi (commit=%s)", commit)
            return
        self._done = True
        # PDF'ye yazma taranmış sayfalarda birkaç saniye sürebilir. Widget'ı
        # sinyali göndermeden kaldırmazsak deleteLater() event-loop'a kadar
        # çalışmaz ve eski editör yeni sayfa görüntüsünün üstünde kalır.
        self.hide()
        log.debug("editor _finish: commit=%s metin=%r", commit, self.text())
        if commit:
            self.committed.emit(self.text())
        else:
            self.cancelled.emit()


class BlockEditor(QTextEdit):
    """Bir paragrafı kutusuz, doğal satır akışıyla yerinde düzenler."""

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent, text: str, font: QFont, color_css: str,
                 background_css: str, cursor_index: int,
                 preserve_source: bool = True,
                 line_height_px: float | None = None) -> None:
        super().__init__(parent)
        self._done = False
        self._source_pristine = bool(text) and preserve_source
        self._background_mask = None
        self._background_css = background_css
        self._live_style = (
            "QTextEdit { background: %s; color: %s; border: none; padding: 0;"
            " selection-background-color: #8f6a47;"
            " selection-color: #ffffff; }" % (background_css, color_css)
        )
        self._textured_live_style = (
            "QTextEdit { background: transparent; color: %s; border: none;"
            " padding: 0; selection-background-color: #8f6a47;"
            " selection-color: #ffffff; }" % color_css
        )
        self._source_style = (
            "QTextEdit { background: transparent;"
            " color: rgba(0, 0, 0, 0); border: none; padding: 0;"
            " selection-background-color: rgba(143, 106, 71, 46);"
            " selection-color: rgba(0, 0, 0, 0); }"
        )
        self.setFrameStyle(0)
        self.setAcceptRichText(False)
        self.setFont(font)
        self.document().setDocumentMargin(0)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground,
                                     True)
        self.setStyleSheet(self._source_style if self._source_pristine
                           else self._live_style)
        self.setPlainText(text)
        block_format = QTextBlockFormat()
        block_format.setAlignment(Qt.AlignmentFlag.AlignJustify)
        if line_height_px is not None and line_height_px > 1:
            block_format.setLineHeight(
                line_height_px,
                QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        all_text = self.textCursor()
        all_text.select(QTextCursor.SelectionType.Document)
        all_text.mergeBlockFormat(block_format)
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(len(text), cursor_index)))
        self.setTextCursor(cursor)
        self.textChanged.connect(self._start_live_editing)

    @property
    def source_pixels_preserved(self) -> bool:
        return self._source_pristine

    def set_background_mask(self, mask: QLabel) -> None:
        self._background_mask = mask

    def _start_live_editing(self) -> None:
        if not self._source_pristine:
            return
        self._source_pristine = False
        if self._background_mask is not None:
            self._background_mask.show()
            self._background_mask.raise_()
            self.raise_()
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.viewport().setAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent,
                                         False)
            self.viewport().setAutoFillBackground(False)
            self.viewport().setStyleSheet(
                "background: transparent; border: none;")
            self.setStyleSheet(self._textured_live_style)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.viewport().setAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground, False)
            # Tek başına kullanılan editörlerde de kaynak gliflerinin iki kat
            # çizilmemesi için opak kâğıt rengi güvenli yedek olarak kalır.
            self.viewport().setStyleSheet(
                f"background: {self._background_css}; border: none;")
            self.setStyleSheet(self._live_style)
            self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent,
                                         True)
            self.viewport().setAutoFillBackground(True)
        self.viewport().update()
        self.update()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape:
            self._finish(commit=False)
            return
        if (ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._finish(commit=True)
            return
        # Enter yeni satır, yukarı/aşağı ise Qt'nin doğal görsel satır gezinmesidir.
        super().keyPressEvent(ev)

    def focusOutEvent(self, ev) -> None:
        super().focusOutEvent(ev)
        self._finish(commit=True)

    def _finish(self, commit: bool) -> None:
        if self._done:
            return
        self._done = True
        text = self.toPlainText()
        # Ağır PDF paragraf düzenlemesi başlamadan canlı katmanı gerçekten
        # kaldır. Sadece deleteLater() kullanmak, işlem bitene kadar ekranda
        # iki metin katmanı bırakıyordu.
        self.hide()
        log.debug("block editor _finish: commit=%s metin=%r", commit, text)
        if commit:
            self.committed.emit(text)
        else:
            self.cancelled.emit()


class _PageWidget(QLabel):
    """Tek bir sayfayı gösterir; rubber-band seçim ve tık olaylarını üstteki
    ContinuousView'a sayfa numarasıyla iletir."""

    def __init__(self, page_no: int, view: "ContinuousView") -> None:
        super().__init__()
        self.page_no = page_no
        self._view = view
        self.rendered = False
        self._dragging = False
        self._drag_start = QPoint()
        self._drag_rect = QRect()
        self._object_rect = QRect()
        self._object_source_rect = QRect()
        self._object_operation: str | None = None
        self._object_handle: str | None = None
        self._object_start_rect = QRect()
        self._object_start_point = QPoint()
        self.setStyleSheet("background: white;")

    # ---------- render ----------

    def set_placeholder(self, w: int, h: int) -> None:
        self.rendered = False
        self.clear()
        self.setFixedSize(w, h)

    def set_page_pixmap(self, pix: fitz.Pixmap) -> None:
        img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888)
        # pix.samples yaşam süresi için kopya al
        self.setPixmap(QPixmap.fromImage(img.copy()))
        self.setFixedSize(pix.width, pix.height)
        self.rendered = True

    def drop_pixmap(self) -> None:
        if self.rendered:
            self.clear()
            self.rendered = False

    # ---------- mouse ----------

    def clear_object_selection(self) -> None:
        self._object_rect = QRect()
        self._object_source_rect = QRect()
        self._object_operation = None
        self._object_handle = None
        self.update()

    def set_object_rect(self, rect: QRect) -> None:
        self._object_rect = rect.normalized()
        self._object_source_rect = QRect(self._object_rect)
        self.update()

    def _handle_rects(self) -> dict[str, QRect]:
        if self._object_rect.isNull():
            return {}
        r = self._object_rect
        half = 5
        points = {
            "nw": r.topLeft(), "n": QPoint(r.center().x(), r.top()),
            "ne": r.topRight(), "e": QPoint(r.right(), r.center().y()),
            "se": r.bottomRight(), "s": QPoint(r.center().x(), r.bottom()),
            "sw": r.bottomLeft(), "w": QPoint(r.left(), r.center().y()),
        }
        return {name: QRect(point.x() - half, point.y() - half,
                            half * 2 + 1, half * 2 + 1)
                for name, point in points.items()}

    def _hit_handle(self, point: QPoint) -> str | None:
        for name, rect in self._handle_rects().items():
            if rect.adjusted(-2, -2, 2, 2).contains(point):
                return name
        return None

    @staticmethod
    def _cursor_for_handle(handle: str | None):
        if handle in ("nw", "se"):
            return Qt.CursorShape.SizeFDiagCursor
        if handle in ("ne", "sw"):
            return Qt.CursorShape.SizeBDiagCursor
        if handle in ("n", "s"):
            return Qt.CursorShape.SizeVerCursor
        if handle in ("e", "w"):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.ArrowCursor

    def _clamp_object_rect(self, rect: QRect) -> QRect:
        bounds = self.rect().adjusted(1, 1, -2, -2)
        result = rect.normalized()
        if result.width() < 24:
            result.setWidth(24)
        if result.height() < 24:
            result.setHeight(24)
        if result.left() < bounds.left():
            result.moveLeft(bounds.left())
        if result.top() < bounds.top():
            result.moveTop(bounds.top())
        if result.right() > bounds.right():
            result.moveRight(bounds.right())
        if result.bottom() > bounds.bottom():
            result.moveBottom(bounds.bottom())
        return result

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            z = self._view.zoom
            p = ev.position().toPoint()
            self._view.text_clicked.emit(
                self.page_no, fitz.Point(p.x() / z, p.y() / z))

    def mousePressEvent(self, ev) -> None:
        v = self._view
        if not v.selection_enabled or ev.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        mode = v.interaction_mode
        if mode == "add":
            p = ev.position().toPoint()
            z = v.zoom
            v.point_clicked.emit(self.page_no, fitz.Point(p.x() / z, p.y() / z))
            return
        if mode == "object":
            point = ev.position().toPoint()
            handle = self._hit_handle(point)
            if handle is not None:
                self._object_operation = "resize"
                self._object_handle = handle
                self._object_start_rect = QRect(self._object_rect)
                self._object_start_point = point
                return
            if not self._object_rect.isNull() and self._object_rect.contains(point):
                self._object_operation = "move"
                self._object_start_rect = QRect(self._object_rect)
                self._object_start_point = point
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                return
            self.clear_object_selection()
            self._dragging = True
            self._drag_start = point
            self._drag_rect = QRect(point, point)
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        self._dragging = True
        self._drag_start = ev.position().toPoint()
        self._drag_rect = QRect(self._drag_start, self._drag_start)
        self.update()

    def mouseMoveEvent(self, ev) -> None:
        point = ev.position().toPoint()
        if self._object_operation == "move":
            delta = point - self._object_start_point
            self._object_rect = self._clamp_object_rect(
                self._object_start_rect.translated(delta))
            self.update()
            return
        if self._object_operation == "resize":
            rect = QRect(self._object_start_rect)
            handle = self._object_handle or ""
            if "w" in handle:
                rect.setLeft(point.x())
            if "e" in handle:
                rect.setRight(point.x())
            if "n" in handle:
                rect.setTop(point.y())
            if "s" in handle:
                rect.setBottom(point.y())
            self._object_rect = self._clamp_object_rect(rect)
            self.setCursor(self._cursor_for_handle(handle))
            self.update()
            return
        if self._dragging:
            self._drag_rect = QRect(self._drag_start,
                                    point).normalized()
            self.update()
            return
        if self._view.interaction_mode == "object":
            handle = self._hit_handle(point)
            if handle is not None:
                self.setCursor(self._cursor_for_handle(handle))
            elif not self._object_rect.isNull() and self._object_rect.contains(point):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
        elif self._view.interaction_mode == "edit":
            self.setCursor(Qt.CursorShape.IBeamCursor)

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._object_operation is not None:
            changed = self._object_rect != self._object_source_rect
            source = QRect(self._object_source_rect)
            target = QRect(self._object_rect)
            self._object_operation = None
            self._object_handle = None
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            if changed and not source.isNull() and not target.isNull():
                z = self._view.zoom
                source_pdf = fitz.Rect(source.left() / z, source.top() / z,
                                       source.right() / z, source.bottom() / z)
                target_pdf = fitz.Rect(target.left() / z, target.top() / z,
                                       target.right() / z, target.bottom() / z)
                self._object_source_rect = QRect(target)
                self._view.object_transform_requested.emit(
                    self.page_no, source_pdf, target_pdf)
            self.update()
            return
        if not self._dragging or ev.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        rect = self._drag_rect
        self._drag_rect = QRect()
        self.update()
        z = self._view.zoom
        if self._view.interaction_mode == "object":
            if rect.width() > 10 and rect.height() > 10:
                self._object_rect = QRect(rect)
                self._object_source_rect = QRect(rect)
                self._view.object_selected.emit(
                    self.page_no, fitz.Rect(rect.left() / z, rect.top() / z,
                                            rect.right() / z, rect.bottom() / z))
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        if rect.width() > 3 and rect.height() > 3:
            self._view.rect_selected.emit(self.page_no, fitz.Rect(
                rect.left() / z, rect.top() / z,
                rect.right() / z, rect.bottom() / z))

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if not self._drag_rect.isNull():
            painter = QPainter(self)
            pen = QPen(QColor("#b78a5d"))
            pen.setWidth(1)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._drag_rect)
            painter.end()
        if not self._object_rect.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#b78a5d"), 1.4))
            painter.setBrush(QColor(183, 138, 93, 18))
            painter.drawRect(self._object_rect)
            painter.setPen(QPen(QColor("#f0dcc6"), 1))
            painter.setBrush(QBrush(QColor("#8b603a")))
            for handle in self._handle_rects().values():
                painter.drawRect(handle)
            painter.end()


class ContinuousView(QScrollArea):
    """Alt alta dizilmiş sayfalarla kaydırmalı görünüm.

    Sinyaller PDF koordinatı taşır:
    - rect_selected(page_no, fitz.Rect)
    - point_clicked(page_no, fitz.Point)      # yazı ekleme modunda tek tık
    - text_clicked(page_no, fitz.Point)       # seçim modunda sürüklemesiz tık
    - edit_committed(page_no, fitz.Rect, str, TextStyle)  # inline editör onayı
    - current_page_changed(page_no)
    """

    rect_selected = Signal(int, object)
    point_clicked = Signal(int, object)
    text_clicked = Signal(int, object)
    edit_committed = Signal(int, object, str, object)
    current_page_changed = Signal(int)
    object_selected = Signal(int, object)
    object_transform_requested = Signal(int, object, object)

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._pdf = None
        self.zoom = 1.5
        self.interaction_mode = "edit"
        self.selection_enabled = True
        self.click_mode = False
        self._pages: list[_PageWidget] = []
        self._current_page = 0
        self._editor: InlineEditor | BlockEditor | None = None
        self._update_pending = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)
        self._update_timer.timeout.connect(self._update_visible_now)

        self._container = QWidget()
        self._container.setObjectName("pageContainer")
        self._layout = QVBoxLayout(self._container)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter
                                  | Qt.AlignmentFlag.AlignTop)
        self._layout.setSpacing(PAGE_GAP)
        self._layout.setContentsMargins(24, 24, 24, 24)
        self.setWidget(self._container)

        self.verticalScrollBar().valueChanged.connect(self._schedule_update)

    # ---------- doküman ----------

    def set_document(self, pdf) -> None:
        """pdf: PdfDocument veya None (kapat)."""
        self.close_editor()
        self._pdf = pdf
        for w in self._pages:
            self._layout.removeWidget(w)
            w.deleteLater()
        self._pages.clear()
        self._current_page = 0
        if pdf is None or pdf.doc is None:
            return
        for no in range(pdf.page_count):
            w = _PageWidget(no, self)
            w.setCursor(Qt.CursorShape.IBeamCursor)
            pw, ph = pdf.page_size(no)
            w.set_placeholder(int(pw * self.zoom), int(ph * self.zoom))
            self._layout.addWidget(
                w, 0, Qt.AlignmentFlag.AlignHCenter)
            self._pages.append(w)
        self.verticalScrollBar().setValue(0)
        self._relayout_and_update()

    def set_zoom(self, zoom: float) -> None:
        self.close_editor()
        self.clear_object_selection()
        self.zoom = zoom
        if self._pdf is None or self._pdf.doc is None:
            return
        for w in self._pages:
            pw, ph = self._pdf.page_size(w.page_no)
            w.set_placeholder(int(pw * zoom), int(ph * zoom))
        self._relayout_and_update()

    def _relayout_and_update(self) -> None:
        # QScrollArea (widgetResizable) konteyneri viewport'a sıkıştırabiliyor;
        # gerçek içerik boyutunu minimum olarak bildir ki sayfalar üst üste binmesin
        m = self._layout.contentsMargins()
        total_h = m.top() + m.bottom()
        max_w = 0
        for w in self._pages:
            total_h += w.height()
            max_w = max(max_w, w.width())
        total_h += PAGE_GAP * max(0, len(self._pages) - 1)
        self._container.setMinimumSize(max_w + m.left() + m.right(), total_h)
        self._layout.activate()
        self._schedule_update()
        QTimer.singleShot(0, self._schedule_update)

    def refresh_page(self, page_no: int) -> None:
        """Düzenleme sonrası tek sayfayı yeniden çizer — eski pixmap'i de direkt temizler."""
        if 0 <= page_no < len(self._pages):
            w = self._pages[page_no]
            w.rendered = False
            w.clear()
            w.update()
            if self._pdf is not None:
                self._pdf.invalidate_render_cache(page_no)
            self._schedule_update()
            QTimer.singleShot(0, lambda: self._force_repaint_page(page_no))

    def _force_repaint_page(self, page_no: int) -> None:
        if 0 <= page_no < len(self._pages):
            w = self._pages[page_no]
            if not w.rendered:
                vp = self.viewport()
                if vp is not None:
                    vp.update(w.geometry())
            self._update_visible_now()

    def refresh_all(self) -> None:
        for w in self._pages:
            w.rendered = False
            w.clear()
        if self._pdf is not None:
            self._pdf.invalidate_render_cache()
        self.viewport().update()
        self._schedule_update()

    def scroll_to_page(self, page_no: int) -> None:
        if 0 <= page_no < len(self._pages):
            self.verticalScrollBar().setValue(self._pages[page_no].y() - 12)

    @property
    def current_page(self) -> int:
        return self._current_page

    def set_interaction_mode(self, mode: str) -> None:
        """edit / delete / add / object etkileşimlerinden birini etkinleştirir."""
        self.close_editor()
        self.interaction_mode = mode
        self.selection_enabled = True
        self.click_mode = mode == "add"
        if mode != "object":
            self.clear_object_selection()
        cursors = {
            "edit": Qt.CursorShape.IBeamCursor,
            "delete": Qt.CursorShape.CrossCursor,
            "add": Qt.CursorShape.IBeamCursor,
            "object": Qt.CursorShape.CrossCursor,
        }
        cursor = cursors.get(mode, Qt.CursorShape.ArrowCursor)
        for page in self._pages:
            page.setCursor(cursor)

    def clear_object_selection(self) -> None:
        for page in self._pages:
            page.clear_object_selection()

    def set_object_selection(self, page_no: int, rect: fitz.Rect) -> None:
        for page in self._pages:
            page.clear_object_selection()
        if 0 <= page_no < len(self._pages):
            z = self.zoom
            self._pages[page_no].set_object_rect(QRect(
                round(rect.x0 * z), round(rect.y0 * z),
                max(1, round(rect.width * z)), max(1, round(rect.height * z))))

    # ---------- inline editör ----------

    def open_editor(self, page_no: int, hit, click_point: fitz.Point) -> None:
        """Algılanan paragrafı kendi yerinde, kutusuz tek metin akışı olarak açar."""
        self.close_editor()
        if not (0 <= page_no < len(self._pages)):
            return
        page_widget = self._pages[page_no]
        z = self.zoom
        rect = hit.layout_rect
        text = hit.text
        style = hit.style
        r, g, b = (int(v * 255) for v in style.color)
        br, bg, bb = hit.background
        is_block = len(getattr(hit, "lines", ())) > 1
        if is_block:
            centers = [((line.rect.y0 + line.rect.y1) / 2) * z
                       for line in hit.lines]
            line_steps = [right - left
                          for left, right in zip(centers, centers[1:])
                          if right - left > 1]
            line_height_px = (statistics.median(line_steps)
                              if line_steps else None)
            editor = BlockEditor(
                page_widget, text, _qfont_for_hit(hit, z),
                f"rgb({r},{g},{b})", f"rgb({br},{bg},{bb})",
                hit.cursor_index(click_point.x),
                preserve_source=(hit.source != "blank-cell"),
                line_height_px=line_height_px)
        else:
            editor = InlineEditor(
                page_widget, text, _qfont_for_hit(hit, z),
                f"rgb({r},{g},{b})", f"rgb({br},{bg},{bb})",
                hit.cursor_index(click_point.x), hit.align,
                preserve_source=(hit.source != "blank-cell"))
        x = round(rect.x0 * z)
        y = round(rect.y0 * z) - 1
        w = max(round(rect.width * z), 48)
        h = max(round(rect.height * z) + (5 if is_block else 3),
                round(style.size * z * (1.45 if is_block else 1.25)))
        editor.setGeometry(x, y, w, h)
        source_pixmap = page_widget.pixmap()
        if (getattr(hit, "source", "") == "ocr" and self._pdf is not None
                and source_pixmap is not None and not source_pixmap.isNull()):
            background = self._pdf.editor_background_patch(
                page_no, hit, source_pixmap.toImage(), (x, y, w, h))
            if background is not None and not background.isNull():
                mask = QLabel(page_widget)
                mask.setGeometry(x, y, w, h)
                mask.setPixmap(QPixmap.fromImage(background))
                mask.setStyleSheet("background: transparent; border: none;")
                mask.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                mask.hide()
                editor.set_background_mask(mask)
        if is_block:
            minimum_height = h

            def resize_to_document() -> None:
                wanted = max(minimum_height,
                             round(editor.document().size().height()) + 4)
                maximum = max(minimum_height,
                              page_widget.height() - editor.y() - 2)
                editor.resize(editor.width(), min(wanted, maximum))

            editor.textChanged.connect(resize_to_document)
        editor.committed.connect(
            lambda t: self._on_editor_commit(page_no, hit, t, style))
        editor.cancelled.connect(self.close_editor)
        editor.show()
        editor.setFocus()
        self._editor = editor
        log.info("editor acildi: sayfa=%s kaynak=%s satir=%s rect=%s metin=%r",
                 page_no, hit.source, len(getattr(hit, "lines", [hit])),
                 hit.rect, text)

    def close_editor(self) -> None:
        if self._editor is not None:
            editor, self._editor = self._editor, None
            editor._done = True  # odak kaybı sinyalinin tekrar tetiklemesini önle
            parent = editor.parentWidget()
            dirty_rect = editor.geometry()
            mask = getattr(editor, "_background_mask", None)
            editor.hide()
            editor.clearFocus()
            editor.deleteLater()
            if mask is not None:
                mask.hide()
                mask.deleteLater()
            if parent is not None:
                # on_edit_committed senkron ve pahalıdır. Eski katmanın
                # silinmesini event-loop'a bırakmadan PDF sayfasını şimdi çiz.
                parent.repaint(dirty_rect)

    def _on_editor_commit(self, page_no: int, hit,
                          text: str, style) -> None:
        self.close_editor()
        self.edit_committed.emit(page_no, hit, text, style)

    # ---------- lazy render ----------

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._schedule_update()

    def _schedule_update(self) -> None:
        if self._update_pending:
            return
        self._update_pending = True
        self._update_timer.start()

    def _update_visible_now(self) -> None:
        self._update_pending = False
        if self._pdf is None or self._pdf.doc is None or not self._pages:
            return
        vp_h = self.viewport().height()
        top = self.verticalScrollBar().value()
        bottom = top + vp_h
        render_band = (top - vp_h * RENDER_MARGIN, bottom + vp_h * RENDER_MARGIN)
        drop_band = (top - vp_h * DROP_MARGIN, bottom + vp_h * DROP_MARGIN)
        center = top + vp_h / 2
        current = self._current_page

        for w in self._pages:
            y0, y1 = w.y(), w.y() + w.height()
            if y0 <= center <= y1:
                current = w.page_no
            if y1 >= render_band[0] and y0 <= render_band[1]:
                if not w.rendered:
                    w.set_page_pixmap(self._pdf.render_page(w.page_no, self.zoom))
            elif y1 < drop_band[0] or y0 > drop_band[1]:
                w.drop_pixmap()

        if current != self._current_page:
            self._current_page = current
            self.current_page_changed.emit(current)
