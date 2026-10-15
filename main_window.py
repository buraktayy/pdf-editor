"""Ana pencere: koyu (Adobe benzeri) tema, sürekli kaydırmalı görünüm,
stil eşleştirmeli düzenleme modları."""
from __future__ import annotations

import logging
import os
import traceback

import fitz

log = logging.getLogger("pdfeditor")
from PySide6.QtCore import (QObject, QRunnable, QSize, Qt, QThreadPool, Signal,
                            Slot)
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPlainTextEdit, QSizePolicy, QStackedWidget,
                               QToolBar, QVBoxLayout, QWidget)

from page_view import ContinuousView
from ocr_engine import ocr_is_available, recognize_pdf_page
from pdf_document import PdfDocument, TextBlockHit, TextHit, TextStyle
from welcome_view import (COPPER, WelcomePage, create_line_icon,
                          pdf_badge_pixmap)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(BASE_DIR, "assets", "icon.ico")

ZOOM_LEVELS = [0.75, 1.0, 1.25, 1.5, 2.0, 3.0]

DARK_QSS = """
QMainWindow { background: #101210; }
QToolBar#mainToolbar {
    background: #171916;
    border: none;
    border-bottom: 1px solid #30332d;
    padding: 8px 12px;
    spacing: 4px;
}
QToolBar#mainToolbar::separator {
    background: #353831;
    width: 1px;
    margin: 7px 10px;
}
QToolButton {
    color: #c9cbc4;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 7px 10px;
    font-family: "Segoe UI";
    font-size: 12px;
}
QToolButton:hover { background: #252822; border-color: #353930; color: #f1efe8; }
QToolButton:pressed { background: #30332c; }
QToolButton:checked {
    background: #372d23;
    border-color: #6f5338;
    color: #e6c39e;
}
QToolButton:disabled { color: #5e615a; background: transparent; }
QToolButton#openToolButton { color: #e3c09b; }
QScrollArea { border: none; background: #30322f; }
QWidget#pageContainer { background: #30322f; }
QScrollBar:vertical { background: #20221f; width: 11px; }
QScrollBar::handle:vertical { background: #51554c; border-radius: 5px;
                              min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #6b7065; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: #20221f; height: 11px; }
QScrollBar::handle:horizontal { background: #51554c; border-radius: 5px;
                                min-width: 30px; }
QStatusBar {
    background: #151714;
    color: #858980;
    border-top: 1px solid #292c27;
    padding-left: 7px;
    font-size: 11px;
}
QComboBox {
    background: #242722;
    color: #d5d6cf;
    border: 1px solid #3b3f37;
    border-radius: 6px;
    padding: 5px 9px;
    min-width: 68px;
}
QComboBox:hover { border-color: #66513b; }
QComboBox QAbstractItemView {
    background: #242722;
    color: #d5d6cf;
    selection-background-color: #5e452f;
}
QLabel#brandTitle { color: #ece9e1; font-size: 12px; font-weight: 600; }
QLabel#workspaceContext { color: #777b72; font-size: 10px; }
QLabel#pageIndicator { color: #bfc1ba; padding: 0 7px; font-size: 12px; }
QLabel#welcomeKicker { color: #b78a5d; }
QLabel#welcomeTitle { color: #ece9e0; }
QLabel#welcomeSubtitle { color: #9a9d94; }
QLabel#welcomeHint { color: #6f736b; }
QPushButton#primaryOpenButton {
    background: #b78a5d;
    color: #171713;
    border: 1px solid #c59a6f;
    border-radius: 7px;
    padding: 11px 19px;
    font-family: "Segoe UI";
    font-size: 12px;
    font-weight: 600;
}
QPushButton#primaryOpenButton:hover { background: #c69a6d; border-color: #d3ab82; }
QPushButton#primaryOpenButton:pressed { background: #a7794f; }
QDialog, QMessageBox { background: #232521; color: #d6d7d0; }
QDialog QLabel, QMessageBox QLabel { color: #d6d7d0; }
QLabel#styleInfo { color: #cfaa82; font-style: italic; }
QPlainTextEdit {
    background: #181a17;
    color: #e4e3dd;
    border: 1px solid #454941;
    border-radius: 5px;
    padding: 5px;
}
QDoubleSpinBox {
    background: #282b26;
    color: #d6d7d0;
    border: 1px solid #454941;
    border-radius: 5px;
    padding: 4px 7px;
}
QPushButton {
    background: #363a33;
    color: #e0e1da;
    border: 1px solid #464b42;
    border-radius: 5px;
    padding: 7px 18px;
}
QPushButton:hover { background: #41463d; }
QPushButton:default { background: #9c714b; color: #171713; border-color: #b78a5d; }
QPushButton:default:hover { background: #b08158; }
"""


