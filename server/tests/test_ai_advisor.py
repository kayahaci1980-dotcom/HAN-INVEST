import unittest
from unittest.mock import patch

from app import ai_advisor


class GeminiAdvisorTests(unittest.TestCase):
    def test_groq_reset_headers_are_parsed_and_respected(self):
        from datetime import datetime, timezone
        self.assertAlmostEqual(ai_advisor._duration_header_seconds("7.6s"), 7.6)
        self.assertAlmostEqual(ai_advisor._duration_header_seconds("2m59.5s"), 179.5)
        self.assertEqual(ai_advisor._duration_header_seconds("invalid"), 0.0)
        headers = {
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "7.6s",
            "x-ratelimit-remaining-requests": "20",
        }
        delay = ai_advisor._set_groq_rate_limit_cooldown(response_headers=headers)
        self.assertAlmostEqual(delay, 180.0)
        remaining = ai_advisor._groq_cooldown_remaining(datetime.now(timezone.utc))
        self.assertGreater(remaining, 170.0)

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

    def test_groq_cooldown_uses_gemini_with_prebuilt_request_body(self):
        async def run():
            from datetime import datetime, timedelta, timezone

            candidates = [{
                "symbol": "BTCUSDT", "quote_volume_24h": 10_000_000,
                "price": 100.0, "trade_ready": True, "long_score": 90,
                "short_score": 0,
            }]
            async def fake_gemini(received_candidates, body, api_key):
                self.assertEqual(received_candidates, ai_advisor._candidate_payload({"assets": candidates}))
                self.assertIn("contents", body)
                self.assertEqual(api_key, "gemini-test-key")
                return {
                    "available": True, "mode": "SHADOW",
                    "provider": "Google Gemini (fallback)", "model": "test",
                    "status": "OK", "market_bias": "LONG",
                    "recommendations": [{
                        "symbol": "BTCUSDT", "action": "LONG",
                        "confidence": 90, "reason": "test",
                    }],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

            ai_advisor._last_success_fingerprint = None
            ai_advisor._last_success_result = None
            with patch.dict("os.environ", {
                "GROQ_API_KEY": "groq-test-key",
                "GEMINI_API_KEY": "gemini-test-key",
            }, clear=False), patch.object(
                ai_advisor, "_groq_cooldown_until",
                datetime.now(timezone.utc) + timedelta(seconds=60),
            ), patch.object(ai_advisor, "_gemini_fallback", side_effect=fake_gemini) as fallback:
                result = await ai_advisor.refresh_ai_advisor({"assets": candidates})
                fallback.assert_awaited_once()
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["provider"], "Google Gemini (fallback)")
            self.assertEqual(result["recommendations"][0]["action"], "LONG")

        import asyncio
        asyncio.run(run())

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
