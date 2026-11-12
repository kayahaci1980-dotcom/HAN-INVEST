# HAN INVEST automatic paper trading deployment

This feature branch adds automatic virtual spot trading for Binance, Bybit, Coinbase Exchange, and OKX.

## Trading rules

- Open a virtual position when the exchange-verified spot asset has score >= 83. A separate BUY signal and bullish BTC filter are not required.
- Close when the mark price is <= 98% of the entry price (a 2% loss), or when the current score is below 83.
- If the mark price reaches >= 110% of entry, close and immediately reopen the same asset. This take-profit re-entry deliberately bypasses the score >= 83 entry rule. The normal score exit rule applies again on the next scan.
- ATR(14) from completed daily candles is used for position sizing. The displayed stop and target are the user's fixed -2% and +10% levels.
- The account starts with 1,000 USDT, uses virtual fills only, and does not submit exchange orders.
- The worker scans every 300 seconds after each completed cycle; it runs inside the API server, not inside the Android process.

## Required Render service settings

The Android app currently points to the test API service. Deploy this branch to that test service first and verify it before changing any production service.

1. Use an always-on Render web service plan. A sleeping/free instance cannot provide reliable 24/7 trading.
2. Attach a persistent disk mounted at `/var/data`.
3. Set environment variable `PAPER_DB_PATH=/var/data/han-invest-paper.sqlite3`.
4. Set `PAPER_SCAN_INTERVAL_SECONDS=300`.
5. Deploy branch `auto-paper-rules-coinbase-okx`.
6. Check `/health` and `/api/v1/paper/state`. The state must report `storage_persistent_configured: true`.
7. In the Android app, open the virtual account and enable the server automatic mode only after the persistent disk and deployment are verified.

Without the persistent disk, the API intentionally blocks enabling automatic paper trading to avoid losing the virtual account after a restart.
