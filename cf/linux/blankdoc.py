"""Blank documents the editors are started on, so they open straight into an editable view.

WPS Office's start page ("新建") is an embedded web page that loads templates from docer.wps.cn;
behind a broken proxy or offline it spins for a minute before the offline "blank document" button
appears, and the `edit` work script has nothing to type into.  Starting `wps <file>` skips the
start page altogether.  The files live under ~/.cache/context_freeze and are (re)created on demand
by `ensure()`; the catalogue's WPS command references BLANK_DOCX and LinuxDesktop._spawn calls
ensure() before launching.  `python3 -m cf.linux.blankdoc` prints the paths (for shell use)."""
from __future__ import annotations

import os
import zipfile

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "context_freeze")
BLANK_DOCX = os.path.join(CACHE_DIR, "blank.docx")
BLANK_TOKEN = ".cache/context_freeze/blank."        # a cmd containing this gets ensure() before launch

_CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                  '<Default Extension="xml" ContentType="application/xml"/>'
                  '<Override PartName="/word/document.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                  '</Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" '
         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
         'Target="word/document.xml"/></Relationships>')
_DOCUMENT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p/><w:sectPr/></w:body></w:document>')


def write_docx(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", _DOCUMENT)
    os.replace(tmp, path)
    return path


def is_docx(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def ensure(docx: str = BLANK_DOCX) -> dict:
    """Create the blank files that do not exist yet (a document the user typed into and WPS
    auto-saved is left alone).  Returns {name: path}."""
    if not is_docx(docx):
        write_docx(docx)
    return {"docx": docx}


def reset(docx: str = BLANK_DOCX) -> dict:
    """Overwrite with fresh blanks (between experiment runs, so the `edit` drafts do not pile up)."""
    write_docx(docx)
    return {"docx": docx}


if __name__ == "__main__":
    import sys
    paths = reset() if "--reset" in sys.argv else ensure()
    for k, v in paths.items():
        print(f"{k} {v}")
