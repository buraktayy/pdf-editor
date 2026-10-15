import ctypes
import logging
import os
import sys
import traceback

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from main_window import BASE_DIR, ICON_PATH, MainWindow

LOG_PATH = os.path.join(BASE_DIR, "debug.log")

# pythonw ile konsol olmadığından hatalar görünmez; her şey debug.log'a yazılır
logging.basicConfig(filename=LOG_PATH, filemode="w", level=logging.DEBUG,
                    format="%(asctime)s %(levelname)s %(message)s",
                    encoding="utf-8")
log = logging.getLogger("pdfeditor")


def _excepthook(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log.error("YAKALANMAMIS HATA:\n%s", text)
    try:
        QMessageBox.critical(None, "Beklenmeyen Hata",
                             "Bir hata oluştu (debug.log'a kaydedildi):\n\n"
                             + str(exc))
    except Exception:
        pass


def main() -> int:
    sys.excepthook = _excepthook
    # Windows görev çubuğunda Python simgesi yerine uygulamanın kendi simgesini göster
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "BurakSoft.PDFDuzenleyici.1")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("PDF Düzenleyici")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    log.info("uygulama basladi")
    window = MainWindow()
    window.show()
    # komut satırından / kısayoldan dosya argümanıyla açılabilsin
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        window.load_path(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
