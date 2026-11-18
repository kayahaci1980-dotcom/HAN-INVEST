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


class GeminiBtcDirectionGateTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [{"symbol": "ETHUSDT"}, {"symbol": "SOLUSDT"}]

    def test_bullish_btc_blocks_short_recommendations(self):
        raw = {"btc_direction": "BULLISH", "market_bias": "LONG", "decisions": [
            {"symbol": "ETHUSDT", "action": "LONG", "confidence": 95, "reason": "trend aligned"},
            {"symbol": "SOLUSDT", "action": "SHORT", "confidence": 99, "reason": "short setup"},
        ]}
        result = ai_advisor._validated_result(raw, self.candidates, "test-model")
        decisions = {item["symbol"]: item["action"] for item in result["recommendations"]}
        self.assertEqual(result["btc_direction"], "BULLISH")
        self.assertEqual(decisions, {"ETHUSDT": "LONG", "SOLUSDT": "WAIT"})

    def test_bearish_btc_blocks_long_recommendations(self):
        raw = {"btc_direction": "BEARISH", "market_bias": "SHORT", "decisions": [
            {"symbol": "ETHUSDT", "action": "LONG", "confidence": 95, "reason": "countertrend"},
            {"symbol": "SOLUSDT", "action": "SHORT", "confidence": 90, "reason": "trend aligned"},
        ]}
        result = ai_advisor._validated_result(raw, self.candidates, "test-model")
        decisions = {item["symbol"]: item["action"] for item in result["recommendations"]}
        self.assertEqual(decisions, {"ETHUSDT": "WAIT", "SOLUSDT": "SHORT"})

    def test_neutral_or_unknown_btc_blocks_all_directions(self):
        for direction in ("NEUTRAL", "UNKNOWN", None):
            raw = {"btc_direction": direction, "market_bias": "MIXED", "decisions": [
                {"symbol": "ETHUSDT", "action": "LONG", "confidence": 99, "reason": "test"},
                {"symbol": "SOLUSDT", "action": "SHORT", "confidence": 99, "reason": "test"},
            ]}
            result = ai_advisor._validated_result(raw, self.candidates, "test-model")
            self.assertTrue(all(item["action"] == "WAIT" for item in result["recommendations"]))

    def test_btc_context_requires_valid_bybit_btc_price(self):
        self.assertIsNone(ai_advisor._btc_market_context({"assets": []}))
        self.assertIsNone(ai_advisor._btc_market_context({"assets": [{"symbol": "BTCUSDT", "price": 0}]}))
        context = ai_advisor._btc_market_context({"assets": [
            {"symbol": "BTCUSDT", "price": 67000, "change_1h_pct": 0.8, "ma20": 66500}
        ]})
        self.assertEqual(context["symbol"], "BTCUSDT")
        self.assertEqual(context["price"], 67000)
