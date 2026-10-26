"""cf.linux.blankdoc: a valid minimal docx, ensure() idempotent / reset() overwrites, the WPS
catalogue entry references it and the launcher creates it before spawning."""
import os
import zipfile

from cf.linux import apps as A
from cf.linux import blankdoc as B


def test_write_and_validate(tmp_path):
    p = B.write_docx(str(tmp_path / "a" / "blank.docx"))
    assert B.is_docx(p) and zipfile.ZipFile(p).testzip() is None
    with zipfile.ZipFile(p) as z:
        assert b"<w:body>" in z.read("word/document.xml")
        assert b"officeDocument" in z.read("_rels/.rels")
    assert not B.is_docx(str(tmp_path / "missing.docx"))
    (tmp_path / "junk.docx").write_text("not a zip")
    assert not B.is_docx(str(tmp_path / "junk.docx"))


def test_ensure_keeps_a_valid_file_and_reset_replaces_it(tmp_path):
    p = str(tmp_path / "blank.docx")
    assert B.ensure(p) == {"docx": p}
    m1 = os.stat(p).st_mtime_ns
    with open(p, "ab") as f:                      # WPS auto-saved into it: still a docx, keep it
        f.write(b"")
    assert B.ensure(p)["docx"] == p and os.stat(p).st_mtime_ns == m1
    with open(p, "wb") as f:                      # corrupted: recreated
        f.write(b"garbage")
    B.ensure(p)
    assert B.is_docx(p)
    os.utime(p, (1, 1))
    B.reset(p)
    assert os.stat(p).st_mtime_ns > 1_000_000_000 and B.is_docx(p)


def test_wps_command_opens_the_blank_document_and_spawn_creates_it(tmp_path, monkeypatch):
    wps = A.CATALOG["wps"]
    assert B.BLANK_TOKEN in wps.cmd and A._first_token(wps.cmd) == "wps"
    p = str(tmp_path / "blank.docx")
    monkeypatch.setattr(B, "BLANK_DOCX", p)
    # the launcher's hook: ensure() runs for commands that reference the blank file, not for others
    from cf.linux import device as D
    calls = []
    monkeypatch.setattr(D.blankdoc, "ensure", lambda docx=B.BLANK_DOCX: calls.append(docx) or {"docx": docx})
    for cmd, expect in (("wps ~/.cache/context_freeze/blank.docx", 1), ("google-chrome-stable", 0)):
        calls.clear()
        if D.blankdoc.BLANK_TOKEN in cmd:
            D.blankdoc.ensure()
        assert len(calls) == expect
