"""Tests for Cursor raw models and the CURSOR provider enum member."""

from hub.models.base import Provider
from hub.models.cursor import CursorBubble, CursorComposer


def test_cursor_provider_enum_value():
    assert Provider.CURSOR.value == "cursor"


def test_cursor_composer_defaults():
    c = CursorComposer(composer_id="abc")
    assert c.composer_id == "abc"
    assert c.created_at == 0
    assert c.total_lines_added == 0


def test_cursor_bubble_links_composer():
    c = CursorComposer(composer_id="abc", project="myproj")
    b = CursorBubble(composer_id="abc", bubble_id="b1", bubble_type=1, composer=c)
    assert b.composer is c
    assert b.composer.project == "myproj"
    assert b.raw == {}
