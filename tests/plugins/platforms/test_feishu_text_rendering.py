"""Feishu's extracted pure rendering keeps text, style and newline semantics."""

import pytest

from plugins.platforms.feishu import adapter
from plugins.platforms.feishu import feishu_text_rendering as rendering


@pytest.mark.parametrize(
    ('element', 'expected'),
    [
        ({'text': '*literal*'}, '*literal*'),
        ({'text': 'word', 'style': {'bold': True, 'italic': 'true'}}, '***word***'),
        ({'text': '`x`', 'style': {'code': 1, 'bold': True}}, '`` `x` ``'),
        ({'text': '', 'style': {'bold': True}}, ''),
        ({'text': 'word', 'style': ['bold']}, 'word'),
    ],
)
def test_text_styles_and_literal_characters(element, expected):
    assert adapter._render_text_element(element) == expected
    assert rendering._render_text_element(element) == expected


def test_code_fences_preserve_content_and_sanitize_language():
    element = {'lang': ' py\nunsafe\r ', 'content': 'first\r\nsecond'}
    assert adapter._render_code_block_element(element) == '```py unsafe\nfirst\nsecond\n```'


def test_plain_fallback_preserves_links_and_feishu_formatting():
    value = '> ~~removed~~ <u>under</u>\r\n[link]( https://example.org )'
    assert adapter._strip_markdown_to_plain_text(value) == 'removed under\nlink (https://example.org)'


def test_normalization_keeps_style_helper_exports():
    assert adapter._to_boolean('true') is True
    assert adapter._to_boolean('false') is False
    assert adapter._escape_markdown_text('[label]') == r'\[label\]'
