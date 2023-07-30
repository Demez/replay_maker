import os
import sys
import pythoncom

if os.name == "nt":
    import win32api
    import win32file
    # from win32com.shell import shell
    from win32com import storagecon


def add_tag(file_path, key, value):
    pass


def get_file_info(file_path, key, value):
    pass

