from listeners.views.app_home_builder import build_app_home_view
from listeners.views.feedback_builder import build_feedback_blocks


def test_build_feedback_blocks():
    blocks = build_feedback_blocks()

    assert len(blocks) > 0
    # The block should contain a feedback action
    block_dict = blocks[0].to_dict()
    action_ids = [e["action_id"] for e in block_dict["elements"]]
    assert "feedback" in action_ids


def test_build_app_home_view_default():
    view = build_app_home_view()

    assert view["type"] == "home"

    # Should have a header and a section
    block_types = [b["type"] for b in view["blocks"]]
    assert "header" in block_types
    assert "section" in block_types

    section_texts = [
        b["text"]["text"] for b in view["blocks"] if b["type"] == "section"
    ]
    assert any("/test-status" in text for text in section_texts)


def test_build_app_home_view_connect():
    view = build_app_home_view(install_url="https://example.com/slack/install")
    assert view["type"] == "home"


def test_build_app_home_view_connected():
    view = build_app_home_view(is_connected=True)
    assert view["type"] == "home"
