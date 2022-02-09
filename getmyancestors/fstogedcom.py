#!/usr/bin/env python3
# coding: utf-8

# global imports
import re
import os
import sys
import time
import asyncio
import tempfile
from tkinter import (
    Tk,
    StringVar,
    IntVar,
    filedialog,
    messagebox,
    Menu,
    TclError,
    PhotoImage,
)
from tkinter.ttk import Frame, Label, Entry, Button, Checkbutton, Treeview, Notebook
from threading import Thread
from diskcache import Cache

# local imports
from getmyancestors.classes.classes import (
    Session,
    Tree,
    Indi,
    Fam,
    Gedcom,
    EntryWithMenu,
    FilesToMerge,
    Merge,
    SignIn,
    StartIndis,
    Options,
    Download,
    FStoGEDCOM,
)

from getmyancestors.translation import translations


def main():
    root = Tk()
    root.title("FamilySearch to GEDCOM")
    if sys.platform != "darwin":
        root.iconphoto(
            True,
            PhotoImage(file=os.path.join(os.path.dirname(__file__), "fstogedcom.png")),
        )
    fstogedcom = FStoGEDCOM(root)
    fstogedcom.mainloop()


if __name__ == "__main__":
    main()
