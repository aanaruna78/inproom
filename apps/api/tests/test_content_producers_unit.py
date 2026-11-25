"""INPROOM-159 (story INPROOM-27): the built-in text@1 producer is deterministic and fails loudly."""

import json

import pytest

from app.content.producers import CONFLUENCE, JIRA, PARSE, TEXT, ProducerError, for_media_type


def test_confluence_storage_becomes_plain_text_without_macro_parameters():
    storage = (
        "<h1>Architecture</h1><p>Services:</p><ul><li>api</li><li>web &amp; jobs</li></ul>"
        '<ac:structured-macro ac:name="toc"><ac:parameter ac:name="maxLevel">2</ac:parameter>'
        "</ac:structured-macro><p>Ünïcode ✓</p>"
    )
    out = TEXT.build(storage.encode(), CONFLUENCE + "; charset=utf-8").decode()
    assert out == "Architecture\nServices:\napi\nweb & jobs\nÜnïcode ✓\n"
    assert TEXT.build(storage.encode(), CONFLUENCE).decode() == out  # deterministic


def test_jira_record_becomes_key_summary_and_description():
    record = {
        "id": "1",
        "key": "ABC-1",
        "fields": {
            "summary": "Login",
            "description": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "As a user"}]}],
            },
        },
    }
    out = TEXT.build(json.dumps(record).encode(), JIRA).decode()
    assert out == "ABC-1: Login\nAs a user\n"
    with pytest.raises(ProducerError):
        TEXT.build(b"{not json", JIRA)
    with pytest.raises(ProducerError):
        TEXT.build(b"\xff\xfe", CONFLUENCE)


def test_producers_are_chosen_by_input_media_type():
    assert for_media_type(CONFLUENCE + "; charset=utf-8") == [PARSE, TEXT]
    assert for_media_type("image/png") == []
