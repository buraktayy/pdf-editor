"""Taranmış PDF sayfaları için yerel Tesseract satır haritası üretimi.

Bu modül ağ kullanmaz. Sayfa, bağımsız bir PDF kopyasından yüksek çözünürlükte
işlenir ve Tesseract TSV çıktısı PDF koordinatlarına dönüştürülür.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import statistics
import subprocess
import tempfile
from dataclasses import dataclass

import fitz


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_TESSDATA = os.path.join(BASE_DIR, "assets", "tessdata")
TESSERACT_CANDIDATES = (
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Tesseract-OCR", "tesseract.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Tesseract-OCR", "tesseract.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 "Programs", "Tesseract-OCR", "tesseract.exe"),
)


@dataclass(frozen=True)
class OcrWord:
    rect: fitz.Rect
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrGlyph:
    rect: fitz.Rect
    text: str


@dataclass(frozen=True)
class OcrLine:
    rect: fitz.Rect
    block_rect: fitz.Rect
    text: str
    words: tuple[OcrWord, ...]
    confidence: float
    glyphs: tuple[OcrGlyph, ...] = ()


def find_tesseract() -> str | None:
    for path in TESSERACT_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def ocr_is_available() -> bool:
    return (find_tesseract() is not None
            and os.path.isfile(os.path.join(PROJECT_TESSDATA,
                                            "tur.traineddata")))


def _subprocess_window_options() -> dict:
    """Tesseract'i Windows'ta gorunur bir konsol olusturmadan baslatir."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _union_rect(rects: list[fitz.Rect]) -> fitz.Rect:
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result.include_rect(rect)
    return result


_PERCENT_CONTEXT = {
    "tutarının", "tutarina", "tutarına", "oranının", "oraninin",
    "oranında", "oraninda", "yüzde", "yuzde",
}


def _normalize_percentage_words(words: list[OcrWord]) -> list[OcrWord]:
    """Tesseract'in yüzde işaretini iki rakam/harf sanmasını düzeltir.

    Taranmış Türkçe belgelerde ``%`` glifi sıkça ``90``, ``96``, ``Y6`` ya
    da tek ``6`` olarak bölünür. Düzeltme yalnızca "tutarının/oranının" gibi
    açık bir yüzde bağlamından sonra ve apostrofla ek alan kısa oranlarda
    uygulanır; normal belge numaraları ile tarihlere dokunulmaz.
    """
    result: list[OcrWord] = []
    previous = ""
    for word in words:
        text = word.text
        normalized = text
        context = previous.casefold().strip(".,;:()[]{}")
        if context in _PERCENT_CONTEXT and not text.startswith("%"):
            match = re.fullmatch(
                r"(?P<prefix>Y6|V6|90|96|[69])?"
                r"(?P<number>\d{1,2})"
                r"(?P<suffix>['’][^\s]*)",
                text,
                flags=re.IGNORECASE,
            )
            if match is not None:
                prefix = match.group("prefix") or ""
                number = match.group("number")
                # Örn. 2023'te gibi yıllar kalıbın en fazla iki haneli oran
                # kısmına zaten sığmaz. Önek yoksa kaybolmuş bir % kabul edilir.
                if prefix or 1 <= len(number) <= 2:
                    normalized = "%" + number + match.group("suffix")
        result.append(
            word if normalized == text else
            OcrWord(rect=fitz.Rect(word.rect), text=normalized,
                    confidence=word.confidence)
        )
        previous = normalized
    return result


def _glyph_ink_geometry(pixmap: fitz.Pixmap, rect: fitz.Rect,
                        scale_x: float, scale_y: float
                        ) -> tuple[float, float, float, int] | None:
    """Tek OCR glifinde (corr, y/x egimi, en/boy, bilesen) olcer."""
    x0 = max(0, math.floor(rect.x0 * scale_x) - 1)
    y0 = max(0, math.floor(rect.y0 * scale_y) - 1)
    x1 = min(pixmap.width, math.ceil(rect.x1 * scale_x) + 1)
    y1 = min(pixmap.height, math.ceil(rect.y1 * scale_y) + 1)
    if x1 - x0 < 2 or y1 - y0 < 3:
        return None
    data = pixmap.samples
    channels = pixmap.n

    def luma(x: int, y: int) -> float:
        offset = (y * pixmap.width + x) * channels
        return (data[offset] * .299 + data[offset + 1] * .587
                + data[offset + 2] * .114)

    border = []
    for x in range(x0, x1):
        border.append(luma(x, y0)); border.append(luma(x, y1 - 1))
    for y in range(y0 + 1, y1 - 1):
        border.append(luma(x0, y)); border.append(luma(x1 - 1, y))
    background = statistics.median(border) if border else 255.0
    threshold = max(25.0, background - 24.0)
    ink: set[tuple[int, int]] = set()
    for y in range(y0, y1):
        for x in range(x0, x1):
            if luma(x, y) <= threshold:
                ink.add((x, y))
    if len(ink) < 8:
        return None

    min_x = min(x for x, _y in ink); max_x = max(x for x, _y in ink)
    min_y = min(y for _x, y in ink); max_y = max(y for _x, y in ink)
    width = max_x - min_x + 1
    height = max_y - min_y + 1
    aspect = width / max(height, 1)
    mean_x = sum(x for x, _y in ink) / len(ink)
    mean_y = sum(y for _x, y in ink) / len(ink)
    var_x = sum((x - mean_x) ** 2 for x, _y in ink)
    var_y = sum((y - mean_y) ** 2 for _x, y in ink)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in ink)
    correlation = covariance / math.sqrt(var_x * var_y) \
        if var_x > .001 and var_y > .001 else 0.0
    slope = covariance / var_x if var_x > .001 else 0.0

    remaining = set(ink)
    component_areas: list[int] = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        area = 1
        while stack:
            cx, cy = stack.pop()
            for nx in range(cx - 1, cx + 2):
                for ny in range(cy - 1, cy + 2):
                    if (nx, ny) in remaining:
                        remaining.remove((nx, ny))
                        stack.append((nx, ny))
                        area += 1
        component_areas.append(area)
    minimum_component = max(3, round(len(ink) * .035))
    components = sum(1 for area in component_areas
                     if area >= minimum_component)
    return correlation, slope, aspect, components


