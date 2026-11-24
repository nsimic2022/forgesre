"""The visual guide stays in step with its script: every page has a picture and both pages list it."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("visual_guide", ROOT / "scripts" / "visual_guide.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["visual_guide"] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


def test_every_page_has_a_picture_and_both_languages():
    vg = _script()
    keys = [p.key for p in vg.PAGES]
    assert len(keys) == len(set(keys))
    en = (ROOT / "docs" / "visual-guide.md").read_text(encoding="utf-8")
    sr = (ROOT / "docs" / "vizuelni-vodic-sr.md").read_text(encoding="utf-8")
    for page in vg.PAGES:
        assert page.steps and all(s.en and s.sr for s in page.steps), page.key
        assert (ROOT / "docs" / "guide" / f"{page.key}.png").is_file(), page.key
        assert f"guide/{page.key}.png" in en and f"guide/{page.key}.png" in sr
        assert f"## {page.title_en}" in en and f"## {page.title_sr}" in sr


def test_markdown_numbers_follow_the_marks():
    vg = _script()
    page = vg.PAGES[0]
    text = vg._markdown([(page, [(1, page.steps[0]), (2, page.steps[2])])], "sr")
    assert f"1. {page.steps[0].sr}" in text and f"2. {page.steps[2].sr}" in text
    assert "](visual-guide.md)" in text
