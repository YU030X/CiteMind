"""请求体字节上限回归：必须在 JSON 解析之前按实际收到的字节数生效。

额外字段、空白、``\\u`` 转义、以及缺失 Content-Length 的分块传输都不能绕过；超限时不
进入路由、不触发 tokenizer，也不回显正文。
"""

import asyncio

from asgi_driver import AsgiRequest, call_embed, embed_scope, json_body, running_app
from support import StubEmbedder, build_settings

# 让语义上限与传输上限都收窄到很小的值，便于构造“解析前就已超限”的请求。
TINY_LIMITS = {
    "embedding_max_batch_size": 1,
    "embedding_max_request_bytes": 32,
    "embedding_max_total_bytes": 32,
    "embedding_max_chars_per_text": 32,
    "embedding_max_tokens_per_text": 32,
    "embedding_max_total_tokens": 32,
}

SENTINEL_PADDING = "P" * 400


def test_extra_fields_cannot_bypass_the_request_byte_limit() -> None:
    stub = StubEmbedder()
    settings = build_settings(**TINY_LIMITS)

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            # texts 只有 2 字节，但带一个 400 字节的无关字段。
            body = json_body(["ab"], pad=400)
            assert len(body) > 32
            response = await call_embed(app, chunks=[body])
            assert response.status == 413
            assert response.body_json()["code"] == "EMBEDDING_PAYLOAD_TOO_LARGE"
            # 未进入路由：tokenizer 与编码都没有被触发，正文也没有被回显。
            assert stub.token_count_calls == []
            assert stub.embed_calls == []
            assert SENTINEL_PADDING not in response.text()

    asyncio.run(scenario())


def test_unicode_escapes_cannot_bypass_the_request_byte_limit() -> None:
    stub = StubEmbedder()
    settings = build_settings(**TINY_LIMITS)

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            # 解析后正文很短，但每个字符都被转义成 6 字节，原始字节数远超上限。
            body = b'{"kind":"document","texts":["\\u4e16\\u754c"],"pad":"' + b" " * 200 + b'"}'
            assert len(body) > 32
            response = await call_embed(app, chunks=[body])
            assert response.status == 413
            assert stub.token_count_calls == []
            assert "\\u4e16" not in response.text()

    asyncio.run(scenario())


def test_chunked_body_without_content_length_is_still_limited() -> None:
    stub = StubEmbedder()
    settings = build_settings(**TINY_LIMITS)

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            chunks = [
                b'{"kind":"document","texts":',
                b'["' + b"a" * 200 + b'"]}',
            ]
            request = AsgiRequest(app, embed_scope(), chunks)
            response = await request.run()
            assert response.status == 413
            assert stub.token_count_calls == []

    asyncio.run(scenario())


def test_invalid_json_over_the_limit_is_413_not_422() -> None:
    stub = StubEmbedder()
    settings = build_settings(**TINY_LIMITS)

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            response = await call_embed(app, chunks=[b'{"texts": [' + b"x" * 200])
            assert response.status == 413
            assert "x" * 32 not in response.text()

    asyncio.run(scenario())


def test_declared_content_length_over_the_limit_is_rejected_before_reading() -> None:
    stub = StubEmbedder()
    settings = build_settings(**TINY_LIMITS)

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            response = await call_embed(
                app,
                chunks=[json_body(["ab"])],
                headers={"content-length": "100000"},
            )
            assert response.status == 413
            assert stub.token_count_calls == []

    asyncio.run(scenario())


def test_default_request_limit_tightens_with_the_text_budget() -> None:
    """只调紧文本预算时，超大请求体也必须在解析前被拒（审查复现：上限 32 时 10031 字节得 200）。"""

    stub = StubEmbedder()
    settings = build_settings(
        embedding_max_batch_size=1,
        embedding_max_total_bytes=32,
        embedding_max_chars_per_text=32,
        embedding_max_tokens_per_text=32,
        embedding_max_total_tokens=32,
    )
    assert settings.embedding_max_request_bytes is None
    assert settings.request_byte_limit < 10031

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            body = json_body(["ab"], pad=10000)
            assert len(body) > 10000
            response = await call_embed(app, chunks=[body])
            assert response.status == 413
            assert stub.token_count_calls == []
            assert stub.embed_calls == []

    asyncio.run(scenario())


def test_request_within_the_byte_limit_still_succeeds() -> None:
    stub = StubEmbedder()
    settings = build_settings(
        embedding_max_batch_size=1,
        embedding_max_request_bytes=4096,
        embedding_max_total_bytes=1024,
        embedding_max_chars_per_text=128,
        embedding_max_tokens_per_text=512,
        embedding_max_total_tokens=512,
    )
    texts = ["在限制内的普通请求"]

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            response = await call_embed(app, chunks=[json_body(texts)])
            assert response.status == 200
            assert response.body_json()["tokenCounts"] == [len(texts[0]) + 2]

    asyncio.run(scenario())


def test_content_length_parse_failure_falls_back_to_byte_counting() -> None:
    stub = StubEmbedder()
    settings = build_settings(**TINY_LIMITS)

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            response = await call_embed(
                app,
                chunks=[json_body(["ab"], pad=400)],
                headers={"content-length": "not-a-number"},
            )
            # 不可信的 Content-Length 不能作为放行依据；仍按实际字节拒绝。
            assert response.status == 413
            assert stub.token_count_calls == []

    asyncio.run(scenario())