def _normalize_visual_symbol_glyphs(
        line: OcrLine, glyphs: list[OcrGlyph], pixmap: fitz.Pixmap,
        scale_x: float, scale_y: float) -> OcrLine:
    """Box OCR'in `/`->`1` ve `%`->`9` hatasini yalniz sekil kanitiyla duzeltir."""
    corrected: list[OcrGlyph] = []
    for glyph in glyphs:
        character = glyph.text
        if character in {"1", "9"}:
            geometry = _glyph_ink_geometry(
                pixmap, glyph.rect, scale_x, scale_y)
            if geometry is not None:
                correlation, slope, aspect, components = geometry
                if (character == "1" and correlation < -.55
                        and slope < -1.2 and .25 <= aspect <= .58):
                    character = "/"
                elif (character == "9" and components >= 3
                      and aspect >= .85):
                    character = "%"
        corrected.append(OcrGlyph(fitz.Rect(glyph.rect), character))

    words: list[OcrWord] = []
    for word in line.words:
        inside = [glyph for glyph in corrected
                  if (word.rect.x0 - .8
                      <= (glyph.rect.x0 + glyph.rect.x1) / 2
                      <= word.rect.x1 + .8)
                  and (glyph.rect & word.rect).height
                  >= min(glyph.rect.height, word.rect.height) * .30]
        inside.sort(key=lambda glyph: (glyph.rect.x0, glyph.rect.y0))
        value = word.text
        if (len(inside) == len(value)
                and all(len(glyph.text) == 1 for glyph in inside)):
            value = "".join(glyph.text for glyph in inside)
        words.append(OcrWord(fitz.Rect(word.rect), value, word.confidence))
    return OcrLine(
        rect=fitz.Rect(line.rect), block_rect=fitz.Rect(line.block_rect),
        text=" ".join(word.text for word in words), words=tuple(words),
        confidence=line.confidence, glyphs=tuple(corrected))


