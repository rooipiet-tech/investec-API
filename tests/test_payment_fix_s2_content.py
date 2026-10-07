"""Slice 2 fix round R4: more inline-style hiding patterns are hidden text, never typed."""
from __future__ import annotations

import pytest

from tests.mail_helpers import make_msg
from tests.timing_helper import assert_fast



def typed(style, tag="div"):
    from invespend.payments import content

    v = content.text_view(make_msg(html=f'<{tag} style="{style}">pay 777</{tag}><div>visible</div>'))
    return v.typed_text


HIDDEN = [
    "opacity:0", "opacity: 0", "opacity:0.0", "OPACITY:0 !important", "color:red;opacity:0;",
    "font-size:0", "font-size:0px", "font-size: 0pt", "font-size:0em", "font-size:0rem", "font-size:0.0px",
    "font-size:0%", "font-size:0 !important", "max-height:0", "max-height:0px", "max-width:0", "max-width: 0px",
    "width:0", "height:0", "width:0px", "height:0px;overflow:hidden", "overflow:hidden;width:0;height:0",
    "text-indent:-999px", "text-indent:-9999px", "text-indent: -1000em", "text-indent:-99999px",
    "line-height:0", "line-height:0px", "font-size:12px;line-height:0",
    "display:none", "visibility:hidden", "mso-hide:all",
]
VISIBLE = [
    "opacity:0.5", "opacity:1", "opacity:0.9", "font-size:12px", "font-size:0.9em", "font-size:10pt",
    "font-size:100%", "max-height:100px", "max-height:0.5em", "max-width:600px", "width:100px", "height:20px",
    "width:100%", "text-indent:-5px", "text-indent:20px", "text-indent:-998px", "line-height:1.5",
    "line-height:20px", "border-width:0", "min-height:0", "min-width:0", "margin-height:0", "padding:0",
    "color:#fff", "color:#333;background:#fff", "font-weight:bold", "line-height:0.8", "border:0",
    "overflow:hidden", "letter-spacing:0", "word-spacing:0",
]


@pytest.mark.parametrize("style", HIDDEN)
def test_hidden_style_is_not_typed(style):
    assert typed(style) == "visible"


@pytest.mark.parametrize("style", HIDDEN)
def test_hidden_style_on_inline_tag_is_not_typed(style):
    assert typed(style, "span") == "visible"


@pytest.mark.parametrize("style", VISIBLE)
def test_legitimate_visible_style_stays_typed(style):
    assert "pay 777" in typed(style)


def test_hidden_style_nested_content_dropped():
    from invespend.payments import content

    v = content.text_view(make_msg(html='<div style="font-size:0"><p>pay 1</p><b>pay 2</b></div><p>ok</p>'))
    assert v.typed_text == "ok"


@pytest.mark.parametrize("data", [
    "'font-size:' + '0' * 200000", "'opacity:0 ' * 20000", "'text-indent:-' + '9' * 200000",
    "'width:' + ' ' * 200000 + '0'", "'font-size:0' * 20000", "'max-height:' + '0' * 99999 + 'x'",
])
def test_hidden_style_regex_is_linear(data):
    assert_fast(
        f"from invespend.payments import content\ndata = {data}",
        "content._HIDDEN_STYLE.search(data)",
    )
