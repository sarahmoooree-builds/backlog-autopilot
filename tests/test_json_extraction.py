"""Unit tests for the ``_extract_json_array`` helper in ``ingest.py``.

The helper pulls a JSON array out of a Devin v3 session payload via four
progressively-lenient strategies: ``structured_output`` → scan message lists
for parseable content → scan for embedded ``[...]`` substrings → last-resort
top-level string fields. Every strategy is exercised here.
"""

from __future__ import annotations

from ingest import _extract_json_array


class TestStructuredOutput:
    def test_extracts_from_structured_output(self, devin_structured_output_response):
        out = _extract_json_array(devin_structured_output_response)
        assert out == [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

    def test_empty_structured_output_falls_through(self):
        # Empty list is falsy — the helper should fall through to messages.
        session = {
            "structured_output": [],
            "messages": [
                {"content": '[{"id": 1}]'},
            ],
        }
        assert _extract_json_array(session) == [{"id": 1}]

    def test_structured_output_wrong_type_falls_through(self):
        session = {
            "structured_output": {"not": "a list"},
            "messages": [{"content": '[{"id": 2}]'}],
        }
        assert _extract_json_array(session) == [{"id": 2}]


class TestMessageScan:
    def test_content_field(self, devin_messages_response):
        out = _extract_json_array(devin_messages_response)
        assert out == [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

    def test_message_field_alias(self):
        session = {
            "messages": [{"role": "assistant", "message": '[{"id": 9}]'}],
        }
        assert _extract_json_array(session) == [{"id": 9}]

    def test_text_field_alias(self):
        session = {"messages": [{"text": '[{"id": 1}]'}]}
        assert _extract_json_array(session) == [{"id": 1}]

    def test_body_field_alias(self):
        session = {"messages": [{"body": '[{"id": 2}]'}]}
        assert _extract_json_array(session) == [{"id": 2}]

    def test_items_alias_list(self):
        # v3 sometimes exposes messages as "items".
        session = {"items": [{"content": '[{"id": 3}]'}]}
        assert _extract_json_array(session) == [{"id": 3}]

    def test_conversation_alias(self):
        session = {"conversation": [{"content": '[{"id": 4}]'}]}
        assert _extract_json_array(session) == [{"id": 4}]

    def test_history_alias(self):
        session = {"history": [{"content": '[{"id": 5}]'}]}
        assert _extract_json_array(session) == [{"id": 5}]

    def test_prefers_latest_message(self):
        # The helper iterates in reverse, so the latest parseable message wins.
        session = {
            "messages": [
                {"content": '[{"id": 1}]'},
                {"content": '[{"id": 2}]'},
            ]
        }
        assert _extract_json_array(session) == [{"id": 2}]

    def test_non_dict_messages_skipped(self):
        session = {
            "messages": [
                "raw string",
                None,
                {"content": '[{"id": 7}]'},
            ]
        }
        assert _extract_json_array(session) == [{"id": 7}]


class TestEmbeddedSubstringParse:
    def test_embedded_json_in_prose(self, devin_embedded_json_response):
        out = _extract_json_array(devin_embedded_json_response)
        assert out == [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

    def test_top_level_string_field_parses(self):
        session = {"output": '[{"id": 1}]'}
        assert _extract_json_array(session) == [{"id": 1}]

    def test_top_level_embedded_substring(self):
        session = {"result": "prefix [{\"id\": 2}] suffix"}
        assert _extract_json_array(session) == [{"id": 2}]

    def test_multiple_top_level_fields(self):
        # Helper iterates through several well-known string field names.
        for field in ("output", "result", "response", "output_text", "last_message"):
            assert _extract_json_array({field: '[{"id": 99}]'}) == [{"id": 99}]


class TestGracefulFailure:
    def test_garbage_returns_none(self, devin_garbage_response):
        assert _extract_json_array(devin_garbage_response) is None

    def test_completely_empty_returns_none(self):
        assert _extract_json_array({}) is None

    def test_plain_object_not_accepted(self):
        # The helper specifically wants a list; an object in the content is rejected.
        session = {"messages": [{"content": '{"id": 1}'}]}
        assert _extract_json_array(session) is None

    def test_malformed_json_returns_none(self):
        session = {"messages": [{"content": "[not valid json,"}]}
        assert _extract_json_array(session) is None

    def test_non_string_content_ignored(self):
        session = {"messages": [{"content": 123}]}
        assert _extract_json_array(session) is None
