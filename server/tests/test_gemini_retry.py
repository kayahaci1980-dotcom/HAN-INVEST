import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import ai_advisor


class GeminiTransientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_503_and_succeeds_without_bypassing_validation(self):
        request = httpx.Request("POST", "https://generativelanguage.googleapis.com/test")
        overloaded = httpx.Response(503, headers={"retry-after": "0.5"}, request=request)
        success = httpx.Response(200, json={"candidates": []}, request=request)

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise httpx.HTTPStatusError("temporary overload", request=request, response=overloaded)
                return success

        client = FakeClient()
        with patch.object(ai_advisor.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await ai_advisor._post_gemini_with_retry(
                client, "https://generativelanguage.googleapis.com/test", "test-key", {}
            )
        self.assertIs(result, success)
        self.assertEqual(client.calls, 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_does_not_retry_non_transient_http_errors(self):
        request = httpx.Request("POST", "https://generativelanguage.googleapis.com/test")
        denied = httpx.Response(400, request=request)

        class FakeClient:
            calls = 0

            async def post(self, *args, **kwargs):
                self.calls += 1
                raise httpx.HTTPStatusError("bad request", request=request, response=denied)

        client = FakeClient()
        with patch.object(ai_advisor.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                await ai_advisor._post_gemini_with_retry(
                    client, "https://generativelanguage.googleapis.com/test", "test-key", {}
                )
        self.assertEqual(client.calls, 1)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
