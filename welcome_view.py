"""PDF Düzenleyici için animasyonlu, dosya odaklı açılış yüzeyi."""
from __future__ import annotations

import math
import os

from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer,
                            Signal, QPropertyAnimation)
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)


INK = QColor("#d8d5cd")
MUTED_INK = QColor("#8e9188")
COPPER = QColor("#b78a5d")


def create_line_icon(name: str, color: QColor | str = INK,
                     size: int = 20) -> QIcon:
    """Harici ikon paketine ihtiyaç duymayan ince çizgili araç ikonu üretir."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.55)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    s = float(size)
    path = QPainterPath()
    if name == "open":
        path.moveTo(.13*s, .32*s); path.lineTo(.39*s, .32*s)
        path.lineTo(.47*s, .23*s); path.lineTo(.83*s, .23*s)
        path.lineTo(.87*s, .72*s); path.lineTo(.18*s, .72*s); path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(.20*s, .44*s), QPointF(.80*s, .44*s))
    elif name in ("save", "save-as"):
        painter.drawRoundedRect(QRectF(.20*s, .14*s, .60*s, .72*s), 1.5, 1.5)
        painter.drawRect(QRectF(.31*s, .14*s, .35*s, .23*s))
        painter.drawRoundedRect(QRectF(.31*s, .55*s, .38*s, .24*s), 1.5, 1.5)
        if name == "save-as":
            painter.drawLine(QPointF(.70*s, .47*s), QPointF(.91*s, .47*s))
            painter.drawLine(QPointF(.81*s, .37*s), QPointF(.81*s, .58*s))
    elif name == "undo":
        path.moveTo(.28*s, .40*s); path.lineTo(.13*s, .28*s); path.lineTo(.28*s, .16*s)
        painter.drawPath(path)
        painter.drawArc(QRectF(.20*s, .23*s, .63*s, .55*s), 35*16, 245*16)
    elif name == "delete":
        painter.drawRoundedRect(QRectF(.28*s, .31*s, .44*s, .54*s), 2, 2)
        painter.drawLine(QPointF(.20*s, .25*s), QPointF(.80*s, .25*s))
        painter.drawLine(QPointF(.39*s, .16*s), QPointF(.61*s, .16*s))
        painter.drawLine(QPointF(.42*s, .42*s), QPointF(.42*s, .73*s))
        painter.drawLine(QPointF(.58*s, .42*s), QPointF(.58*s, .73*s))
    elif name == "type":
        painter.drawLine(QPointF(.20*s, .22*s), QPointF(.80*s, .22*s))
        painter.drawLine(QPointF(.50*s, .22*s), QPointF(.50*s, .79*s))
        painter.drawLine(QPointF(.34*s, .79*s), QPointF(.66*s, .79*s))
        painter.drawLine(QPointF(.20*s, .22*s), QPointF(.20*s, .34*s))
        painter.drawLine(QPointF(.80*s, .22*s), QPointF(.80*s, .34*s))
    elif name == "edit":
        path.moveTo(.18*s, .73*s); path.lineTo(.25*s, .51*s)
        path.lineTo(.66*s, .10*s); path.lineTo(.86*s, .30*s)
        path.lineTo(.45*s, .71*s); path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(.57*s, .20*s), QPointF(.77*s, .40*s))
        painter.drawLine(QPointF(.17*s, .84*s), QPointF(.80*s, .84*s))
    elif name == "object":
        painter.drawRect(QRectF(.25*s, .25*s, .50*s, .50*s))
        painter.setBrush(QColor(color))
        for x, y in ((.20, .20), (.80, .20), (.80, .80), (.20, .80)):
            painter.drawRect(QRectF((x-.055)*s, (y-.055)*s, .11*s, .11*s))
    elif name == "image":
        painter.drawRoundedRect(QRectF(.18*s, .18*s, .64*s, .64*s), 1.5, 1.5)
        painter.drawEllipse(QPointF(.36*s, .36*s), .07*s, .07*s)
        path.moveTo(.23*s, .74*s)
        path.lineTo(.44*s, .50*s)
        path.lineTo(.56*s, .62*s)
        path.lineTo(.66*s, .48*s)
        path.lineTo(.78*s, .74*s)
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "file-up":
        path.moveTo(.25*s, .10*s); path.lineTo(.61*s, .10*s)
        path.lineTo(.80*s, .29*s); path.lineTo(.80*s, .87*s)
        path.lineTo(.25*s, .87*s); path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(.61*s, .10*s), QPointF(.61*s, .29*s))
        painter.drawLine(QPointF(.61*s, .29*s), QPointF(.80*s, .29*s))
        painter.drawLine(QPointF(.52*s, .70*s), QPointF(.52*s, .40*s))
        painter.drawLine(QPointF(.40*s, .52*s), QPointF(.52*s, .40*s))
        painter.drawLine(QPointF(.64*s, .52*s), QPointF(.52*s, .40*s))
    else:
        painter.drawEllipse(QRectF(.23*s, .23*s, .54*s, .54*s))

    painter.end()
    return QIcon(pixmap)


def pdf_badge_pixmap(size: int = 32) -> QPixmap:
    """Üst araç çubuğunda kullanılan özgün, küçük PDF işareti."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(2, 1, size - 5, size - 3)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, QColor("#c1976b"))
    gradient.setColorAt(1, QColor("#8b603a"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(rect, 6, 6)
    painter.setPen(QPen(QColor("#171713"), 1.35))
    painter.drawLine(QPointF(size*.30, size*.36), QPointF(size*.70, size*.36))
    painter.drawLine(QPointF(size*.30, size*.51), QPointF(size*.62, size*.51))
    painter.drawLine(QPointF(size*.30, size*.66), QPointF(size*.54, size*.66))
    painter.end()
    return pixmap


class DocumentEmblem(QWidget):
    """Kâğıt katmanlarını ve tarama ışığını sürekli, sakin biçimde canlandırır."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(350, 310)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(32)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        self._phase = (self._phase + .009) % 1.0
        self.update()

    def _draw_sheet(self, painter: QPainter, angle: float, dx: float,
                    dy: float, fill: QColor, border: QColor) -> None:
        painter.save()
        painter.translate(self.width()/2 + dx, self.height()/2 + dy)
        painter.rotate(angle)
        rect = QRectF(-82, -112, 164, 224)
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3, 3)
        painter.restore()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        float_y = math.sin(self._phase * math.tau) * 4.0

        # İnce yörünge çizgileri görsele teknik bir karakter verir.
        painter.setPen(QPen(QColor(183, 138, 93, 42), 1))
        painter.drawEllipse(QRectF(24, 26, 296, 256))
        painter.drawArc(QRectF(47, 48, 250, 212), 205*16, 122*16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(183, 138, 93, 180))
        orbit = self._phase * math.tau
        painter.drawEllipse(QPointF(172 + math.cos(orbit)*148,
                                    154 + math.sin(orbit)*128), 2.5, 2.5)

        self._draw_sheet(painter, -7.5, -23, float_y + 4,
                         QColor("#292b27"), QColor("#454840"))
        self._draw_sheet(painter, 7.0, 24, float_y + 7,
                         QColor("#232521"), QColor("#3d4039"))

        painter.save()
        painter.translate(self.width()/2, self.height()/2 + float_y)
        rect = QRectF(-84, -116, 168, 230)
        paper = QLinearGradient(rect.topLeft(), rect.bottomRight())
        paper.setColorAt(0, QColor("#eee9df"))
        paper.setColorAt(.68, QColor("#ded7ca"))
        paper.setColorAt(1, QColor("#c9c0b1"))
        painter.setPen(QPen(QColor(255, 255, 255, 48), 1))
        painter.setBrush(paper)
        painter.drawRoundedRect(rect, 3, 3)

        # Katlanmış kâğıt köşesi.
        fold = QPainterPath()
        fold.moveTo(53, -116); fold.lineTo(84, -85); fold.lineTo(53, -85)
        fold.closeSubpath()
        painter.setBrush(QColor("#b9ad9b"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(fold)
        painter.setPen(QPen(QColor("#907b63"), 1))
        painter.drawLine(QPointF(53, -116), QPointF(53, -85))
        painter.drawLine(QPointF(53, -85), QPointF(84, -85))

        painter.setPen(QPen(QColor("#5b5a54"), 3.2,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(-53, -74), QPointF(27, -74))
        painter.setPen(QPen(QColor("#8b887f"), 2.0,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for y, width in ((-49, 106), (-32, 91), (-15, 112), (2, 74),
                         (40, 104), (57, 86)):
            painter.drawLine(QPointF(-53, y), QPointF(-53 + width, y))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(COPPER)
        painter.drawRoundedRect(QRectF(-53, 77, 41, 7), 3.5, 3.5)

        # Yukarıdan aşağı ilerleyen çok hafif tarama çizgisi.
        scan_y = -90 + self._phase * 176
        scan = QLinearGradient(QPointF(-68, scan_y), QPointF(68, scan_y))
        scan.setColorAt(0, QColor(183, 138, 93, 0))
        scan.setColorAt(.5, QColor(183, 138, 93, 150))
        scan.setColorAt(1, QColor(183, 138, 93, 0))
        painter.setPen(QPen(QBrush(scan), 1.2))
        painter.drawLine(QPointF(-68, scan_y), QPointF(68, scan_y))
        painter.restore()


class WelcomePage(QWidget):
    """İlk açılışta görünen karşılama alanı ve PDF sürükle-bırak hedefi."""

    open_requested = Signal()
    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("welcomePage")
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setMinimumSize(760, 520)
        self._pointer = QPointF(0, 0)
        self._drag_active = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(52, 34, 52, 34)
        outer.addStretch(1)

        self.content = QWidget(self)
        self.content.setObjectName("welcomeContent")
        row = QHBoxLayout(self.content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(58)

        self.emblem = DocumentEmblem(self.content)
        row.addWidget(self.emblem, 0, Qt.AlignmentFlag.AlignCenter)

        copy = QWidget(self.content)
        copy.setMaximumWidth(530)
        copy_layout = QVBoxLayout(copy)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(0)

        kicker = QLabel("BELGE ATÖLYESİ", copy)
        kicker.setObjectName("welcomeKicker")
        kicker_font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        kicker_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.2)
        kicker.setFont(kicker_font)
        copy_layout.addWidget(kicker)

        title = QLabel("Belgenizi masaya\nbırakın.", copy)
        title.setObjectName("welcomeTitle")
        title.setFont(QFont("Segoe UI Variable Display", 31, QFont.Weight.DemiBold))
        copy_layout.addSpacing(12)
        copy_layout.addWidget(title)

        subtitle = QLabel(
            "PDF’yi biçimini bozmadan açın; metni, görseli ve tabloları "
            "doğrudan sayfanın üzerinde düzenleyin.", copy)
        subtitle.setObjectName("welcomeSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setFont(QFont("Segoe UI", 11))
        copy_layout.addSpacing(18)
        copy_layout.addWidget(subtitle)

        self.open_button = QPushButton("PDF seç ve aç", copy)
        self.open_button.setObjectName("primaryOpenButton")
        self.open_button.setIcon(create_line_icon("file-up", QColor("#171713"), 22))
        self.open_button.setIconSize(QSize(20, 20))
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.open_requested)
        self.open_button.setSizePolicy(QSizePolicy.Policy.Fixed,
                                       QSizePolicy.Policy.Fixed)
        copy_layout.addSpacing(26)
        copy_layout.addWidget(self.open_button)

        hint = QLabel("PDF dosyasını bu alana sürükleyip bırakabilirsiniz", copy)
        hint.setObjectName("welcomeHint")
        hint.setFont(QFont("Segoe UI", 9))
        copy_layout.addSpacing(12)
        copy_layout.addWidget(hint)
        copy_layout.addStretch(1)

        row.addWidget(copy, 1, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self.content, 0, Qt.AlignmentFlag.AlignCenter)
        outer.addStretch(1)

        self._opacity = QGraphicsOpacityEffect(self.content)
        self.content.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(520)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.emblem.start()
        self._fade.stop()
        self._opacity.setOpacity(0.0)
        self._fade.start()

    def hideEvent(self, event) -> None:
        self.emblem.stop()
        super().hideEvent(event)

    def mouseMoveEvent(self, event) -> None:
        self._pointer = event.position()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._pointer = QPointF(self.width()*.42, self.height()*.50)
        self.update()
        super().leaveEvent(event)

    @staticmethod
    def _pdf_path_from_mime(mime) -> str | None:
        if not mime.hasUrls() or len(mime.urls()) != 1:
            return None
        path = mime.urls()[0].toLocalFile()
        if path and os.path.isfile(path) and path.lower().endswith(".pdf"):
            return path
        return None

    def dragEnterEvent(self, event) -> None:
        if self._pdf_path_from_mime(event.mimeData()):
            self._drag_active = True
            self.setProperty("dragActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._drag_active = False
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        path = self._pdf_path_from_mime(event.mimeData())
        self._drag_active = False
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        if path:
            event.acceptProposedAction()
            self.file_dropped.emit(path)
        else:
            event.ignore()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0e100e"))

        pointer = self._pointer
        if pointer.isNull():
            pointer = QPointF(self.width()*.43, self.height()*.50)
        glow = QRadialGradient(pointer, max(self.width(), self.height())*.62)
        glow.setColorAt(0, QColor(98, 73, 45, 48))
        glow.setColorAt(.45, QColor(65, 51, 33, 21))
        glow.setColorAt(1, QColor(14, 16, 14, 0))
        painter.fillRect(self.rect(), QBrush(glow))

        grid_pen = QPen(QColor(112, 117, 105, 16), 1)
        painter.setPen(grid_pen)
        step = 32
        for x in range(0, self.width(), step):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            painter.drawLine(0, y, self.width(), y)

        painter.setPen(QPen(QColor(183, 138, 93, 26), 1))
        painter.drawLine(QPointF(0, self.height()*.83),
                         QPointF(self.width(), self.height()*.68))
        if self._drag_active:
            painter.setPen(QPen(QColor(183, 138, 93, 185), 2,
                                Qt.PenStyle.DashLine))
            painter.setBrush(QColor(183, 138, 93, 15))
            painter.drawRoundedRect(self.rect().adjusted(18, 18, -18, -18), 12, 12)
        painter.end()
        super().paintEvent(event)
