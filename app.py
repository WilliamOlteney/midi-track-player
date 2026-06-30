"""MIDI Track Player — application entry point.

Run with:

    python app.py

Loads a Standard MIDI File and plays one or more selected tracks to an
external MIDI output device.
"""

import sys

from PySide6.QtWidgets import QApplication

from midiplay.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MIDI Track Player")
    app.setOrganizationName("MIDITrackPlayer")  # used by QSettings later

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
