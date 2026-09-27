"""回答结构严格校验单测：额外字段、类型、allowlist 与无引用回答的拒绝。"""

from __future__ import annotations

import json

import pytest
from rag_backend.generation.answer_schema import (
    AnswerSchemaError,
    parse_answer,
)

ALLOWED = frozenset({"E1", "E2"})


def _payload(**overrides: object) -> str:
    base: dict[str, object] = {
        "sentences": [{"text": "制度规定。", "citationIds": ["E1"]}],
        "insufficientEvidence": False,
        "followUp": None,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_valid_answer_maps_sentences_and_citations() -> None:
    parsed = parse_answer(_payload(), allowed_citation_ids=ALLOWED)

    assert parsed.insufficient_evidence is False
    assert parsed.citation_ids == ("E1",)
    assert parsed.answer_text == "制度规定。"


def test_duplicate_citation_ids_are_deduplicated_in_order() -> None:
    content = _payload(
        sentences=[
            {"text": "第一句。", "citationIds": ["E2", "E1", "E2"]},
            {"text": "第二句。", "citationIds": ["E1"]},
        ]
    )
    parsed = parse_answer(content, allowed_citation_ids=ALLOWED)

    assert parsed.sentences[0].citation_ids == ("E2", "E1")
    assert parsed.citation_ids == ("E2", "E1")


def test_refusal_without_sentences_is_accepted() -> None:
    parsed = parse_answer(
        _payload(sentences=[], insufficientEvidence=True),
        allowed_citation_ids=ALLOWED,
    )

    assert parsed.insufficient_evidence is True
    assert parsed.sentences == ()
    assert parsed.citation_ids == ()


def test_refusal_with_sentences_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(insufficientEvidence=True), allowed_citation_ids=ALLOWED
        )


def test_non_refusal_without_sentences_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(_payload(sentences=[]), allowed_citation_ids=ALLOWED)


def test_unknown_citation_id_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(sentences=[{"text": "句子。", "citationIds": ["E9"]}]),
            allowed_citation_ids=ALLOWED,
        )


def test_sentence_without_citation_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(sentences=[{"text": "句子。", "citationIds": []}]),
            allowed_citation_ids=ALLOWED,
        )


def test_extra_fields_are_rejected() -> None:
    content = json.dumps(
        {
            "sentences": [{"text": "句子。", "citationIds": ["E1"]}],
            "insufficientEvidence": False,
            "followUp": None,
            "url": "https://example.invalid",
        },
        ensure_ascii=False,
    )
    with pytest.raises(AnswerSchemaError):
        parse_answer(content, allowed_citation_ids=ALLOWED)

    nested_extra = json.dumps(
        {
            "sentences": [
                {"text": "句子。", "citationIds": ["E1"], "page": 3}
            ],
            "insufficientEvidence": False,
            "followUp": None,
        },
        ensure_ascii=False,
    )
    with pytest.raises(AnswerSchemaError):
        parse_answer(nested_extra, allowed_citation_ids=ALLOWED)


def test_wrong_types_are_rejected() -> None:
    for content in (
        "{not json",
        "[]",
        json.dumps({"sentences": "x", "insufficientEvidence": False}),
        json.dumps(
            {
                "sentences": [{"text": 1, "citationIds": ["E1"]}],
                "insufficientEvidence": False,
            }
        ),
        json.dumps(
            {
                "sentences": [{"text": "句。", "citationIds": [1]}],
                "insufficientEvidence": False,
            }
        ),
        json.dumps(
            {
                "sentences": [{"text": "句。", "citationIds": ["E1"]}],
                "insufficientEvidence": "false",
            }
        ),
    ):
        with pytest.raises(AnswerSchemaError):
            parse_answer(content, allowed_citation_ids=ALLOWED)


def test_blank_sentence_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(sentences=[{"text": "   ", "citationIds": ["E1"]}]),
            allowed_citation_ids=ALLOWED,
        )