def recognize_pdf_page(pdf_bytes: bytes, page_no: int,
                       render_scale: float = 4.0) -> list[OcrLine]:
    """Bir sayfayı Türkçe OCR ile okuyup satırları PDF koordinatında döndürür."""
    executable = find_tesseract()
    language_file = os.path.join(PROJECT_TESSDATA, "tur.traineddata")
    if executable is None:
        raise RuntimeError("Tesseract OCR bulunamadı.")
    if not os.path.isfile(language_file):
        raise RuntimeError("Türkçe OCR dil modeli bulunamadı.")

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if not 0 <= page_no < document.page_count:
            raise IndexError("OCR sayfa numarası geçersiz.")
        page = document[page_no]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale),
                                 alpha=False, colorspace=fitz.csRGB)
        scale_x = pixmap.width / page.rect.width
        scale_y = pixmap.height / page.rect.height
        image_bytes = pixmap.tobytes("png")
    finally:
        document.close()

    with tempfile.TemporaryDirectory(prefix="pdf-editor-ocr-") as temp_dir:
        image_path = os.path.join(temp_dir, "page.png")
        output_base = os.path.join(temp_dir, "result")
        with open(image_path, "wb") as stream:
            stream.write(image_bytes)
        command = [
            executable, image_path, output_base,
            "-l", "tur",
            "--tessdata-dir", PROJECT_TESSDATA,
            "--oem", "1",
            "--psm", "3",
            "-c", "preserve_interword_spaces=1",
            "-c", "tessedit_create_tsv=1",
            "-c", "tessedit_create_boxfile=1",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=90,
            check=False,
            **_subprocess_window_options(),
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "OCR işlemi başarısız oldu.")
        tsv_path = output_base + ".tsv"
        box_path = output_base + ".box"
        if not os.path.isfile(tsv_path):
            raise RuntimeError("OCR satır çıktısı üretilemedi.")
        with open(tsv_path, "r", encoding="utf-8", errors="replace") as stream:
            tsv = stream.read()
        if os.path.isfile(box_path):
            with open(box_path, "r", encoding="utf-8",
                      errors="replace") as stream:
                box_text = stream.read()
        else:
            box_text = ""

    page_glyphs: list[OcrGlyph] = []
    for raw_line in box_text.splitlines():
        parts = raw_line.rsplit(" ", 5)
        if len(parts) != 6:
            continue
        character = parts[0]
        if not character.strip() or character == "~":
            continue
        try:
            left, bottom, right, top = (float(value) for value in parts[1:5])
        except ValueError:
            continue
        rect = fitz.Rect(left / scale_x,
                         (pixmap.height - top) / scale_y,
                         right / scale_x,
                         (pixmap.height - bottom) / scale_y)
        if not rect.is_empty and rect.width > .05 and rect.height > .05:
            page_glyphs.append(OcrGlyph(rect=rect, text=character))

    groups: dict[tuple[int, int, int], list[OcrWord]] = {}
    block_words: dict[int, list[OcrWord]] = {}
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    for row in reader:
        try:
            if int(row.get("level", 0)) != 5:
                continue
            text = (row.get("text") or "").strip()
            confidence = float(row.get("conf", -1))
            if not text or confidence < 12:
                continue
            left = float(row["left"])
            top = float(row["top"])
            width = float(row["width"])
            height = float(row["height"])
            rect = fitz.Rect(left / scale_x, top / scale_y,
                             (left + width) / scale_x,
                             (top + height) / scale_y)
            word = OcrWord(rect=rect, text=text, confidence=confidence)
            block = int(row["block_num"])
            key = (block, int(row["par_num"]), int(row["line_num"]))
            groups.setdefault(key, []).append(word)
            block_words.setdefault(block, []).append(word)
        except (KeyError, TypeError, ValueError):
            continue

    block_rects = {
        block: _union_rect([word.rect for word in words])
        for block, words in block_words.items() if words
    }
    lines: list[OcrLine] = []
    for (block, _paragraph, _line_no), words in groups.items():
        words.sort(key=lambda item: (item.rect.x0, item.rect.y0))
        line_rect = _union_rect([word.rect for word in words])
        # Tesseract'in farklı sütunlardan tek satıra kattığı uzak parçaları ayır.
        chunks: list[list[OcrWord]] = [[]]
        for word in words:
            previous = chunks[-1][-1] if chunks[-1] else None
            gap = word.rect.x0 - previous.rect.x1 if previous else 0.0
            split_gap = max(32.0, line_rect.height * 5.0)
            if previous and gap > split_gap:
                chunks.append([])
            chunks[-1].append(word)
        for chunk in chunks:
            if not chunk:
                continue
            chunk = _normalize_percentage_words(chunk)
            rect = _union_rect([word.rect for word in chunk])
            text = " ".join(word.text for word in chunk)
            confidence = sum(word.confidence for word in chunk) / len(chunk)
            lines.append(OcrLine(rect=rect,
                                 block_rect=block_rects.get(block, rect),
                                 text=text,
                                 words=tuple(chunk),
                                 confidence=confidence))

    glyph_groups: list[list[OcrGlyph]] = [[] for _line in lines]
    for glyph in page_glyphs:
        center_x = (glyph.rect.x0 + glyph.rect.x1) / 2
        center_y = (glyph.rect.y0 + glyph.rect.y1) / 2
        best_index = -1
        best_score = float("inf")
        for index, line in enumerate(lines):
            y_padding = max(2.0, line.rect.height * .55)
            if not (line.rect.y0 - y_padding <= center_y
                    <= line.rect.y1 + y_padding):
                continue
            x_padding = max(3.0, line.rect.height * .45)
            if not (line.rect.x0 - x_padding <= center_x
                    <= line.rect.x1 + x_padding):
                continue
            vertical = abs(center_y - (line.rect.y0 + line.rect.y1) / 2)
            horizontal = max(line.rect.x0 - center_x,
                             center_x - line.rect.x1, 0.0)
            score = vertical * 4.0 + horizontal
            if score < best_score:
                best_index, best_score = index, score
        if best_index >= 0:
            glyph_groups[best_index].append(glyph)

    normalized_lines: list[OcrLine] = []
    for index, line in enumerate(lines):
        glyphs = sorted(glyph_groups[index],
                        key=lambda item: (item.rect.x0, item.rect.y0))
        with_glyphs = OcrLine(
            rect=line.rect, block_rect=line.block_rect, text=line.text,
            words=line.words, confidence=line.confidence,
            glyphs=tuple(glyphs))
        normalized_lines.append(_normalize_visual_symbol_glyphs(
            with_glyphs, glyphs, pixmap, scale_x, scale_y))
    lines = normalized_lines
    lines.sort(key=lambda line: (round(line.rect.y0, 1), line.rect.x0))
    return lines
