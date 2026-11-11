"""Chinese figure labels: which font is chosen, and that nothing is drawn as an empty box."""
import logging
import os
import sys
import warnings

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from cf.linux import plots as P  # noqa: E402

matplotlib = pytest.importorskip("matplotlib")
from matplotlib import font_manager  # noqa: E402

FONTS = {f.name: f.fname for f in font_manager.fontManager.ttflist}
DROID = "Droid Sans Fallback"            # Chinese glyphs only: no letters, no digits
MIXED = "① 呼叫 / 切换响应时间 (柱 = P50, 须 = P95): 7-Zip · 压缩 0.1 × m_fg, 100 %"


@pytest.fixture(autouse=True)
def _fresh_choice(monkeypatch):
    monkeypatch.delenv("CF_PLOT_FONT", raising=False)
    monkeypatch.setattr(P, "_font_choice", None)
    with matplotlib.rc_context():
        yield


def _fake_fonts(monkeypatch, coverage):
    """coverage: {family: "cjk" | "cjk+latin" | "latin"}; the file name is the family."""
    def covers(fname, text):
        kind = coverage[fname]
        return ("cjk" in kind) if text == P._CJK_PROBE else ("latin" in kind)
    monkeypatch.setattr(P, "_covers", covers)
    return [(name, name) for name in coverage]


def test_a_font_with_letters_wins_over_one_with_chinese_glyphs_only(monkeypatch):
    fonts = _fake_fonts(monkeypatch, {DROID: "cjk", "DejaVu Sans": "latin", "WenQuanYi Micro Hei": "cjk+latin"})
    assert P._pick_cjk_font(fonts) == ("WenQuanYi Micro Hei", True)
    # preference order among complete fonts
    fonts = _fake_fonts(monkeypatch, {"WenQuanYi Zen Hei": "cjk+latin", "Noto Sans CJK SC": "cjk+latin"})
    assert P._pick_cjk_font(fonts) == ("Noto Sans CJK SC", True)


def test_chinese_only_font_is_still_used_and_unknown_fonts_are_found(monkeypatch):
    fonts = _fake_fonts(monkeypatch, {DROID: "cjk", "DejaVu Sans": "latin"})
    assert P._pick_cjk_font(fonts) == (DROID, False)
    fonts = _fake_fonts(monkeypatch, {"DejaVu Sans": "latin", "Some Vendor Hei": "cjk+latin", "Some Mono": "cjk+latin"})
    assert P._pick_cjk_font(fonts) == ("Some Vendor Hei", True)          # not in the list, found by its glyphs
    fonts = _fake_fonts(monkeypatch, {"DejaVu Sans": "latin"})
    assert P._pick_cjk_font(fonts) == ("", False)


def test_env_names_the_font(monkeypatch):
    fonts = _fake_fonts(monkeypatch, {DROID: "cjk", "WenQuanYi Micro Hei": "cjk+latin"})
    monkeypatch.setenv("CF_PLOT_FONT", DROID)
    assert P._pick_cjk_font(fonts) == (DROID, False)
    monkeypatch.setenv("CF_PLOT_FONT", "Not Installed")
    assert P._pick_cjk_font(fonts) == ("WenQuanYi Micro Hei", True)


def test_family_list_puts_dejavu_first_for_a_chinese_only_font(monkeypatch):
    from matplotlib import rcParams
    monkeypatch.setattr(P, "_pick_cjk_font", lambda fonts=None: (DROID, False))
    assert P._use_cjk_font() is True
    assert list(rcParams["font.family"]) == ["DejaVu Sans", DROID] and rcParams["axes.unicode_minus"] is False
    monkeypatch.setattr(P, "_font_choice", None)
    monkeypatch.setattr(P, "_pick_cjk_font", lambda fonts=None: ("WenQuanYi Micro Hei", True))
    assert P._use_cjk_font() is True and list(rcParams["font.family"]) == ["WenQuanYi Micro Hei", "DejaVu Sans"]
    # no per-glyph fallback (matplotlib < 3.6): a Chinese-only font cannot draw the digits -> English labels
    monkeypatch.setattr(P, "_font_choice", None)
    monkeypatch.setattr(P, "_pick_cjk_font", lambda fonts=None: (DROID, False))
    monkeypatch.delattr(font_manager.FontManager, "_find_fonts_by_props", raising=False)
    assert P._use_cjk_font() is False
    monkeypatch.setattr(P, "_font_choice", None)
    monkeypatch.setattr(P, "_pick_cjk_font", lambda fonts=None: ("", False))
    assert P._use_cjk_font() is False


@pytest.mark.skipif("WenQuanYi Micro Hei" not in FONTS, reason="needs fonts-wqy-microhei")
def test_a_font_installed_after_matplotlib_listed_the_fonts_is_found(monkeypatch):
    """The list is cached in ~/.cache/matplotlib and not refreshed when a font package is installed."""
    fm = font_manager.fontManager
    stale = [f for f in fm.ttflist if "WenQuanYi" not in f.name]
    assert len(stale) < len(fm.ttflist)
    monkeypatch.setattr(fm, "ttflist", stale)
    assert P._add_new_fonts(font_manager) >= 1
    assert "WenQuanYi Micro Hei" in {f.name for f in fm.ttflist}
    assert P._add_new_fonts(font_manager) == 0                           # nothing new the second time
    monkeypatch.setattr(fm, "ttflist", [f for f in fm.ttflist if "WenQuanYi" not in f.name])
    name, latin = P._pick_cjk_font()
    assert latin is True and name != DROID


def _draw(text, caplog):
    """Render a figure with `text` everywhere (title, ticks, legend, a log axis) -> the problems reported."""
    plt = P._plt()
    with warnings.catch_warnings(record=True) as w, caplog.at_level(logging.WARNING, logger="matplotlib"):
        warnings.simplefilter("always")
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.bar([0, 1], [3, 300], label=text)
        ax.set_yscale("log")
        ax.set_xticks([0, 1])
        ax.set_xticklabels([text[:12], "QQ"])
        ax.set_title(text)
        ax.set_ylabel("ms")
        ax.legend()
        fig.canvas.draw()
        plt.close(fig)
    return [str(x.message) for x in w if "Glyph" in str(x.message)] + [r.getMessage() for r in caplog.records
                                                                      if "glyph" in r.getMessage().lower()]


@pytest.mark.skipif(DROID not in FONTS or not hasattr(font_manager.FontManager, "_find_fonts_by_props"),
                    reason="needs the Droid Sans Fallback font and matplotlib >= 3.6")
def test_no_empty_boxes_with_a_chinese_only_font(monkeypatch, caplog):
    """The font of a minimal Ubuntu: used alone, every digit and letter of the figure was a box."""
    assert P._covers(FONTS[DROID], P._CJK_PROBE) and not P._covers(FONTS[DROID], P._LATIN_PROBE)
    monkeypatch.setenv("CF_PLOT_FONT", DROID)
    assert P._use_cjk_font() is True
    assert _draw(MIXED, caplog) == []
    # what the old code did: the font in front of the generic sans-serif list
    with matplotlib.rc_context():
        matplotlib.rcParams["font.family"] = ["sans-serif"]
        matplotlib.rcParams["font.sans-serif"] = [DROID, "DejaVu Sans"]
        caplog.clear()
        assert _draw(MIXED, caplog)


def test_no_empty_boxes_with_whatever_this_machine_has(caplog):
    zh = P._use_cjk_font()
    assert _draw(MIXED if zh else "1. call / switch response (bar P50, whisker P95)", caplog) == []