class _OcrSignals(QObject):
    finished = Signal(int, int, object, str)


class _OcrJob(QRunnable):
    """OCR'yi arayüzü dondurmadan bağımsız belge kopyasında çalıştırır."""

    def __init__(self, token: int, page_no: int, pdf_bytes: bytes) -> None:
        super().__init__()
        self.token = token
        self.page_no = page_no
        self.pdf_bytes = pdf_bytes
        self.signals = _OcrSignals()

    @Slot()
    def run(self) -> None:
        try:
            lines = recognize_pdf_page(self.pdf_bytes, self.page_no)
            error = ""
        except Exception as exc:
            lines = []
            error = str(exc)
        try:
            self.signals.finished.emit(self.token, self.page_no, lines, error)
        except RuntimeError:
            # Pencere OCR sürerken kapatılmış olabilir; sonuç artık tüketilemez.
            pass


class TextDialog(QDialog):
    """Yazı ekleme / değiştirme dialoğu; algılanan stili gösterir."""

    def __init__(self, parent, title: str, style: TextStyle,
                 initial_text: str = "", old_text: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)

        info = QLabel(f"Eşleştirilen stil:  {style.describe()}")
        info.setObjectName("styleInfo")
        layout.addWidget(info)

        if old_text is not None:
            layout.addWidget(QLabel("Seçilen alandaki mevcut metin:"))
            old_view = QPlainTextEdit(old_text)
            old_view.setReadOnly(True)
            old_view.setMaximumHeight(70)
            layout.addWidget(old_view)
            layout.addWidget(QLabel("Yeni metin (boş bırakılırsa alan sadece silinir):"))
        else:
            layout.addWidget(QLabel("Eklenecek metin:"))

        self.text_edit = QPlainTextEdit(initial_text)
        self.text_edit.setMinimumHeight(80)
        layout.addWidget(self.text_edit)

        form = QFormLayout()
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4, 96)
        self.size_spin.setDecimals(1)
        self.size_spin.setValue(style.size)
        self.size_spin.setSuffix(" pt")
        form.addRow("Font boyutu:", self.size_spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.text_edit.setFocus()

    def values(self) -> tuple[str, float]:
        return self.text_edit.toPlainText(), self.size_spin.value()


class MainWindow(QMainWindow):
    MODE_NONE = "none"
    MODE_DELETE = "delete"
    MODE_ADD = "add"
    MODE_OBJECT = "object"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Düzenleyici")
        self.resize(1180, 820)
        self.setMinimumSize(900, 650)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        self.setStyleSheet(DARK_QSS)

        self.pdf = PdfDocument()
        self.mode = self.MODE_NONE
        self._document_token = 0
        self._ocr_pool = QThreadPool(self)
        self._ocr_pool.setMaxThreadCount(1)
        self._ocr_jobs: dict[tuple[int, int], _OcrJob] = {}
        self._pending_text_click: tuple[int, int, fitz.Point] | None = None

        self.view = ContinuousView()
        self.view.rect_selected.connect(self.on_rect_selected)
        self.view.point_clicked.connect(self.on_point_clicked)
        self.view.text_clicked.connect(self.on_text_clicked)
        self.view.edit_committed.connect(self.on_edit_committed)
        self.view.current_page_changed.connect(self.on_current_page_changed)
        self.view.object_selected.connect(self.on_object_selected)
        self.view.object_transform_requested.connect(self.on_object_transform)

        self.welcome = WelcomePage()
        self.welcome.open_requested.connect(self.open_pdf)
        self.welcome.file_dropped.connect(self.load_path)

        self.workspace = QStackedWidget()
        self.workspace.setObjectName("workspaceStack")
        self.workspace.addWidget(self.welcome)
        self.workspace.addWidget(self.view)
        self.workspace.setCurrentWidget(self.welcome)
        self.setCentralWidget(self.workspace)
        self._edit_old_text: str | None = None  # inline editördeki orijinal metin

        self._build_toolbar()
        self._update_ui()

    # ---------- kurulum ----------

    def _build_toolbar(self) -> None:
        tb = QToolBar("Araçlar")
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        brand = QWidget(tb)
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(2, 0, 7, 0)
        brand_layout.setSpacing(9)
        badge = QLabel(brand)
        badge.setPixmap(pdf_badge_pixmap(31))
        brand_layout.addWidget(badge)
        brand_copy = QWidget(brand)
        brand_copy_layout = QVBoxLayout(brand_copy)
        brand_copy_layout.setContentsMargins(0, 0, 0, 0)
        brand_copy_layout.setSpacing(0)
        brand_title = QLabel("PDF DÜZENLEYİCİ", brand_copy)
        brand_title.setObjectName("brandTitle")
        self.workspace_context = QLabel("YENİ ÇALIŞMA ALANI", brand_copy)
        self.workspace_context.setObjectName("workspaceContext")
        brand_copy_layout.addWidget(brand_title)
        brand_copy_layout.addWidget(self.workspace_context)
        brand_layout.addWidget(brand_copy)
        tb.addWidget(brand)
        tb.addSeparator()

        self.act_open = QAction(create_line_icon("open", COPPER), "Aç", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_pdf)
        tb.addAction(self.act_open)
        tb.widgetForAction(self.act_open).setObjectName("openToolButton")

        self.act_save = QAction(create_line_icon("save"), "Kaydet", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.triggered.connect(self.save_pdf)
        tb.addAction(self.act_save)

        self.act_save_as = QAction(create_line_icon("save-as"), "Farklı kaydet", self)
        self.act_save_as.triggered.connect(self.save_pdf_as)
        tb.addAction(self.act_save_as)

        self.act_undo = QAction(create_line_icon("undo"), "Geri al", self)
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.act_undo.triggered.connect(self.undo)
        tb.addAction(self.act_undo)

        tb.addSeparator()

        self.act_delete = QAction(create_line_icon("delete"), "Sil", self)
        self.act_delete.setToolTip(
            "Alanı seçin; taranmış sayfalarda zemin dokusu korunarak silinir")
        self.act_add = QAction(create_line_icon("type"), "Yazı ekle", self)
        self.act_add.setToolTip("Yazının başlayacağı noktaya tıklayın; "
                                "satırdaki stil otomatik eşleştirilir")
        self.act_image = QAction(create_line_icon("image"), "Görsel ekle", self)
        self.act_image.setToolTip("Sayfaya yeni resim veya logo ekleyin")
        self.act_image.triggered.connect(self.add_image)
        self.act_object = QAction(create_line_icon("object", COPPER),
                                  "Nesne", self)
        self.act_object.setToolTip(
            "Tablo, görsel veya şekli seçin; içinden taşıyın, köşelerden boyutlandırın")
        self._mode_actions = {
            self.MODE_DELETE: self.act_delete,
            self.MODE_ADD: self.act_add,
            self.MODE_OBJECT: self.act_object,
        }
        for mode, act in self._mode_actions.items():
            act.setCheckable(True)
            act.triggered.connect(
                lambda checked, m=mode: self.set_mode(
                    m if checked else self.MODE_NONE))
            tb.addAction(act)
        tb.addAction(self.act_image)

        tb.addSeparator()

        spacer = QWidget(tb)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.page_label = QLabel("- / -")
        self.page_label.setObjectName("pageIndicator")
        tb.addWidget(self.page_label)

        self.zoom_box = QComboBox()
        for z in ZOOM_LEVELS:
            self.zoom_box.addItem(f"%{int(z * 100)}", z)
        self.zoom_box.setCurrentIndex(ZOOM_LEVELS.index(self.view.zoom))
        self.zoom_box.currentIndexChanged.connect(self.on_zoom_changed)
        tb.addWidget(self.zoom_box)

        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage("Bir PDF açın (Ctrl+O).")

    # ---------- durum ----------

    def _update_ui(self) -> None:
        loaded = self.pdf.doc is not None
        for act in (self.act_save, self.act_save_as, self.act_delete,
                    self.act_add, self.act_image, self.act_object):
            act.setEnabled(loaded)
        self.act_undo.setEnabled(loaded and self.pdf.can_undo)
        if loaded:
            self.page_label.setText(
                f"{self.view.current_page + 1} / {self.pdf.page_count}")
            name = os.path.basename(self.pdf.path) if self.pdf.path else "adsız"
            self.workspace_context.setText(name.upper())
            star = " •" if self.pdf.dirty else ""
            self.setWindowTitle(f"{name}{star} — PDF Düzenleyici")
        else:
            self.page_label.setText("- / -")
            self.workspace_context.setText("YENİ ÇALIŞMA ALANI")
            self.setWindowTitle("PDF Düzenleyici")

    def set_mode(self, mode: str) -> None:
        if mode not in (self.MODE_NONE, self.MODE_DELETE,
                        self.MODE_ADD, self.MODE_OBJECT):
            raise ValueError(f"Geçersiz araç modu: {mode}")
        self.mode = mode
        # QActionGroup'un otomatik uncheck sırası, görünür seçimle gerçek modu
        # ayırabiliyordu. Bütün düğmeleri tek noktadan ve sinyalleri geçici
        # engelleyerek kesin biçimde eşitle.
        for action_mode, action in getattr(self, "_mode_actions", {}).items():
            previous = action.blockSignals(True)
            action.setChecked(action_mode == mode)
            action.blockSignals(previous)
        view_mode = {
            self.MODE_NONE: "edit",
            self.MODE_DELETE: "delete",
            self.MODE_ADD: "add",
            self.MODE_OBJECT: "object",
        }[mode]
        self.view.set_interaction_mode(view_mode)
        messages = {
            self.MODE_NONE: "Metni düzenlemek için herhangi bir satıra tıklayın.",
            self.MODE_DELETE: "Silinecek alanı sürükleyerek seçin — zemin otomatik onarılır.",
            self.MODE_ADD: "Yazının başlayacağı noktaya tıklayın "
                           "(satırdaki font/boyut/renk otomatik eşleştirilir).",
            self.MODE_OBJECT: "Nesneyi çerçeveleyin; içinden taşıyın, "
                              "köşe ve kenarlardan boyutlandırın.",
        }
        self.statusBar().showMessage(messages[mode])

    # ---------- dosya işlemleri ----------

    def open_pdf(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "PDF Aç", "",
                                              "PDF Dosyaları (*.pdf)")
        if path:
            self.load_path(path)

    def load_path(self, path: str) -> None:
        try:
            self.pdf.open(path)
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"PDF açılamadı:\n{e}")
            return
        self._document_token += 1
        self._pending_text_click = None
        self.view.set_document(self.pdf)
        self.set_mode(self.MODE_NONE)
        self.workspace.setCurrentWidget(self.view)
        self._update_ui()
        if self.pdf.page_count and self.pdf.page_needs_ocr(0):
            self.statusBar().showMessage(
                "Taranmış belge algılandı — metin satırları hazırlanıyor…")
            self._start_ocr(0)
        else:
            self.statusBar().showMessage(
                "Açıldı — metni düzenlemek için herhangi bir satıra tıklayın.")

    def _start_ocr(self, page_no: int) -> None:
        if self.pdf.doc is None or self.pdf.ocr_ready(page_no):
            return
        key = (self._document_token, page_no)
        if key in self._ocr_jobs:
            return
        if not ocr_is_available():
            self.statusBar().showMessage(
                "Bu taranmış sayfa için Türkçe OCR bileşeni bulunamadı.")
            return
        job = _OcrJob(self._document_token, page_no, self.pdf.snapshot_bytes())
        job.signals.finished.connect(self._on_ocr_finished)
        self._ocr_jobs[key] = job
        self._ocr_pool.start(job)

    @Slot(int, int, object, str)
    def _on_ocr_finished(self, token: int, page_no: int,
                         lines: object, error: str) -> None:
        self._ocr_jobs.pop((token, page_no), None)
        if token != self._document_token or self.pdf.doc is None:
            return
        if error:
            log.error("ocr hatasi sayfa=%s: %s", page_no, error)
            self.statusBar().showMessage(f"Metin tanıma başarısız: {error}")
            return
        try:
            self.pdf.set_ocr_lines(page_no, list(lines))
        except Exception as exc:
            log.error("ocr sonucunu hazirlama hatasi:\n%s", traceback.format_exc())
            self.statusBar().showMessage(f"Metin haritası hazırlanamadı: {exc}")
            return
        count = self.pdf.ocr_line_count(page_no)
        self.statusBar().showMessage(
            f"{count} satır tanındı — düzenlemek için metne tıklayın.")
        pending = self._pending_text_click
        if pending is not None and pending[0] == token and pending[1] == page_no:
            self._pending_text_click = None
            self.on_text_clicked(page_no, pending[2])

    def save_pdf(self) -> None:
        if self.pdf.doc is None:
            return
        try:
            self.pdf.save()
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Kaydedilemedi:\n{e}")
            return
        self._update_ui()
        self.statusBar().showMessage(f"Kaydedildi: {self.pdf.path}")

    def save_pdf_as(self) -> None:
        if self.pdf.doc is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Farklı Kaydet",
                                              self.pdf.path or "",
                                              "PDF Dosyaları (*.pdf)")
        if not path:
            return
        try:
            self.pdf.save(path)
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Kaydedilemedi:\n{e}")
            return
        self._update_ui()
        self.statusBar().showMessage(f"Kaydedildi: {path}")

    def undo(self) -> None:
        if self.pdf.can_undo:
            self.pdf.undo()
            self._document_token += 1
            self._pending_text_click = None
            self.view.refresh_all()
            self._update_ui()
            if self.pdf.page_needs_ocr(self.view.current_page):
                self._start_ocr(self.view.current_page)
            self.statusBar().showMessage("Son işlem geri alındı.")

    def on_zoom_changed(self, index: int) -> None:
        self.view.set_zoom(self.zoom_box.itemData(index))

    def on_current_page_changed(self, page_no: int) -> None:
        self._update_ui()
        if (self.pdf.doc is not None and self.pdf.page_needs_ocr(page_no)
                and not self.pdf.ocr_ready(page_no)):
            self._start_ocr(page_no)

    # ---------- düzenleme işlemleri ----------

    def on_rect_selected(self, page_no: int, rect: fitz.Rect) -> None:
        if self.mode == self.MODE_DELETE:
            reply = QMessageBox.question(
                self, "Alanı Sil",
                "Seçilen alandaki içerik kalıcı olarak silinsin mi?")
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    self.pdf.delete_area(page_no, rect)
                except Exception as exc:
                    log.error("delete_area hatasi:\n%s", traceback.format_exc())
                    QMessageBox.critical(
                        self, "Silme Hatası",
                        "Alan silinemedi (ayrıntı debug.log'da):\n\n" + str(exc))
                    return
                self._document_token += 1
                self.view.refresh_page(page_no)
                self._update_ui()
                self.statusBar().showMessage(
                    "Alan silindi; tarama zemini çevre dokusuyla onarıldı.")
        elif self.mode == self.MODE_NONE:
            center = fitz.Point((rect.x0 + rect.x1) / 2,
                                (rect.y0 + rect.y1) / 2)
            self.on_text_clicked(page_no, center)

    def on_text_clicked(self, page_no: int, point: fitz.Point) -> None:
        """Varsayılan davranış: kelimeye tıklansa bile tüm satırı düzenle."""
        if self.mode != self.MODE_NONE or self.pdf.doc is None:
            return
        if self.pdf.page_needs_ocr(page_no) and not self.pdf.ocr_ready(page_no):
            self._pending_text_click = (self._document_token, page_no, point)
            self._start_ocr(page_no)
            self.statusBar().showMessage(
                "Bu sayfa tarama — Türkçe metin satırları tanınıyor…")
            return
        hit = self.pdf.text_block_at_point(page_no, point)
        if hit is None:
            hit = self.pdf.blank_cell_at_point(page_no, point)
            if hit is None:
                self.statusBar().showMessage(
                    "Burada düzenlenebilir bir satır veya tablo hücresi bulunamadı.")
                return
        self._edit_old_text = hit.text
        self.view.open_editor(page_no, hit, point)
        source_note = "OCR" if hit.source == "ocr" else "PDF"
        line_count = len(hit.lines) if isinstance(hit, TextBlockHit) else 1
        save_hint = "Ctrl+Enter" if line_count > 1 else "Enter"
        self.statusBar().showMessage(
            f"{line_count} satırlık metin akışı · {source_note} · "
            f"{hit.style.describe()} · {save_hint}: kaydet, Esc: vazgeç.")

    def on_edit_committed(self, page_no: int, hit: TextHit | TextBlockHit,
                          new_text: str, style) -> None:
        old = self._edit_old_text
        self._edit_old_text = None
        log.info("commit: sayfa=%s kaynak=%s rect=%s eski=%r yeni=%r",
                 page_no, hit.source, hit.rect, old, new_text)
        if new_text == old:
            self.statusBar().showMessage(
                "Değişiklik yapılmadı (metin aynı kaldı).")
            log.info("commit atlandi: metin ayni")
            return
        try:
            if isinstance(hit, TextBlockHit) and len(hit.lines) > 1:
                used_size = self.pdf.replace_text_block(page_no, hit, new_text)
            elif isinstance(hit, TextBlockHit):
                # Tek OCR satirini paragraf compositoruna sokmak her revizyonda
                # tum satiri yeniden diziyor ve eski PNG katmanlarini biriktiriyordu.
                used_size = self.pdf.replace_text_hit(
                    page_no, hit.lines[0], new_text)
            else:
                used_size = self.pdf.replace_text_hit(page_no, hit, new_text)
        except Exception as e:
            log.error("replace_text hatasi:\n%s", traceback.format_exc())
            QMessageBox.critical(
                self, "Satır Düzenleme Hatası",
                "Satır düzenlenemedi (ayrıntı debug.log'da):\n\n" + str(e))
            return
        log.info("replace_text tamam")
        self._document_token += 1
        self.view.refresh_page(page_no)
        self._update_ui()
        if (self.pdf.page_needs_ocr(page_no)
                and not self.pdf.ocr_ready(page_no)):
            self._start_ocr(page_no)
        size_note = f" · {used_size:.1f} pt" if used_size is not None else ""
        self.statusBar().showMessage(
            "Değişmeyen kelimeler orijinal görünümüyle korundu" + size_note
            + " — Kaydet (Ctrl+S).")

    def on_object_selected(self, page_no: int, rect: fitz.Rect) -> None:
        self.statusBar().showMessage(
            "Nesne seçildi — içinden sürükleyerek taşıyın; "
            "köşe veya kenar tutamaçlarından boyutlandırın.")

    def on_object_transform(self, page_no: int, source: fitz.Rect,
                            target: fitz.Rect) -> None:
        self.statusBar().showMessage("Nesne taşınıyor ve sayfa zemini onarılıyor…")
        try:
            self.pdf.move_region(page_no, source, target)
        except Exception as exc:
            log.error("move_region hatasi:\n%s", traceback.format_exc())
            self.view.clear_object_selection()
            QMessageBox.critical(
                self, "Nesne Taşıma Hatası",
                "Nesne taşınamadı (ayrıntı debug.log'da):\n\n" + str(exc))
            return
        self._document_token += 1
        self.view.refresh_page(page_no)
        self.view.set_object_selection(page_no, target)
        self._update_ui()
        self.statusBar().showMessage(
            "Nesne taşındı/boyutlandırıldı — Kaydet (Ctrl+S).")

    def on_point_clicked(self, page_no: int, point: fitz.Point) -> None:
        if self.mode != self.MODE_ADD:
            return

        # Kullanıcı mevcut bir metne tıkladıysa, üstüne yazı bindirmek yerine
        # doğrudan o metni düzenleme moduna al (Adobe Acrobat davranışı).
        hit = self.pdf.text_block_at_point(page_no, point)
        if hit is None:
            hit = self.pdf.blank_cell_at_point(page_no, point)
        if hit is not None:
            self.set_mode(self.MODE_NONE)
            self._edit_old_text = hit.text
            self.view.open_editor(page_no, hit, point)
            source_note = "OCR" if hit.source == "ocr" else "PDF"
            line_count = len(hit.lines) if isinstance(hit, TextBlockHit) else 1
            save_hint = "Ctrl+Enter" if line_count > 1 else "Enter"
            self.statusBar().showMessage(
                f"{line_count} satırlık metin akışı · {source_note} · "
                f"{hit.style.describe()} · {save_hint}: kaydet, Esc: vazgeç.")
            return

        style = self.pdf.style_at_point(page_no, point)
        dlg = TextDialog(self, "Yazı Ekle", style)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text, fontsize = dlg.values()
            if text.strip():
                try:
                    result = self.pdf.insert_text(
                        page_no, point, text, style=style, fontsize=fontsize)
                    if isinstance(result, tuple):
                        used_size, inserted_rect = result
                    else:
                        used_size, inserted_rect = result, None
                except Exception as exc:
                    log.error("insert_text hatasi:\n%s", traceback.format_exc())
                    QMessageBox.critical(
                        self, "Yazı Ekleme Hatası",
                        "Yazı eklenemedi (ayrıntı debug.log'da):\n\n" + str(exc))
                    return
                self._document_token += 1
                self.view.refresh_page(page_no)
                self.set_mode(self.MODE_OBJECT)
                if inserted_rect is not None:
                    self.view.set_object_selection(page_no, inserted_rect)
                self._update_ui()
                self.statusBar().showMessage(
                    f"Yazı eklendi ({used_size:.1f} pt) — "
                    "Gerekiyorsa sürükleyerek taşıyın veya yeniden konumlandırın.")

    def add_image(self) -> None:
        """Kullanıcının seçtiği görseli sayfaya ekler ve nesne düzenleme moduna geçer."""
        if self.pdf.doc is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Görsel Seç", "",
            "Görseller (*.png *.jpg *.jpeg *.bmp *.webp *.tiff);;Tüm Dosyalar (*)")
        if not path:
            return
        from PySide6.QtGui import QImageReader
        reader = QImageReader(path)
        img_size = reader.size()
        w = max(40, img_size.width())
        h = max(40, img_size.height())
        page_no = self.view.current_page
        page_rect = self.pdf.doc[page_no].rect
        # Sayfanın ortasına estetik ölçekte yerleştir (~180-260 pt genişlik)
        target_w = min(240.0, page_rect.width * 0.45)
        target_h = target_w * (h / w)
        cx = page_rect.width / 2
        cy = page_rect.height / 2
        target_rect = fitz.Rect(cx - target_w / 2, cy - target_h / 2,
                                cx + target_w / 2, cy + target_h / 2)
        try:
            self.pdf.insert_image(page_no, target_rect, path)
            self._document_token += 1
            self.view.refresh_page(page_no)
            # Doğrudan nesne moduna geç ve eklenen görseli seçili yap (Adobe Acrobat tarzı)
            self.set_mode(self.MODE_OBJECT)
            self.view.set_object_selection(page_no, target_rect)
            self._update_ui()
            self.statusBar().showMessage(
                "Görsel eklendi! Tutamaçlardan boyutlandırabilir ve taşıyabilirsiniz — Kaydet (Ctrl+S).")
        except Exception as exc:
            log.error("insert_image hatasi:\n%s", traceback.format_exc())
            QMessageBox.critical(self, "Görsel Ekleme Hatası",
                                 "Görsel eklenemedi:\n\n" + str(exc))

    # ---------- kapanış ----------

    def _confirm_discard(self) -> bool:
        if not self.pdf.dirty:
            return True
        reply = QMessageBox.warning(
            self, "Kaydedilmemiş Değişiklikler",
            "Kaydedilmemiş değişiklikler var. Devam edilsin mi?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def closeEvent(self, ev) -> None:
        if self._confirm_discard():
            ev.accept()
        else:
            ev.ignore()
