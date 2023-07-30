import os
import sys
import hashlib
import argparse
import traceback
import pyperclip as pc
from datetime import datetime, timedelta

from video_player import VideoPlayer

try:
    import qdarkstyle
    HAS_DARK_THEME = True
except ImportError:
    HAS_DARK_THEME = False
    pass


if os.name == "nt":
    from ctypes import windll, wintypes, byref, FormatError, WinError

# TODO: split this video player from this file, just too messy and pretty useless
# for pycharm, install pyqt5-stubs, so you don't get 10000 errors for no reason
from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtCore import *


# TEMP_FOLDER = ("TEMP" + os.sep + str(datetime.now()) + os.sep).replace(":", "-").replace(".", "-")
ROOT_FOLDER = f"{os.path.dirname(os.path.realpath(__file__))}{os.sep}"
TEMP_FOLDER = f"{ROOT_FOLDER}TEMP{os.sep}"


def parse_args() -> argparse.Namespace:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("-i", "--input", default="timestamps.txt")
    arg_parser.add_argument("-v", "--verbose", action="store_true")
    arg_parser.add_argument("-nd", "--no-dark-theme", action="store_true")
    arg_parser.add_argument("-c", "--cpus", nargs=2, help="cpu affinity range", default=[0, 11])
    return arg_parser.parse_args()


def PrintException(*args):
    print("", *args, traceback.format_exc())


def RemoveWidgets(layout: QLayout) -> None:
    try:
        for i in reversed(range(layout.count())):
            try:
                layout.itemAt(i).widget().setParent(None)
            except AttributeError:
                continue
    except:
        PrintException("Error Removing Widgets")
        
        
def get_hash(string: str):
    return hashlib.md5(string.encode('utf-8')).hexdigest()


def get_date_modified(file: str):
    return os.path.getmtime(file) if os.name == "nt" else os.stat(file).st_mtime
    
    
def get_date_created(file: str) -> float:
    return os.path.getctime(file) if os.name == "nt" else os.stat(file).st_atime


def set_file_times(file: str, created: float, mod: float, access: float):
    if os.name == "nt":
        os.utime(file, (access, mod))
        win32_setctime(file, created)
    else:
        oldest_date = min(created, mod, access)
        os.utime(file, (access, oldest_date))


# https://github.com/Delgan/win32-setctime/blob/master/win32_setctime.py
def win32_setctime(file: str, timestamp: float):
    if not os.name == "nt":
        return
    
    """Set the "ctime" (creation time) attribute of a file given an unix timestamp (Windows only)."""
    file = os.path.normpath(os.path.abspath(str(file)))
    timestamp = int((timestamp * 10000000) + 116444736000000000)
    
    if not 0 < timestamp < (1 << 64):
        raise ValueError("The system value of the timestamp exceeds u64 size: %d" % timestamp)
    
    atime = wintypes.FILETIME(0xFFFFFFFF, 0xFFFFFFFF)
    mtime = wintypes.FILETIME(0xFFFFFFFF, 0xFFFFFFFF)
    ctime = wintypes.FILETIME(timestamp & 0xFFFFFFFF, timestamp >> 32)
    
    handle = wintypes.HANDLE(windll.kernel32.CreateFileW(file, 256, 0, None, 3, 128, None))
    if handle.value == wintypes.HANDLE(-1).value:
        raise WinError()
    
    if not wintypes.BOOL(windll.kernel32.SetFileTime(handle, byref(ctime), byref(atime), byref(mtime))):
        raise WinError()
    
    if not wintypes.BOOL(windll.kernel32.CloseHandle(handle)):
        raise WinError()


# ==================================================================================================
# QT UI
# ==================================================================================================
        

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.layout = QHBoxLayout()
        
        # meh
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        self.setLayout(self.layout)
        self.videoPlayer = VideoPlayer(self)
        self.videoPath = ""
        self.video = None
        
        self.layout.addWidget(self.videoPlayer)
        
        self.show()
        
    def dropEvent(self, event: QDropEvent):
        text = event.mimeData().text()
        header = ""
        if text.startswith("file:///"):
            header = "file:///"
            
        self.set_video(text[len(header):])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        text = event.mimeData().text()
        if text.startswith("file:///"):
            event.accept()
        elif text.startswith("https://") or text.startswith("ftp://"):
            event.accept()
        
    def set_video(self, path: str):
        # self.video.set_video_path(url)
        self.videoPath = path
        self.videoPlayer.load_video(path)
        self.setWindowTitle(path)
        #self.videoList.on_video_loaded(path)
        # self.video.show()
        # self.layout.addWidget(self.videoPlayer)
        
    def get_current_time(self):
        #return self.videoPlayer.get_seconds_from_frames(self.videoPlayer.get_current_frame())
        return self.videoPlayer.get_playback_time()
    
    def get_current_timedelta(self):
        time = self.get_current_time()
        time2 = timedelta(seconds=time)
        return time2
    
    def copy_current_time(self):
        time = str(self.get_current_timedelta())

        if "." not in time:
            time += ".000"
        else:
            time = time[:-3]
        
        pc.copy(time)
        print("Copied Time: " + time)
        
    def copy_filename(self):
        filename = os.path.basename(self.videoPath)
        pc.copy(filename)
        print("Copied Filename: " + filename.__str__())
        
    def copy_template(self):
        filename = os.path.basename(self.videoPath)
        pc.copy(filename)
        print("Copied Filename: " + filename.__str__())


# Back up the reference to the exceptionhook
sys._excepthook = sys.excepthook


def QtExceptionHook(exctype, value, traceback):
    # Print the error and traceback
    print(exctype, value, traceback)
    # Call the normal Exception hook after
    sys._excepthook(exctype, value, traceback)
    sys.exit(1)


# Set the exception hook to our wrapping function
sys.excepthook = QtExceptionHook


if __name__ == "__main__":
    APP = QApplication(sys.argv)
    APP.setDesktopSettingsAware(True)

    ARGS = parse_args()

    if HAS_DARK_THEME and not ARGS.no_dark_theme:
        APP.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())
    
    main_window = MainWindow()
    
    try:
        sys.exit(APP.exec_())
    except Exception as F:
        print(F)

