package com.haninvest.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.text.InputType
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class CryptoScanner(private val context: Context) {
    private val blue = Color.rgb(55, 105, 245)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private fun background(color: Int) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(14).toFloat()
    }

    private fun text(value: String, size: Float, color: Int, bold: Boolean = false) =
        TextView(context).apply {
            text = value
            textSize = size
            setTextColor(color)
            if (bold) setTypeface(null, Typeface.BOLD)
        }

    fun createView(): View {
        val section = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        section.addView(text("₿ Криптосканер HAN INVEST", 21f, dark, true))
        section.addView(text(
            "Рейтинг монет по рыночной активности. WATCH — наблюдать, не команда покупать.",
            13f, muted
        ))
        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(15), dp(16), dp(15))
            background = background(Color.WHITE)
            elevation = dp(2).toFloat()
        }
        val capitalInput = EditText(context).apply {
            hint = "Ваш капитал в USDT"
            setText("100")
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            textSize = 14f
            setSingleLine(true)
        }
        val riskInput = EditText(context).apply {
            hint = "Риск на сделку, % (по умолчанию 1)"
            setText("1")
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            textSize = 14f
            setSingleLine(true)
        }
        val status = text("Нажмите «Обновить», чтобы загрузить рейтинг.", 14f, dark)
        val refresh = Button(context).apply {
            this.text = "ОБНОВИТЬ КРИПТОСКАНЕР"
            setTextColor(Color.WHITE)
            background = background(blue)
            setOnClickListener {
                val capital = capitalInput.text.toString().trim().toDoubleOrNull()
                val risk = riskInput.text.toString().trim().toDoubleOrNull() ?: 1.0
                if (capitalInput.text.isNotBlank() && (capital == null || capital <= 0.0)) {
                    capitalInput.error = "Введите капитал больше нуля"
                } else if (risk !in 0.1..5.0) {
                    riskInput.error = "Допустимый риск: от 0,1% до 5%"
                } else {
                    load(status, capital, risk)
                }
            }
        }
        panel.addView(text("РАСЧЁТ РАЗМЕРА ПОЗИЦИИ", 13f, muted, true))
        panel.addView(capitalInput)
        panel.addView(riskInput)
        panel.addView(text("Риск по умолчанию — 1% капитала. Только спотовая торговля, без плеча.", 12f, muted))
        panel.addView(status)
        panel.addView(refresh)
        val autoRefreshLabel = text(
            "Автообновление каждые 15 минут включено.",
            12f, muted
        )
        panel.addView(autoRefreshLabel)

        val autoRefreshHandler = Handler(Looper.getMainLooper())
        val autoRefreshTask = object : Runnable {
            override fun run() {
                if (!section.isAttachedToWindow) return
                val capital = capitalInput.text.toString().trim().toDoubleOrNull()
                val risk = riskInput.text.toString().trim().toDoubleOrNull() ?: 1.0
                if (capital != null && capital > 0.0 && risk in 0.1..5.0) {
                    autoRefreshLabel.text = "Автоматически обновляем рынок..."
                    load(status, capital, risk)
                }
                autoRefreshHandler.postDelayed(this, 15L * 60L * 1000L)
            }
        }
        section.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(view: View) {
                autoRefreshHandler.removeCallbacks(autoRefreshTask)
                autoRefreshHandler.postDelayed(autoRefreshTask, 15L * 60L * 1000L)
            }

            override fun onViewDetachedFromWindow(view: View) {
                autoRefreshHandler.removeCallbacks(autoRefreshTask)
            }
        })
        section.addView(panel)
        section.addView(text(
            "Источник: HAN INVEST API. Резервные агрегированные данные не подтверждают торговую пару на бирже.",
            11f, muted
        ))
        load(status, 100.0, 1.0)
        return section
    }

    private fun load(target: TextView, capitalUsdt: Double? = null, riskPct: Double = 1.0) {
        target.text = "Загрузка рейтинга криптовалют..."
        Thread {
            try {
                val query = buildString {
                    append("limit=30&universe_limit=100&min_volume=1000000&risk_pct=")
                    append(riskPct)
                    if (capitalUsdt != null) {
                        append("&capital_usdt=")
                        append(capitalUsdt)
                    }
                }
                val connection = URL(
                    "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?$query"
                ).openConnection() as HttpURLConnection
                val jsonText = try {
                    connection.requestMethod = "GET"
                    connection.connectTimeout = 20000
                    connection.readTimeout = 25000
                    connection.setRequestProperty("Accept", "application/json")
                    connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.4")
                    if (connection.responseCode !in 200..299) {
                        throw Exception("HTTP ${connection.responseCode}")
                    }
                    connection.inputStream.bufferedReader().use { it.readText() }
                } finally {
                    connection.disconnect()
                }
                val json = JSONObject(jsonText)
                val assets = json.optJSONArray("assets")
                    ?: throw Exception("Нет списка активов")
                val out = StringBuilder()
                val sourceName = json.optString("source", "Неизвестный источник")
                val isBinanceSource = sourceName.contains("Binance public market data", ignoreCase = true) &&
                    !sourceName.contains("fallback", ignoreCase = true)
                out.append("🟡 BINANCE SPOT  |  Источник: $sourceName\n")
                out.append("Проверено монет: ${json.optInt("scanned", assets.length())}\n")
                if (isBinanceSource) {
                    out.append("Подходящих спотовых пар Binance: ${json.optInt("eligible_pairs", 0)}\n")
                } else {
                    out.append("Проверка торговых пар Binance: НЕ ВЫПОЛНЕНА\n")
                    out.append("ВНИМАНИЕ: РЕЗЕРВНЫЙ ИСТОЧНИК — ТОРГОВЛЯ НЕ ПОДТВЕРЖДЕНА\n")
                    out.append("MA20, MA50 и ATR недоступны. Сигналы BUY запрещены.\n")
                }
                out.append("Лучших кандидатов в списке: ${assets.length()}\n")
                val btc = json.optJSONObject("btc_market_filter")
                if (btc != null && btc.optBoolean("available", false)) {
                    val marketLabel = btc.optString("market_state_label", "Не определён")
                    val marketReason = btc.optString("reason", "")
                    out.append("СОСТОЯНИЕ РЫНКА BTC: $marketLabel\n")
                    out.append("BTC: ${btc.optString("price", "—")} | MA20: ${btc.optString("ma20", "—")} | MA50: ${btc.optString("ma50", "—")}\n")
                    out.append("Покупки разрешены: ${if (btc.optBoolean("buy_allowed")) "ДА" else "НЕТ"}\n")
                    if (marketReason.isNotBlank()) out.append("$marketReason\n")
                } else {
                    out.append("СОСТОЯНИЕ РЫНКА BTC: данные недоступны — BUY заблокирован\n")
                }
                out.append("При каждом обновлении сканирование выполняется заново.\n\n")
                // Избранные активы повторно проверяются на каждом запуске сканера.
                val watchSymbols = listOf("0GUSDT", "WLFIUSDT", "AVAXUSDT")
                out.append("ИЗБРАННЫЕ МОНЕТЫ — ПОВТОРНОЕ НАБЛЮДЕНИЕ\n")
                for (watchSymbol in watchSymbols) {
                    val watchedCoin = (0 until assets.length())
                        .map { assets.getJSONObject(it) }
                        .firstOrNull { it.optString("symbol") == watchSymbol }
                    if (watchedCoin == null) {
                        out.append("$watchSymbol: сейчас не входит в лучшие 30\n")
                    } else {
                        val watchedPrice = watchedCoin.optDouble("price", Double.NaN)
                        val watchedVolume = watchedCoin.optDouble("volume_ratio", Double.NaN)
                        out.append("$watchSymbol | № ${watchedCoin.optInt("market_rank")} | ${watchedCoin.optString("signal", "WATCH")}")
                        if (!watchedPrice.isNaN()) out.append(String.format(Locale.US, " | Цена: $%.6f", watchedPrice))
                        if (!watchedVolume.isNaN()) out.append(String.format(Locale.US, " | Объём: %.2fx", watchedVolume))
                        out.append(" | Объём подтверждён: ${if (watchedCoin.optBoolean("volume_confirmed")) "ДА" else "НЕТ"}\n")
                    }
                }
                out.append("\n")
                for (i in 0 until assets.length()) {
                    val coin = assets.getJSONObject(i)
                    val symbol = coin.optString("symbol")
                    val name = coin.optString("name", symbol)
                    val price = coin.optDouble("price", Double.NaN)
                    val change = coin.optDouble("change_24h_pct", Double.NaN)
                    val score = coin.optInt("score", 0)
                    val signal = coin.optString("signal", "WATCH")
                    out.append("№ ${coin.optInt("market_rank", i + 1)}. $name ($symbol)\n")
                    if (!price.isNaN()) {
                        out.append(String.format(Locale.US, "Цена: \\$%.6f USD\n", price))
                    }
                    if (!change.isNaN()) {
                        out.append(String.format(Locale.US, "Изменение 24ч: %.2f%%\n", change))
                    }
                    val earlySignal = coin.optString("early_signal", "")
                    out.append("Оценка: $score/100  |  Сигнал: $signal")
                    if (earlySignal == "EARLY BUY") {
                        out.append("  |  РАННИЙ BUY — не подтверждённая покупка")
                    }
                    out.append("\n")
                    val volumeRatio = coin.optDouble("volume_ratio", Double.NaN)
                    if (!volumeRatio.isNaN()) {
                        out.append(String.format(Locale.US, "Объём к среднему: %.2fx\n", volumeRatio))
                    }
                    out.append("Объём подтверждён: ${if (coin.optBoolean("volume_confirmed")) "ДА" else "НЕТ"}\n")
                    out.append("MA20: ${coin.optString("ma20", "—")} | MA50: ${coin.optString("ma50", "—")}\n")
                    // Explain every failed condition behind BUY / EARLY BUY.
                    val ma20Value = coin.optDouble("ma20", Double.NaN)
                    val ma50Value = coin.optDouble("ma50", Double.NaN)
                    val currentPrice = coin.optDouble("price", Double.NaN)
                    val change6h = coin.optDouble("momentum_6h_pct", Double.NaN)
                    val quoteVolume = coin.optDouble("quote_volume_24h", 0.0)
                    val checksFailed = mutableListOf<String>()
                    if (score < 80) checksFailed.add("Оценка ниже 80 (сейчас $score)")
                    if (ma20Value.isNaN() || ma50Value.isNaN() || currentPrice.isNaN() ||
                        !(currentPrice > ma20Value && ma20Value > ma50Value)) {
                        checksFailed.add("Тренд: цена должна быть выше MA20, а MA20 выше MA50")
                    }
                    if (change6h.isNaN() || change6h < 1.0) {
                        checksFailed.add("Рост за 6 часов меньше 1% или нет данных")
                    }
                    if (volumeRatio.isNaN() || volumeRatio < 1.2) {
                        checksFailed.add("Объём ниже 1.2x")
                    }
                    if (quoteVolume < 1_000_000) {
                        checksFailed.add("Оборот за 24 часа ниже 1 млн USDT")
                    }
                    val btcState = btc?.optString("market_state", "UNKNOWN") ?: "UNKNOWN"
                    if (btcState != "SIDEWAYS" && btcState != "BULLISH") {
                        checksFailed.add("Рынок BTC не боковой — ранний BUY не разрешён")
                    }
                    if (signal != "BUY" && earlySignal != "EARLY BUY") {
                        out.append("ПОЧЕМУ НЕТ BUY: ")
                        out.append(if (checksFailed.isEmpty()) "условия сигнала не подтверждены\n"
                                   else checksFailed.joinToString("; ") + "\n")
                    } else if (earlySignal == "EARLY BUY") {
                        out.append("ПОЧЕМУ EARLY BUY: условия актива выполнены, BTC в боковом рынке. Это предупреждение, не подтверждённая покупка.\n")
                    }
                    out.append("ATR(14): ${coin.optString("atr_14", "—")}\n")
                    if (signal == "BUY") {
                        out.append("Вход: ${coin.optString("entry_price", "—")} USDT\n")
                        out.append("Stop Loss: ${coin.optString("stop_loss", "—")} USDT\n")
                        out.append("Take Profit: ${coin.optString("take_profit", "—")} USDT\n")
                        val sizing = coin.optJSONObject("position_sizing")
                        if (sizing != null && sizing.optBoolean("available", false)) {
                            out.append("Количество: ${sizing.optString("quantity")}\n")
                            out.append("Размер позиции: ${sizing.optString("position_value_usdt")} USDT\n")
                            out.append("Плановый риск: ${sizing.optString("potential_loss_at_stop_usdt")} USDT\n")
                            out.append("Потенциальная прибыль: ${sizing.optString("potential_profit_at_target_usdt")} USDT\n")
                        } else if (capitalUsdt == null) {
                            out.append("Введите капитал, чтобы рассчитать размер позиции.\n")
                        }
                    }
                    if (!isBinanceSource) {
                        out.append("РЕЗЕРВНЫЕ ДАННЫЕ: монета не проверена как спотовая пара Binance — НЕ ТОРГОВАТЬ ПО ЭТОМУ СПИСКУ\n")
                    } else if (coin.optBoolean("trade_ready", false)) {
                        out.append("Торговая пара подтверждена\n")
                    } else {
                        out.append("Пара не подтверждена — не готово к сделке\n")
                    }
                    out.append("\n")
                }
                // Fetch Bybit independently: never mix its prices or indicators with Binance rows.
                try {
                    val bybitConnection = URL(
                        "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000&capital_usdt=${capitalUsdt ?: 100.0}&risk_pct=${riskPct}"
                    ).openConnection() as HttpURLConnection
                    val bybitText = try {
                        bybitConnection.requestMethod = "GET"
                        bybitConnection.connectTimeout = 20000
                        bybitConnection.readTimeout = 30000
                        bybitConnection.setRequestProperty("Accept", "application/json")
                        if (bybitConnection.responseCode !in 200..299) throw Exception("Bybit API unavailable")
                        bybitConnection.inputStream.bufferedReader().use { it.readText() }
                    } finally {
                        bybitConnection.disconnect()
                    }
                    val bybitJson = JSONObject(bybitText)
                    val bybitAssets = bybitJson.optJSONArray("assets")
                    out.append("\n🟠 BYBIT SPOT — ОТДЕЛЬНЫЙ ИСТОЧНИК\n")
                    out.append("Проверено пар: ${bybitJson.optInt("eligible_pairs")} | Кандидатов: ${bybitJson.optInt("scanned")}\n")
                    val bybitBtc = bybitJson.optJSONObject("btc_market_filter")
                    out.append("BTC Bybit: ${bybitBtc?.optString("market_state", "UNKNOWN") ?: "UNKNOWN"}\n\n")
                    if (bybitAssets != null) {
                        for (i in 0 until bybitAssets.length()) {
                            val coin = bybitAssets.getJSONObject(i)
                            out.append("🟠 № ${coin.optInt("market_rank", i + 1)} ${coin.optString("symbol")}\n")
                            out.append(String.format(Locale.US, "Цена: $%.8f | Оценка: %d/100\n",
                                coin.optDouble("price"), coin.optInt("score")))
                            out.append("Сигнал: ${coin.optString("signal", "WATCH")}")
                            if (coin.optString("early_signal") == "EARLY BUY") {
                                out.append(" | EARLY BUY — только наблюдение, не покупка")
                            }
                            out.append("\nMA20: ${coin.optString("ma20")} | MA50: ${coin.optString("ma50")}\n")
                            out.append("ATR(14): ${coin.optString("atr_14", "—")}\n")
                            out.append("Объём: ${coin.optString("volume_ratio", "—")}x | Подтверждён: ${if (coin.optBoolean("volume_confirmed")) "ДА" else "НЕТ"}\n")
                            out.append("Пара Spot: ${if (coin.optBoolean("trade_ready")) "ДА" else "НЕТ"}\n")
                            if (coin.optString("signal") == "BUY") {
                                out.append("Вход: ${coin.optString("entry_price", "—")} USDT | Stop Loss: ${coin.optString("stop_loss", "—")} USDT | Take Profit: ${coin.optString("take_profit", "—")} USDT\n")
                                val bybitSizing = coin.optJSONObject("position_sizing")
                                if (bybitSizing != null && bybitSizing.optBoolean("available", false)) {
                                    out.append("Количество: ${bybitSizing.optString("quantity")} | Позиция: ${bybitSizing.optString("position_value_usdt")} USDT\n")
                                    out.append("Риск при Stop Loss: ${bybitSizing.optString("potential_loss_at_stop_usdt")} USDT\n")
                                    out.append("Потенциальная прибыль: ${bybitSizing.optString("potential_profit_at_target_usdt")} USDT\n")
                                } else {
                                    out.append("Размер позиции не рассчитан — не использовать BUY без расчёта риска.\n")
                                }
                            }
                            out.append("\n")
                        }
                    }
                } catch (bybitError: Exception) {
                    out.append("\n🟠 BYBIT: данные временно недоступны — Binance продолжает работать отдельно.\n")
                }
                val notice = json.optString("notice")
                if (notice.isNotBlank()) out.append("\n$notice")
                (context as? android.app.Activity)?.runOnUiThread {
                    target.text = out.toString()
                }
            } catch (error: Exception) {
                (context as? android.app.Activity)?.runOnUiThread {
                    target.text = "Не удалось загрузить рейтинг. Проверьте интернет и повторите попытку."
                }
            }
        }.start()
    }
}
