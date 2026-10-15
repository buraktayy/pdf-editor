from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPainter,
                           QPixmap, QTextCursor)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from main_window import MainWindow
from ocr_engine import (OcrGlyph, OcrLine, OcrWord, _normalize_percentage_words,
                        _subprocess_window_options, ocr_is_available,
                        recognize_pdf_page)
from page_view import BlockEditor, InlineEditor
from pdf_document import PdfDocument, TextStyle


class PdfEditorRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="pdf-editor-tests-")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _path(self, name: str) -> str:
        return os.path.join(self.tempdir.name, name)

    @staticmethod
    def _image_pixels(image: QImage) -> bytes:
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        return bytes(rgba.constBits())

    @classmethod
    def _widget_pixels(cls, widget) -> bytes:
        image = QImage(widget.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        widget.render(image)
        return cls._image_pixels(image)

    def _make_scan_pdf(self, path: str) -> OcrLine:
        fontfile = r"C:\Windows\Fonts\times.ttf"
        source = fitz.open()
        source_page = source.new_page(width=600, height=800)
        source_page.draw_rect(source_page.rect, color=None,
                              fill=(.965, .96, .95), overlay=False)
        line_text = "İşletmelerde mesleki eğitim merkezlerinde devam eder yazıyı"
        source_page.insert_text(
            (72, 190), line_text,
            fontname="BODY", fontfile=fontfile, fontsize=17,
            color=(.12, .12, .11))
        extracted_words = source_page.get_text("words")
        tokens = line_text.split()
        ocr_words = tuple(
            OcrWord(rect=fitz.Rect(item[:4]), text=token, confidence=99.0)
            for item, token in zip(extracted_words, tokens)
        )
        raw = source_page.get_text("rawdict")
        ocr_glyphs = tuple(
            OcrGlyph(rect=fitz.Rect(character["bbox"]),
                     text=character["c"])
            for block in raw.get("blocks", []) if block.get("type") == 0
            for source_line in block.get("lines", [])
            for span in source_line.get("spans", [])
            for character in span.get("chars", [])
            if character.get("c", "").strip()
        )
        line_rect = fitz.Rect(ocr_words[0].rect)
        for word in ocr_words[1:]:
            line_rect.include_rect(word.rect)
        ocr_line = OcrLine(rect=line_rect, block_rect=fitz.Rect(line_rect),
                           text=line_text, words=ocr_words, confidence=99.0,
                           glyphs=ocr_glyphs)
        source_page.draw_rect(fitz.Rect(390, 50, 545, 170),
                              color=(.15, .15, .14), width=1.2)
        source_page.draw_line((390, 92), (545, 92),
                              color=(.15, .15, .14), width=1.0)
        source_page.draw_line((465, 50), (465, 170),
                              color=(.15, .15, .14), width=1.0)
        source_page.insert_text((398, 82), "TABLO", fontname="TITLE",
                                fontfile=fontfile, fontsize=14,
                                color=(.12, .12, .11))
        source_page.insert_text((398, 280), "KORUNACAK METİN",
                                fontname="KEEP", fontfile=fontfile,
                                fontsize=14, color=(.12, .12, .11))
        pixmap = source_page.get_pixmap(matrix=fitz.Matrix(2, 2),
                                        alpha=False, colorspace=fitz.csRGB)
        source.close()
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.insert_image(page.rect, pixmap=pixmap)
        document.save(path)
        document.close()
        return ocr_line

    def test_native_click_returns_complete_line(self) -> None:
        path = self._path("native.pdf")
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        fontfile = r"C:\Windows\Fonts\times.ttf"
        page.insert_text((72, 110), "İşletmelerde mesleki eğitim devam eder",
                         fontname="TNR", fontfile=fontfile, fontsize=12)
        document.save(path)
        document.close()

        pdf = PdfDocument()
        pdf.open(path)
        word_rect = pdf.doc[0].search_for("mesleki")[0]
        point = fitz.Point((word_rect.x0 + word_rect.x1) / 2,
                           (word_rect.y0 + word_rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.text, "İşletmelerde mesleki eğitim devam eder")
        self.assertGreater(hit.cursor_index(point.x), len("İşletmelerde "))
        pdf.replace_text(0, hit.rect, hit.text.replace("mesleki", "uygulamalı"),
                         style=hit.style, layout_rect=hit.layout_rect,
                         baseline=hit.baseline, align=hit.align)
        self.assertIn("uygulamalı", pdf.doc[0].get_text().replace("\xa0", " "))
        pdf.close()

    def test_inline_editor_preserves_source_until_text_changes(self) -> None:
        canvas = QImage(560, 70, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#ebe9e3"))
        painter = QPainter(canvas)
        painter.setPen(QColor("#252522"))
        source_font = QFont("Times New Roman")
        source_font.setPixelSize(18)
        painter.setFont(source_font)
        painter.drawText(8, 43, "İşletmelerde mesleki eğitim devam eder")
        painter.end()

        page = QLabel()
        page.setPixmap(QPixmap.fromImage(canvas))
        page.resize(canvas.size())
        editor = InlineEditor(
            page,
            "İşletmelerde mesleki eğitim devam eder",
            source_font,
            "rgb(37,37,34)",
            "rgb(235,233,227)",
            18,
            0,
            preserve_source=True,
        )
        editor.setGeometry(8, 20, 520, 30)
        editor.show()
        editor.setFocus()
        self.app.processEvents()

        self.assertTrue(editor.source_pixels_preserved)
        self.assertIn("background: transparent", editor.styleSheet())
        self.assertIn("rgba(0, 0, 0, 0)", editor.styleSheet())
        self.assertFalse(hasattr(editor, "_caret_timer"),
                         "QLineEdit'in doğal imlecine ikinci imleç eklenmemeli")
        editor.textEdited.emit(editor.text() + "x")
        self.assertFalse(editor.source_pixels_preserved)
        self.assertIn("rgb(235,233,227)", editor.styleSheet())
        editor.deleteLater()
        page.deleteLater()

    def test_inline_editor_mask_attachment_preserves_pristine_page_pixels(
            self) -> None:
        canvas = QImage(560, 70, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#ebe9e3"))
        painter = QPainter(canvas)
        painter.setPen(QColor("#252522"))
        source_font = QFont("Times New Roman")
        source_font.setPixelSize(18)
        painter.setFont(source_font)
        painter.drawText(8, 43, "İşletmelerde mesleki eğitim devam eder")
        painter.end()

        page = QLabel()
        page.setPixmap(QPixmap.fromImage(canvas))
        page.resize(canvas.size())
        page.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        page.show()
        self.app.processEvents()
        pristine_page = self._widget_pixels(page)
        pristine_source = self._image_pixels(page.pixmap().toImage())

        editor = InlineEditor(
            page,
            "İşletmelerde mesleki eğitim devam eder",
            source_font,
            "rgb(37,37,34)",
            "rgb(235,233,227)",
            18,
            0,
            preserve_source=True,
        )
        editor.setGeometry(8, 20, 520, 30)
        mask = QLabel(page)
        mask.setGeometry(editor.geometry())
        mask.setStyleSheet("background: rgb(235,233,227);")
        mask.hide()

        editor.set_background_mask(mask)
        editor.show()
        page.setFocus()
        editor.clearFocus()
        self.app.processEvents()

        self.assertTrue(editor.source_pixels_preserved)
        self.assertTrue(mask.isHidden(),
                        "Maskeyi bağlamak canlı düzenlemeyi başlatmamalı")
        self.assertEqual(pristine_source,
                         self._image_pixels(page.pixmap().toImage()),
                         "Editör açılışı sayfanın kaynak pixmap'ini değiştirmemeli")
        self.assertEqual(pristine_page, self._widget_pixels(page),
                         "İlk tuştan önce sayfa pikselleri yeniden çizilmemeli")
        editor.deleteLater()
        mask.deleteLater()
        page.deleteLater()

    def test_block_editor_mask_attachment_preserves_pristine_page_pixels(
            self) -> None:
        canvas = QImage(620, 150, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#f8f7f4"))
        painter = QPainter(canvas)
        painter.setPen(QColor("#191918"))
        source_font = QFont("Times New Roman")
        source_font.setPixelSize(18)
        painter.setFont(source_font)
        painter.drawText(18, 58, "Birinci satır doğal akışını korur.")
        painter.drawText(18, 88, "İkinci satır da kaynak piksel olarak kalır.")
        painter.end()

        page = QLabel()
        page.setPixmap(QPixmap.fromImage(canvas))
        page.resize(canvas.size())
        page.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        page.show()
        self.app.processEvents()
        pristine_page = self._widget_pixels(page)
        pristine_source = self._image_pixels(page.pixmap().toImage())

        editor = BlockEditor(
            page,
            "Birinci satır doğal akışını korur.\n"
            "İkinci satır da kaynak piksel olarak kalır.",
            source_font,
            "rgb(25,25,24)",
            "rgb(248,247,244)",
            0,
            preserve_source=True,
            line_height_px=30.0,
        )
        editor.setGeometry(8, 20, 580, 90)
        mask = QLabel(page)
        mask.setGeometry(editor.geometry())
        mask.setStyleSheet("background: rgb(248,247,244);")
        mask.hide()

        editor.set_background_mask(mask)
        editor.show()
        page.setFocus()
        editor.clearFocus()
        self.app.processEvents()

        self.assertTrue(editor.source_pixels_preserved)
        self.assertAlmostEqual(
            editor.document().firstBlock().blockFormat().lineHeight(),
            30.0, places=1,
            msg="Canlı paragraf kaynak satır aralığını kullanmalı")
        first_block = editor.document().firstBlock()
        second_block = first_block.next()
        first_cursor = QTextCursor(editor.document())
        first_cursor.setPosition(first_block.position())
        second_cursor = QTextCursor(editor.document())
        second_cursor.setPosition(second_block.position())
        first_y = editor.cursorRect(first_cursor).center().y()
        second_y = editor.cursorRect(second_cursor).center().y()
        self.assertAlmostEqual(
            second_y - first_y, 30.0, delta=1.0,
            msg="Editör lineHeight değerini yalnız saklamamalı, çizimde de kullanmalı")
        self.assertTrue(mask.isHidden(),
                        "Maskeyi bağlamak canlı paragraf çizimini başlatmamalı")
        self.assertEqual(pristine_source,
                         self._image_pixels(page.pixmap().toImage()),
                         "Paragraf editörü kaynak pixmap'i değiştirmemeli")
        self.assertEqual(pristine_page, self._widget_pixels(page),
                         "İlk tuştan önce paragraf görünümü kaynağı örtmemeli")
        editor.deleteLater()
        mask.deleteLater()
        page.deleteLater()

    def test_block_editor_keeps_source_pixels_during_selection_and_arrows(self) -> None:
        page = QLabel()
        page.resize(620, 180)
        page.show()
        font = QFont("Times New Roman")
        font.setPixelSize(18)
        editor = BlockEditor(
            page,
            "Birinci satır doğal akar ve aşağıdaki satıra geçer.\n"
            "İkinci satır yön tuşlarıyla erişilebilir.",
            font, "rgb(30,30,30)", "rgb(248,247,244)", 8,
            preserve_source=True,
        )
        editor.setGeometry(8, 8, 560, 100)
        editor.show()
        editor.setFocus()
        cursor = editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        self.app.processEvents()

        self.assertTrue(editor.source_pixels_preserved)
        cursor.clearSelection()
        cursor.setPosition(0)
        editor.setTextCursor(cursor)
        selected_position = editor.textCursor().position()
        QTest.keyClick(editor, Qt.Key.Key_Down)
        self.app.processEvents()
        self.assertGreater(editor.textCursor().position(), selected_position)
        self.assertTrue(editor.source_pixels_preserved,
                        "Seçim veya yön tuşu font önizlemesini değiştirmemeli")
        QTest.keyClicks(editor, "x")
        self.app.processEvents()
        self.assertFalse(editor.source_pixels_preserved)
        editor.deleteLater()
        page.deleteLater()

    def test_block_editor_live_surface_covers_original_text(self) -> None:
        """İlk tuştan sonra kaynak ve canlı paragraf iki kat çizilmemeli."""
        canvas = QImage(620, 150, QImage.Format.Format_RGB32)
        paper = QColor(248, 247, 244)
        canvas.fill(paper)
        painter = QPainter(canvas)
        painter.setPen(QColor(25, 25, 24))
        source_font = QFont("Times New Roman")
        source_font.setPixelSize(18)
        painter.setFont(source_font)
        painter.drawText(18, 65, "ALTTAN GÖRÜNMEMESİ GEREKEN KAYNAK METİN")
        painter.end()

        page = QLabel()
        page.setPixmap(QPixmap.fromImage(canvas))
        page.resize(canvas.size())
        page.show()
        editor = BlockEditor(
            page,
            "ALTTAN GÖRÜNMEMESİ GEREKEN KAYNAK METİN",
            source_font, "rgb(25,25,24)", "rgb(248,247,244)", 0,
            preserve_source=True,
        )
        editor.setGeometry(8, 8, 580, 105)
        editor.show()
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "x")
        self.app.processEvents()

        self.assertFalse(editor.source_pixels_preserved)
        self.assertTrue(editor.viewport().autoFillBackground())
        self.assertTrue(editor.viewport().testAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent))
        snapshot = QImage(page.size(), QImage.Format.Format_RGB888)
        snapshot.fill(paper)
        page.render(snapshot)
        dark_pixels = 0
        for y in range(40, 78):
            for x in range(90, 550):
                color = snapshot.pixelColor(x, y)
                if color.red() + color.green() + color.blue() < 600:
                    dark_pixels += 1
        samples = [snapshot.pixelColor(x, y).getRgb()[:3]
                   for x, y in ((10, 10), (100, 50), (300, 60), (540, 70))]
        self.assertLess(
            dark_pixels, 8,
            "Canlı zemin alttaki kaynak gliflerini örtmedi "
            f"(boyut={snapshot.width()}x{snapshot.height()}, örnek={samples})")
        editor.deleteLater()
        page.deleteLater()

    def test_editors_hide_before_commit_handler_runs(self) -> None:
        """Pahalı PDF yazma yuvası çalışırken eski editör görünür kalmamalı."""
        page = QLabel()
        page.resize(620, 180)
        page.show()
        font = QFont("Times New Roman")
        font.setPixelSize(18)
        editor = BlockEditor(
            page, "Bir paragraf doğal biçimde düzenlenir.", font,
            "rgb(30,30,30)", "rgb(248,247,244)", 5,
            preserve_source=True,
        )
        editor.setGeometry(8, 8, 560, 100)
        editor.show()
        editor.setFocus()
        visible_during_commit: list[bool] = []
        editor.committed.connect(
            lambda _text: visible_during_commit.append(editor.isVisible()))

        QTest.keyClick(editor, Qt.Key.Key_Enter,
                       Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()

        self.assertEqual(visible_during_commit, [False])
        editor.deleteLater()
        page.deleteLater()

    def test_editor_background_uses_page_texture_without_mutating_pdf(self) -> None:
        path = self._path("editor-texture.pdf")
        ocr_line = self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [ocr_line])
        word = next(item for item in ocr_line.words if item.text == "mesleki")
        point = fitz.Point((word.rect.x0 + word.rect.x1) / 2,
                           (word.rect.y0 + word.rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        self.assertIsNotNone(hit)

        zoom = 2.0
        rendered = pdf.render_page(0, zoom)
        source = QImage(rendered.samples, rendered.width, rendered.height,
                        rendered.stride, QImage.Format.Format_RGB888).copy()
        before_pdf = bytes(rendered.samples)
        x = round((hit.rect.x0 - 8) * zoom)
        y = round((hit.rect.y0 - 12) * zoom)
        width = round((hit.rect.width + 16) * zoom)
        height = round((hit.rect.height + 24) * zoom)
        source_crop = source.copy(x, y, width, height)
        patch = pdf.editor_background_patch(
            0, hit, source, (x, y, width, height))

        self.assertIsNotNone(patch)
        self.assertEqual((patch.width(), patch.height()), (width, height))
        self.assertEqual(patch.pixelColor(2, 2),
                         source_crop.pixelColor(2, 2),
                         "Editör sınırındaki gerçek kâğıt dokusu değişmemeli")

        def dark_pixels(image: QImage) -> int:
            return sum(
                1 for row in range(image.height())
                for column in range(image.width())
                if sum(image.pixelColor(column, row).getRgb()[:3]) < 600)

        self.assertLess(dark_pixels(patch), dark_pixels(source_crop) * .25)
        after_pdf = bytes(pdf.render_page(0, zoom).samples)
        self.assertEqual(before_pdf, after_pdf,
                         "Canlı zemin önizlemesi PDF'yi değiştirmemeli")
        pdf.close()

    def test_ocr_word_click_opens_complete_multiline_block(self) -> None:
        path = self._path("scan-block.pdf")
        first = self._make_scan_pdf(path)
        dy = 24.0
        second_words = tuple(
            OcrWord(rect=fitz.Rect(word.rect.x0, word.rect.y0 + dy,
                                   word.rect.x1, word.rect.y1 + dy),
                    text=word.text, confidence=word.confidence)
            for word in first.words
        )
        second_rect = fitz.Rect(first.rect.x0, first.rect.y0 + dy,
                                first.rect.x1, first.rect.y1 + dy)
        block_rect = fitz.Rect(first.rect)
        block_rect.include_rect(second_rect)
        first = OcrLine(rect=first.rect, block_rect=block_rect,
                        text=first.text, words=first.words,
                        confidence=first.confidence, glyphs=first.glyphs)
        second = OcrLine(rect=second_rect, block_rect=block_rect,
                         text=first.text, words=second_words,
                         confidence=99.0)

        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [first, second])
        target = next(word for word in first.words if word.text == "mesleki")
        point = fitz.Point((target.rect.x0 + target.rect.x1) / 2,
                           (target.rect.y0 + target.rect.y1) / 2)
        block = pdf.text_block_at_point(0, point)
        self.assertIsNotNone(block)
        self.assertEqual(len(block.lines), 2)
        self.assertEqual(block.text.count(first.text), 2)
        self.assertEqual(pdf._wrap_text_block(0, block, block.text),
                         [first.text, first.text])
        pdf.close()

    def test_long_added_text_wraps_before_page_edge(self) -> None:
        path = self._path("bounded-add.pdf")
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        document.save(path)
        document.close()

        pdf = PdfDocument()
        pdf.open(path)
        text = ("uzun metin sayfanın dışına çıkmadan doğal olarak alt satıra "
                "geçmeli ve bütün harfler net kalmalıdır " * 3).strip()
        used_size = pdf.insert_text(
            0, fitz.Point(470, 110), text,
            style=TextStyle("Times New Roman", 12.0, (.1, .1, .1)),
            fontsize=12.0)
        words = pdf.doc[0].get_text("words")
        self.assertTrue(words)
        self.assertLessEqual(max(word[2] for word in words), 585.5)
        self.assertGreater(len({round(word[1], 1) for word in words}), 1)
        self.assertGreaterEqual(used_size, 4.0)
        pdf.close()

    def test_toolbar_modes_switch_and_cancel_deterministically(self) -> None:
        window = MainWindow()
        for action in (window.act_delete, window.act_add, window.act_object):
            action.setEnabled(True)

        window.act_delete.trigger()
        self.assertEqual(window.mode, window.MODE_DELETE)
        self.assertTrue(window.act_delete.isChecked())

        window.act_add.trigger()
        self.assertEqual(window.mode, window.MODE_ADD)
        self.assertFalse(window.act_delete.isChecked())
        self.assertTrue(window.act_add.isChecked())
        self.assertEqual(window.view.interaction_mode, "add")

        window.act_add.trigger()
        self.assertEqual(window.mode, window.MODE_NONE)
        self.assertFalse(window.act_add.isChecked())
        self.assertFalse(window.act_delete.isChecked())
        self.assertFalse(window.act_object.isChecked())
        self.assertEqual(window.view.interaction_mode, "edit")
        window.close()
        window.deleteLater()

    @unittest.skipUnless(os.name == "nt", "Windows konsol seçeneği")
    def test_tesseract_subprocess_is_hidden_on_windows(self) -> None:
        import subprocess

        options = _subprocess_window_options()
        self.assertEqual(options["creationflags"],
                         subprocess.CREATE_NO_WINDOW)
        self.assertTrue(options["startupinfo"].dwFlags
                        & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(options["startupinfo"].wShowWindow,
                         subprocess.SW_HIDE)

    def test_ocr_percentage_confusions_are_fixed_only_in_rate_context(self) -> None:
        def words(*values: str) -> list[OcrWord]:
            return [OcrWord(fitz.Rect(index * 20, 0, index * 20 + 18, 10),
                            value, 88.0)
                    for index, value in enumerate(values)]

        first = _normalize_percentage_words(
            words("net", "tutarının", "9035'i;"))
        second = _normalize_percentage_words(
            words("net", "tutarının", "Y630'undan"))
        omitted = _normalize_percentage_words(
            words("oranının", "35'i"))
        unrelated = _normalize_percentage_words(
            words("belge", "9035'i", "tutarının", "2023'te"))

        self.assertEqual(first[-1].text, "%35'i;")
        self.assertEqual(second[-1].text, "%30'undan")
        self.assertEqual(omitted[-1].text, "%35'i")
        self.assertEqual([word.text for word in unrelated],
                         ["belge", "9035'i", "tutarının", "2023'te"])

    def test_scan_delete_is_textured_and_undo_exact(self) -> None:
        path = self._path("scan-delete.pdf")
        self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        before = bytes(pdf.render_page(0, 1.4).samples)
        area = fitz.Rect(70, 155, 470, 198)
        pdf.delete_area(0, area)
        clip = pdf.doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=area,
                                    alpha=False, colorspace=fitz.csRGB)
        values = clip.samples
        mean = sum(values) / max(len(values), 1)
        self.assertLess(mean, 253.5)  # saf beyaz kutu değil, kâğıt tonu
        pdf.undo()
        after = bytes(pdf.render_page(0, 1.4).samples)
        self.assertEqual(before, after)
        pdf.close()

    def test_scan_suffix_edit_preserves_word_prefix_pixels(self) -> None:
        path = self._path("scan-character-edit.pdf")
        ocr_line = self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [ocr_line])
        target = next(word for word in ocr_line.words
                      if word.text == "merkezlerinde")
        point = fitz.Point((target.rect.x0 + target.rect.x1) / 2,
                           (target.rect.y0 + target.rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        self.assertIsNotNone(hit)
        scan = pdf._full_page_scan_qimage(0)
        self.assertIsNotNone(scan)
        glyphs = pdf._glyphs_for_word(hit, target, 0, scan[2], scan[1])
        self.assertIsNotNone(glyphs)
        prefix_length = len("merkezlerind")
        prefix_rect = fitz.Rect(
            glyphs[0].rect.x0, target.rect.y0,
            glyphs[prefix_length - 1].rect.x1, target.rect.y1)

        scale = 2.0
        before_pixmap = pdf.render_page(0, scale)
        before_image = QImage(
            before_pixmap.samples, before_pixmap.width, before_pixmap.height,
            before_pixmap.stride, QImage.Format.Format_RGB888).copy()
        bx0, by0, bx1, by1 = pdf._image_rect_for_pdf(
            prefix_rect, pdf.doc[0].rect, before_image)
        prefix_before = before_image.copy(bx0, by0, bx1 - bx0, by1 - by0)

        pdf.replace_text_hit(
            0, hit, hit.text.replace("merkezlerinde", "merkezlerindas", 1))
        self.assertEqual(pdf._last_scan_changed_text, "as")
        self.assertEqual(pdf._last_scan_glyph_coverage, 1.0)

        after_pixmap = pdf.render_page(0, scale)
        after_image = QImage(
            after_pixmap.samples, after_pixmap.width, after_pixmap.height,
            after_pixmap.stride, QImage.Format.Format_RGB888).copy()
        prefix_after = after_image.copy(bx0, by0, bx1 - bx0, by1 - by0)
        self.assertEqual(self._image_pixels(prefix_before),
                         self._image_pixels(prefix_after))
        pdf.close()

    def test_scan_y_glyph_keeps_its_descender_and_source_pixels(self) -> None:
        path = self._path("scan-y-glyph.pdf")
        ocr_line = self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [ocr_line])
        target = next(word for word in ocr_line.words if word.text == "devam")
        point = fitz.Point((target.rect.x0 + target.rect.x1) / 2,
                           (target.rect.y0 + target.rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        scan = pdf._full_page_scan_qimage(0)
        self.assertIsNotNone(hit)
        self.assertIsNotNone(scan)
        _xref, image_rect, image = scan
        scale_x = image.width() / image_rect.width
        scale_y = image.height() / image_rect.height
        font = pdf._scan_edit_font(hit.style, target, scale_x, scale_y)
        draws, coverage = pdf._source_glyph_draws(
            0, hit, "y", font, image, image_rect)
        self.assertEqual(coverage, 1.0)
        self.assertEqual(len(draws), 1)
        sprite, _offset_x, offset_y = draws[0]
        self.assertGreater(offset_y + sprite.height(), 0,
                           "y glifinin taban çizgisi altındaki kuyruğu kırpılmamalı")

        pdf.replace_text_hit(0, hit, hit.text.replace("devam", "yayıy", 1))
        self.assertEqual(pdf._last_scan_glyph_coverage, 1.0)
        pdf.close()

    def test_scan_word_edit_preserves_unchanged_source_pixels(self) -> None:
        path = self._path("scan-word-edit.pdf")
        ocr_line = self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [ocr_line])
        target = next(word for word in ocr_line.words
                      if word.text == "mesleki")
        point = fitz.Point((target.rect.x0 + target.rect.x1) / 2,
                           (target.rect.y0 + target.rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        self.assertIsNotNone(hit)
        scan = pdf._full_page_scan_qimage(0)
        self.assertIsNotNone(scan)
        calibrated_font = pdf._scan_edit_font(
            hit.style, target,
            scan[2].width() / scan[1].width,
            scan[2].height() / scan[1].height)
        font_metrics = QFontMetricsF(calibrated_font)
        self.assertTrue(font_metrics.inFontUcs4(ord("ı")))
        self.assertTrue(font_metrics.inFontUcs4(ord("ğ")))

        scan_before = scan
        self.assertIsNotNone(scan_before)
        _xref, image_rect, before_image = scan_before
        prefix_rect = hit.words[0].rect
        px0, py0, px1, py1 = pdf._image_rect_for_pdf(
            prefix_rect, image_rect, before_image)
        prefix_before = before_image.copy(px0, py0, px1 - px0, py1 - py0)
        prefix_bytes = self._image_pixels(prefix_before)
        whole_before = bytes(pdf.render_page(0, 1.25).samples)

        used_size = pdf.replace_text_hit(
            0, hit, hit.text.replace("mesleki", "eğitim"))
        self.assertIsNotNone(used_size)
        self.assertEqual(pdf._last_scan_glyph_coverage, 1.0)
        self.assertTrue(pdf.ocr_ready(0),
                        "Tarama düzenlemesi sentetik sayfayı yeniden OCR etmemeli")
        scan_after = pdf._full_page_scan_qimage(0)
        self.assertIsNotNone(scan_after)
        after_image = scan_after[2]
        prefix_after = after_image.copy(px0, py0, px1 - px0, py1 - py0)
        self.assertEqual(prefix_bytes, self._image_pixels(prefix_after))

        pdf.undo()
        whole_after_undo = bytes(pdf.render_page(0, 1.25).samples)
        self.assertEqual(whole_before, whole_after_undo)
        self.assertTrue(pdf.ocr_ready(0),
                        "Undo sentetik sayfayı yeniden OCR etmemeli")
        restored = pdf.line_at_point(0, point)
        self.assertIsNotNone(restored)
        self.assertIn("mesleki", restored.text)
        pdf.close()

    def test_equal_length_scan_edits_keep_geometry_single_layer_and_undo_scene(
            self) -> None:
        path = self._path("scan-stable-score-like-edit.pdf")
        original_line = self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [original_line])
        target = next(word for word in original_line.words
                      if word.text == "devam")
        point = fitz.Point((target.rect.x0 + target.rect.x1) / 2,
                           (target.rect.y0 + target.rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        self.assertIsNotNone(hit)
        original_rects = [tuple(word.rect) for word in hit.words]
        source_text = pdf._source_ocr_lines[0][0].text

        def visible_placements() -> int:
            page = pdf.doc[0]
            total = 0
            for info in page.get_images(full=True):
                total += sum(1 for rect in page.get_image_rects(int(info[0]))
                             if fitz.Rect(rect).get_area() > 4)
            return total

        self.assertEqual(visible_placements(), 1)
        pdf.replace_text_hit(0, hit, hit.text.replace("devam", "biter", 1))
        self.assertIn("biter", hit.text)
        self.assertEqual(original_rects, [tuple(word.rect) for word in hit.words])
        self.assertTrue(pdf.ocr_ready(0))
        self.assertEqual(visible_placements(), 1)

        second_word = next(word for word in hit.words if word.text == "biter")
        second_point = fitz.Point((second_word.rect.x0 + second_word.rect.x1) / 2,
                                 (second_word.rect.y0 + second_word.rect.y1) / 2)
        second_hit = pdf.line_at_point(0, second_point)
        self.assertIs(second_hit, hit)
        pdf.replace_text_hit(0, second_hit,
                             second_hit.text.replace("biter", "sürer", 1))
        self.assertIn("sürer", second_hit.text)
        self.assertNotIn("devam", second_hit.text)
        self.assertEqual(original_rects,
                         [tuple(word.rect) for word in second_hit.words])
        self.assertEqual(visible_placements(), 1)
        self.assertEqual(pdf._source_ocr_lines[0][0].text, source_text)

        pdf.undo()
        self.assertTrue(pdf.ocr_ready(0))
        after_one_undo = pdf.line_at_point(0, second_point)
        self.assertIsNotNone(after_one_undo)
        self.assertIn("biter", after_one_undo.text)
        self.assertNotIn("sürer", after_one_undo.text)
        pdf.undo()
        after_two_undos = pdf.line_at_point(0, point)
        self.assertIsNotNone(after_two_undos)
        self.assertIn("devam", after_two_undos.text)
        self.assertFalse(pdf.dirty)
        pdf.close()

    @unittest.skipUnless(ocr_is_available(), "Yerel Türkçe OCR bulunamadı")
    def test_scan_line_can_be_edited_twice_without_reverting(self) -> None:
        path = self._path("scan-reedit.pdf")
        original_line = self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, [original_line])
        mesleki = next(word for word in original_line.words
                       if word.text == "mesleki")
        first_point = fitz.Point((mesleki.rect.x0 + mesleki.rect.x1) / 2,
                                 (mesleki.rect.y0 + mesleki.rect.y1) / 2)
        first_hit = pdf.line_at_point(0, first_point)
        self.assertIsNotNone(first_hit)
        pdf.replace_text_hit(
            0, first_hit, first_hit.text.replace("mesleki", "eğitim", 1))

        self.assertTrue(pdf.ocr_ready(0))
        self.assertIn("eğitim", first_hit.text)
        self.assertNotIn("mesleki", first_hit.text)
        self.assertIn("mesleki", pdf._source_ocr_lines[0][0].text,
                      "Değişmez OCR/glif bankası düzenlenmemeli")
        devam = next(word for word in first_hit.words
                     if word.text == "devam")
        second_point = fitz.Point((devam.rect.x0 + devam.rect.x1) / 2,
                                  (devam.rect.y0 + devam.rect.y1) / 2)
        second_hit = pdf.line_at_point(0, second_point)
        self.assertIsNotNone(second_hit)
        self.assertIs(second_hit, first_hit)
        pdf.replace_text_hit(
            0, second_hit, second_hit.text.replace("devam", "eder", 1))
        self.assertTrue(pdf.ocr_ready(0))
        self.assertIn("eğitim", second_hit.text)
        self.assertNotIn("devam", second_hit.text)

        final_lines = recognize_pdf_page(pdf.snapshot_bytes(), 0)
        final_line = next(line for line in final_lines
                          if "İşletmelerde" in line.text)
        self.assertNotIn("mesleki", final_line.text)
        self.assertGreaterEqual(final_line.text.count("eğitim"), 2,
                                final_line.text)
        self.assertNotIn("devam", final_line.text)
        pdf.close()

    def test_moved_scan_object_can_be_moved_again(self) -> None:
        path = self._path("scan-move.pdf")
        self._make_scan_pdf(path)
        pdf = PdfDocument()
        pdf.open(path)
        source = fitz.Rect(390, 50, 545, 170)
        target = fitz.Rect(390, 235, 545, 325)

        def dark_pixels(rect: fitz.Rect) -> int:
            pix = pdf.doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect,
                                        alpha=False, colorspace=fitz.csRGB)
            data = pix.samples
            return sum(1 for index in range(0, len(data), pix.n)
                       if data[index] + data[index + 1] + data[index + 2] < 510)

        before_target = dark_pixels(target)
        pdf.move_region(0, source, target)
        pdf.move_region(0, target, source)
        after_target = dark_pixels(target)
        self.assertGreater(after_target, before_target * .72)
        pdf.close()

    @unittest.skipUnless(ocr_is_available(), "Yerel Türkçe OCR bulunamadı")
    def test_turkish_ocr_maps_word_to_whole_line(self) -> None:
        path = self._path("scan-ocr.pdf")
        self._make_scan_pdf(path)
        with open(path, "rb") as stream:
            lines = recognize_pdf_page(stream.read(), 0)
        line = next(item for item in lines
                    if "İşletmelerde" in item.text and "mesleki" in item.text)
        pdf = PdfDocument()
        pdf.open(path)
        pdf.set_ocr_lines(0, lines)
        word = next(item for item in line.words if "mesleki" in item.text)
        point = fitz.Point((word.rect.x0 + word.rect.x1) / 2,
                           (word.rect.y0 + word.rect.y1) / 2)
        hit = pdf.line_at_point(0, point)
        self.assertIsNotNone(hit)
        self.assertIn("İşletmelerde mesleki eğitim", hit.text)
        pdf.close()


if __name__ == "__main__":
    unittest.main()
