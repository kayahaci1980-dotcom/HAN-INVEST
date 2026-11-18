import unittest
from unittest.mock import patch

from app import ai_advisor


class GeminiAdvisorTests(unittest.TestCase):
    def test_candidate_payload_sorts_and_limits_to_500(self):
        assets = [
            {"symbol": f"COIN{i}USDT", "quote_volume_24h": i * 1000,
             "price": 1.0, "trade_ready": True, "long_score": 85,
             "short_score": 0, "private_field": "must_not_be_sent"}
            for i in range(600)
        ]
        result = ai_advisor._candidate_payload({"assets": assets})
        self.assertEqual(len(result), 500)
        self.assertEqual(result[0]["symbol"], "COIN599USDT")
        self.assertNotIn("private_field", result[0])

    def test_validation_rejects_unknown_symbols_and_defaults_missing_to_wait(self):
        candidates = [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]
        raw = {
            "market_bias": "mixed",
            "decisions": [
                {"symbol": "BTCUSDT", "action": "SHORT", "confidence": 130,
                 "reason": "downtrend"},
                {"symbol": "FAKEUSDT", "action": "LONG", "confidence": 99,
                 "reason": "not in input"},
            ],
        }
        result = ai_advisor._validated_result(raw, candidates, "gemini-3.5-flash-lite")
        by_symbol = {item["symbol"]: item for item in result["recommendations"]}
        self.assertEqual(result["market_bias"], "MIXED")
        self.assertEqual(by_symbol["BTCUSDT"]["action"], "SHORT")
        self.assertEqual(by_symbol["BTCUSDT"]["confidence"], 100)
        self.assertEqual(by_symbol["ETHUSDT"]["action"], "WAIT")
        self.assertNotIn("FAKEUSDT", by_symbol)

    def test_extract_json_accepts_gemini_candidate_text(self):
        response = {
            "candidates": [{
                "content": {"parts": [{"text": '{"market_bias":"WAIT","decisions":[]}'}]}
            }]
        }
        self.assertEqual(ai_advisor._extract_json(response)["market_bias"], "WAIT")

    def test_retry_delay_prefers_retry_after_and_is_bounded(self):
        self.assertEqual(ai_advisor._retry_delay_seconds("3", 0), 3.0)
        self.assertEqual(ai_advisor._retry_delay_seconds("999", 0), 15.0)
        self.assertEqual(ai_advisor._retry_delay_seconds(None, 0), 2.0)
        self.assertEqual(ai_advisor._retry_delay_seconds(None, 1), 4.0)

    def test_retryable_status_codes_cover_rate_limit_and_server_overload(self):
        self.assertEqual(ai_advisor._RETRYABLE_STATUS_CODES, {429, 500, 502, 503, 504})

    def test_missing_api_key_disables_advisor_without_network_call(self):
        async def run():
            with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
                with patch("app.ai_advisor.httpx.AsyncClient") as client:
                    result = await ai_advisor.refresh_ai_advisor({"assets": []})
                    client.assert_not_called()
            self.assertEqual(result["status"], "NOT_CONFIGURED")
            self.assertEqual(result["mode"], "SHADOW")

        import asyncio
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
