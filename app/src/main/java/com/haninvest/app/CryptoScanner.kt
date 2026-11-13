package com.haninvest.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.max

class CryptoScanner(private val context: Context) {
    private val blue = Color.rgb(55, 105, 245)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)

    private fun dp(v: Int) =
        (v * context.resources.displayMetrics.density).toInt()

    private fun label(v: String, s: Float, c: Int, b: Boolean = false) =
        TextView(context).apply {
            text = v
            textSize = s
            setTextColor(c)
            if (b) setTypeface(null, Typeface.BOLD)
        }

    private fun fetch(url: String): String {
        val c = URL(url).openConnection() as HttpURLConnection
        return try {
            c.connectTimeout = 12000
            c.readTimeout = 18000
            c.setRequestProperty("User-Agent", "HAN-INVEST-Android/3.0")
            c.setRequestProperty("Accept", "application/json")
            if (c.responseCode !in 200..299) throw Exception("HTTP " + c.responseCode)
            c.inputStream.bufferedReader().use { it.readText() }
        } finally {
            c.disconnect()
        }
    }

    private data class Metrics(
        val score: Int,
        val rsi: Double,
        val atrPct: Double,
        val ema: Double,
        val trend: String
    )

    private fun metrics(candles: List<DoubleArray>, change: Double, turnover: Double): Metrics? {
        if (candles.size < 22) return null
        val closes = candles.map { it[3] }
        val highs = candles.map { it[1] }
        val lows = candles.map { it[2] }
        val last = closes.last()
        if (last <= 0.0) return null

        var gains = 0.0
        var losses = 0.0
        for (i in closes.size - 14 until closes.size) {
            val d = closes[i] - closes[i - 1]
            if (d > 0) gains += d else losses -= d
        }
        val avgGain = gains / 14.0
        val avgLoss = losses / 14.0
        val rsi = if (avgLoss == 0.0) 100.0
            else 100.0 - (100.0 / (1.0 + avgGain / avgLoss))

        val emaPeriod = 20.0
        val alpha = 2.0 / (emaPeriod + 1.0)
        var ema = closes.take(20).average()
        for (p in closes.drop(20)) ema = p * alpha + ema * (1.0 - alpha)

        val trs = mutableListOf<Double>()
        for (i in max(1, candles.size - 14) until candles.size) {
            val prev = closes[i - 1]
            trs.add(max(highs[i] - lows[i], max(abs(highs[i] - prev), abs(lows[i] - prev))))
        }
        val atrPct = if (trs.isEmpty()) 0.0 else trs.average() / last * 100.0

        var score = 0
        if (last > ema) score += 30
        if (rsi in 45.0..65.0) score += 25
        else if (rsi in 35.0..70.0) score += 15
        if (change > 0.0) score += 20
        if (turnover >= 1_000_000.0) score += 15
        else if (turnover >= 100_000.0) score += 8
        if (atrPct in 0.5..5.0) score += 10

        return Metrics(
            score.coerceIn(0, 100),
            rsi,
            atrPct,
            ema,
            if (last > ema) "выше EMA20" else "ниже EMA20"
        )
    }

    private fun coinbaseRows(): List<JSONObject> {
        val products = JSONArray(fetch("https://api.exchange.coinbase.com/products"))
        val allowed = setOf(
            "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH",
            "LINK", "AVAX", "DOT", "UNI", "XLM", "ETC", "NEAR", "SUI",
            "HBAR", "AAVE", "ATOM", "FIL", "APT", "ARB", "OP", "TRX"
        )
        val ids = mutableListOf<String>()
        for (i in 0 until products.length()) {
            val p = products.optJSONObject(i) ?: continue
            val base = p.optString("base_currency")
            val quote = p.optString("quote_currency")
            if (base in allowed && (quote == "USD" || quote == "USDT") &&
                !p.optBoolean("trading_disabled")) {
                ids.add(p.optString("id"))
            }
        }

        val pool = Executors.newFixedThreadPool(6)
        try {
            val tasks = ids.map { id ->
                Callable {
                    val ticker = JSONObject(fetch("https://api.exchange.coinbase.com/products/$id/ticker"))
                    val stats = JSONObject(fetch("https://api.exchange.coinbase.com/products/$id/stats"))
                    val price = ticker.optString("price").toDoubleOrNull() ?: return@Callable null
                    val volume = stats.optString("volume").toDoubleOrNull() ?: 0.0
                    val open = stats.optString("open").toDoubleOrNull() ?: 0.0
                    val base = id.substringBefore("-")
                    val quote = id.substringAfter("-", "USD")
                    val change = if (open > 0) (price - open) / open * 100.0 else 0.0
                    JSONObject()
                        .put("id", id)
                        .put("symbol", base + "/" + quote)
                        .put("price", price)
                        .put("change_24h_pct", change)
                        .put("volume_24h", volume)
                        .put("_turnover", volume * price)
                }
            }
            val rows = mutableListOf<JSONObject>()
            for (future in pool.invokeAll(tasks, 35, TimeUnit.SECONDS)) {
                try { future.get()?.let { rows.add(it) } } catch (_: Exception) { }
            }
            return rows.sortedByDescending { it.optDouble("_turnover") }.take(3)
        } finally {
            pool.shutdownNow()
        }
    }

    private fun coinbaseCandles(id: String): List<DoubleArray> {
        val fmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        fmt.timeZone = TimeZone.getTimeZone("UTC")
        val end = System.currentTimeMillis()
        val start = end - 60L * 60L * 1000L * 60L
        val url = "https://api.exchange.coinbase.com/products/$id/candles?granularity=3600&start=" +
            java.net.URLEncoder.encode(fmt.format(Date(start)), "UTF-8") +
            "&end=" + java.net.URLEncoder.encode(fmt.format(Date(end)), "UTF-8")
        val arr = JSONArray(fetch(url))
        val rows = mutableListOf<DoubleArray>()
        for (i in 0 until arr.length()) {
            val a = arr.optJSONArray(i) ?: continue
            if (a.length() < 6) continue
            rows.add(doubleArrayOf(
                a.optDouble(0), a.optDouble(2), a.optDouble(1),
                a.optDouble(4), a.optDouble(5)
            ))
        }
        return rows.sortedBy { it[0] }
    }

    private fun okxCandles(instId: String): List<DoubleArray> {
        val arr = JSONObject(fetch(
            "https://www.okx.com/api/v5/market/candles?instId=$instId&bar=1H&limit=60"
        )).optJSONArray("data") ?: JSONArray()
        val rows = mutableListOf<DoubleArray>()
        for (i in 0 until arr.length()) {
            val a = arr.optJSONArray(i) ?: continue
            if (a.length() < 6) continue
            rows.add(doubleArrayOf(
                a.optString(0).toDoubleOrNull() ?: 0.0,
                a.optString(2).toDoubleOrNull() ?: 0.0,
                a.optString(3).toDoubleOrNull() ?: 0.0,
                a.optString(4).toDoubleOrNull() ?: 0.0,
                a.optString(5).toDoubleOrNull() ?: 0.0
            ))
        }
        return rows.sortedBy { it[0] }
    }

    private fun normalizedSymbol(raw: String): String {
        val cleaned = raw.uppercase(Locale.US).replace("/", "").replace("-", "").replace("_", "")
        return when {
            cleaned.endsWith("USDT") -> cleaned
            cleaned.endsWith("USD") -> cleaned
            else -> cleaned + "USDT"
        }
    }

    private fun exchangeCandles(venue: String, row: JSONObject): List<DoubleArray> {
        val symbol = normalizedSymbol(row.optString("symbol", row.optString("id")))
        if (venue == "okx") return okxCandles(row.optString("id"))
        if (venue == "coinbase") return coinbaseCandles(row.optString("id"))

        val raw = if (venue == "binance") {
            val arr = JSONArray(fetch("https://api.binance.com/api/v3/klines?symbol=$symbol&interval=1h&limit=60"))
            val list = mutableListOf<DoubleArray>()
            for (i in 0 until arr.length()) {
                val a = arr.optJSONArray(i) ?: continue
                if (a.length() < 6) continue
                list.add(doubleArrayOf(
                    a.optString(0).toDoubleOrNull() ?: 0.0,
                    a.optString(2).toDoubleOrNull() ?: 0.0,
                    a.optString(3).toDoubleOrNull() ?: 0.0,
                    a.optString(4).toDoubleOrNull() ?: 0.0,
                    a.optString(5).toDoubleOrNull() ?: 0.0
                ))
            }
            list
        } else {
            val data = JSONObject(fetch(
                "https://api.bybit.com/v5/market/kline?category=spot&symbol=$symbol&interval=60&limit=60"
            )).optJSONObject("result")?.optJSONArray("list") ?: JSONArray()
            val list = mutableListOf<DoubleArray>()
            for (i in 0 until data.length()) {
                val a = data.optJSONArray(i) ?: continue
                if (a.length() < 6) continue
                list.add(doubleArrayOf(
                    a.optString(0).toDoubleOrNull() ?: 0.0,
                    a.optString(2).toDoubleOrNull() ?: 0.0,
                    a.optString(3).toDoubleOrNull() ?: 0.0,
                    a.optString(4).toDoubleOrNull() ?: 0.0,
                    a.optString(5).toDoubleOrNull() ?: 0.0
                ))
            }
            list
        }
        return raw.sortedBy { it[0] }
    }

    private fun buyBlockReason(row: JSONObject): String {
        val score = row.optInt("score", 0)
        return when {
            score < 83 -> "Оценка ниже порога 83"
            !row.optBoolean("trade_ready", false) -> "Пара не подтверждена"
            !row.optBoolean("volume_confirmed", false) -> "Недостаточный объём"
            !row.optBoolean("btc_buy_allowed", false) -> "BTC не в восходящем тренде"
            row.optString("signal") != "BUY" -> "Сигнал стратегии: " + row.optString("signal", "нет")
            else -> "Фильтры пройдены; проверяются баланс, открытая позиция и блокировка после убытка"
        }
    }

    private fun firstNumber(row: JSONObject, vararg keys: String): Double {
        for (key in keys) {
            if (row.has(key)) {
                val value = row.optString(key).toDoubleOrNull()
                if (value != null && value.isFinite()) return value
            }
        }
        return Double.NaN
    }

    private fun loadVenue(venue: String): String {
        val endpoint = when (venue) {
            "binance" -> "/api/v1/crypto/scan?limit=30&universe_limit=100&min_volume=1000000&risk_pct=1"
            "bybit" -> "/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000"
            "okx" -> "/api/v1/crypto/okx/scan?limit=30&min_volume=1000000"
            else -> "/api/v1/crypto/coinbase/scan?limit=30&min_volume=1000000"
        }
        val json = JSONObject(fetch("https://han-invest-api-test.onrender.com$endpoint"))
        val assets = json.optJSONArray("assets") ?: JSONArray()
        if (assets.length() == 0) throw Exception("Нет доступных торговых пар")

        val rows = (0 until assets.length()).mapNotNull { assets.optJSONObject(it) }
            .sortedByDescending { it.optInt("score", 0) }
            .take(3)
        val out = StringBuilder()
        out.append(venue.uppercase(Locale.US)).append(" — ТОП-3 по технической оценке\n")
        for ((index, row) in rows.withIndex()) {
            val symbol = row.optString("symbol", "—")
            val price = firstNumber(row, "price")
            val change = firstNumber(row, "change_24h_pct")
            val turnover = firstNumber(row, "quote_volume_24h")
            val score = row.optInt("score", -1)
            val rsi = firstNumber(row, "rsi_14")
            val atrPct = firstNumber(row, "atr_pct")
            val ema20 = firstNumber(row, "ema20")
            out.append("\n").append(index + 1).append(". ").append(symbol).append("\n")
            if (!price.isNaN()) out.append(String.format(Locale.US, "Цена: $%,.8f\n", price))
            if (!change.isNaN()) out.append(String.format(Locale.US, "Изменение 24ч: %+.2f%%\n", change))
            if (!turnover.isNaN()) out.append(String.format(Locale.US, "Оборот 24ч: $%,.0f\n", turnover))
            if (score >= 0) out.append("Техническая оценка: ").append(score).append("/100\n")
            if (!rsi.isNaN()) out.append(String.format(Locale.US, "RSI(14): %.1f\n", rsi))
            if (!atrPct.isNaN()) out.append(String.format(Locale.US, "ATR(14): %.2f%%\n", atrPct))
            if (!ema20.isNaN()) {
                val trend = if (!price.isNaN() && price > ema20) "выше EMA20" else "ниже EMA20"
                out.append(String.format(Locale.US, "EMA20: $%,.8f — %s\n", ema20, trend))
            }
            out.append("Сигнал: ").append(row.optString("signal", "—")).append("\n")
            out.append("Почему не покупает: ").append(buyBlockReason(row)).append("\n")
        }
        out.append("\nОбновлено: ").append(
            SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date())
        )
        out.append("\nВсе биржи используют единый расчёт RSI Wilder, ATR Wilder, EMA20/50 и оценки из 100 баллов.")
        out.append("\nДанные только для анализа; реальные ордера не отправляются.")
        return out.toString()
    }

    fun createView(): View {
        val root = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        root.addView(label("₿ Крипторынок — все биржи", 21f, dark, true))
        root.addView(label(
            "Binance, Bybit, OKX и Coinbase Exchange: топ‑3 и одинаковые технические показатели. Одна кнопка обновляет все четыре биржи одновременно.",
            13f, muted
        ))

        val refreshAll = Button(context).apply {
            text = "ОБНОВИТЬ ВСЕ БИРЖИ"
            setTextColor(Color.WHITE)
            textSize = 15f
            background = GradientDrawable().apply {
                setColor(blue)
                cornerRadius = dp(12).toFloat()
            }
        }
        root.addView(refreshAll)

        val venues = listOf(
            "binance" to "BINANCE",
            "bybit" to "BYBIT",
            "okx" to "OKX",
            "coinbase" to "COINBASE EXCHANGE"
        )
        val statusViews = linkedMapOf<String, TextView>()
        for ((key, title) in venues) {
            val panel = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(14), dp(13), dp(14), dp(13))
                background = GradientDrawable().apply {
                    setColor(Color.WHITE)
                    cornerRadius = dp(16).toFloat()
                }
                elevation = dp(2).toFloat()
            }
            val heading = label(title, 16f, dark, true)
            val status = label("Ожидание обновления...", 13f, dark)
            status.setPadding(0, dp(10), 0, dp(8))
            statusViews[key] = status
            panel.addView(heading)
            panel.addView(status)
            root.addView(panel)
            root.addView(View(context).apply {
                layoutParams = LinearLayout.LayoutParams(1, dp(12))
            })
        }

        fun refreshAllExchanges() {
            refreshAll.isEnabled = false
            refreshAll.text = "ОБНОВЛЯЮ ВСЕ БИРЖИ..."
            for ((key, title) in venues) {
                statusViews[key]?.text = "Загрузка $title..."
            }
            val completed = java.util.concurrent.atomic.AtomicInteger(0)
            for ((key, title) in venues) {
                Thread {
                    val result = try {
                        loadVenue(key)
                    } catch (e: Exception) {
                        "$title: данные временно недоступны. " +
                            (e.message ?: "Ошибка соединения")
                    }
                    (context as? android.app.Activity)?.runOnUiThread {
                        statusViews[key]?.text = result
                        if (completed.incrementAndGet() == venues.size) {
                            refreshAll.isEnabled = true
                            refreshAll.text = "ОБНОВИТЬ ВСЕ БИРЖИ"
                        }
                    }
                }.start()
            }
        }

        refreshAll.setOnClickListener { refreshAllExchanges() }
        refreshAllExchanges()
        return root
    }
}
