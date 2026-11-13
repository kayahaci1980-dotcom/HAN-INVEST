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
            rows.sortedByDescending { it.optDouble("_turnover") }.take(3)
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
        rows.sortedBy { it[0] }
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
        rows.sortedBy { it[0] }
    }

    private fun loadVenue(venue: String): String {
        val rows: List<JSONObject> = when (venue) {
            "binance" -> {
                val json = JSONObject(fetch(
                    "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?limit=30&universe_limit=100&min_volume=1000000&risk_pct=1"
                ))
                val a = json.optJSONArray("assets") ?: JSONArray()
                (0 until a.length()).mapNotNull { a.optJSONObject(it) }.take(3)
            }
            "bybit" -> {
                val json = JSONObject(fetch(
                    "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000"
                ))
                val a = json.optJSONArray("assets") ?: JSONArray()
                (0 until a.length()).mapNotNull { a.optJSONObject(it) }.take(3)
            }
            "okx" -> {
                val a = JSONObject(fetch(
                    "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
                )).optJSONArray("data") ?: JSONArray()
                val list = mutableListOf<JSONObject>()
                for (i in 0 until a.length()) {
                    val x = a.optJSONObject(i) ?: continue
                    val id = x.optString("instId")
                    if (!id.endsWith("-USDT")) continue
                    val price = x.optString("last").toDoubleOrNull() ?: continue
                    val turnover = x.optString("volCcy24h").toDoubleOrNull() ?: 0.0
                    val open = x.optString("open24h").toDoubleOrNull() ?: 0.0
                    list.add(JSONObject()
                        .put("id", id)
                        .put("symbol", id.replace("-", "/"))
                        .put("price", price)
                        .put("change_24h_pct", if (open > 0) (price-open)/open*100 else 0.0)
                        .put("_turnover", turnover)
                        .put("volume_24h", turnover))
                }
                list.sortedByDescending { it.optDouble("_turnover") }.take(3)
            }
            else -> coinbaseRows()
        }

        if (rows.isEmpty()) throw Exception("Нет доступных торговых пар")
        val out = StringBuilder()
        out.append(venue.uppercase(Locale.US)).append(" — ТОП-3 по обороту 24ч\n")
        for ((index, row) in rows.withIndex()) {
            val symbol = row.optString("symbol", "—")
            val price = row.optDouble("price", Double.NaN)
            val change = row.optDouble("change_24h_pct", Double.NaN)
            val turnover = row.optDouble("_turnover",
                row.optDouble("volume_24h", 0.0))
            out.append("\n").append(index + 1).append(". ").append(symbol).append("\n")
            if (!price.isNaN()) out.append(String.format(Locale.US, "Цена: $%,.8f\n", price))
            if (!change.isNaN()) out.append(String.format(Locale.US, "Изменение 24ч: %+.2f%%\n", change))
            out.append(String.format(Locale.US, "Оборот 24ч: $%,.0f\n", turnover))

            if (venue == "binance" || venue == "bybit") {
                if (row.has("score")) out.append("Оценка сканера: ")
                    .append(row.optInt("score")).append("/100 | ")
                    .append(row.optString("signal", "WATCH")).append("\n")
                if (row.has("rsi")) out.append("RSI: ").append(row.optString("rsi")).append("\n")
                if (row.has("atr_pct")) out.append("ATR: ").append(row.optString("atr_pct")).append("%\n")
            } else {
                try {
                    val candles = if (venue == "okx")
                        okxCandles(row.optString("id"))
                    else coinbaseCandles(row.optString("id"))
                    val m = metrics(candles, if (change.isNaN()) 0.0 else change, turnover)
                    if (m != null) {
                        val state = when {
                            m.score >= 70 -> "сильная"
                            m.score >= 50 -> "нейтральная"
                            else -> "слабая"
                        }
                        out.append("Техническая оценка: ").append(m.score).append("/100 (")
                            .append(state).append(")\n")
                        out.append(String.format(Locale.US, "RSI(14): %.1f\n", m.rsi))
                        out.append(String.format(Locale.US, "ATR(14): %.2f%%\n", m.atrPct))
                        out.append(String.format(Locale.US, "EMA20: $%,.8f — %s\n", m.ema, m.trend))
                    } else {
                        out.append("Техническая оценка: недостаточно свечных данных\n")
                    }
                } catch (_: Exception) {
                    out.append("Техническая оценка: свечи временно недоступны\n")
                }
            }
        }
        out.append("\nОбновлено: ").append(
            SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date())
        )
        out.append("\nОценка — аналитический индикатор, не гарантия прибыли и не приказ на сделку.")
        return out.toString()
    }

    fun createView(): View {
        val root = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        root.addView(label("₿ Крипторынок — все биржи", 21f, dark, true))
        root.addView(label(
            "Четыре списка ТОП-3 загружаются одновременно. Для Coinbase и OKX дополнительно рассчитываются RSI, ATR, EMA20 и техническая оценка.",
            13f, muted
        ))

        val venues = listOf(
            "binance" to "BINANCE",
            "bybit" to "BYBIT",
            "okx" to "OKX",
            "coinbase" to "COINBASE EXCHANGE"
        )
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
            val status = label("Загрузка $title...", 13f, dark)
            status.setPadding(0, dp(10), 0, dp(8))
            val refresh = Button(context).apply {
                text = "ОБНОВИТЬ $title"
                setTextColor(Color.WHITE)
                background = GradientDrawable().apply {
                    setColor(blue)
                    cornerRadius = dp(10).toFloat()
                }
            }
            fun load() {
                status.text = "Загрузка $title..."
                Thread {
                    try {
                        val result = loadVenue(key)
                        (context as? android.app.Activity)?.runOnUiThread {
                            status.text = result
                        }
                    } catch (e: Exception) {
                        (context as? android.app.Activity)?.runOnUiThread {
                            status.text = "$title: данные временно недоступны. " +
                                (e.message ?: "Ошибка соединения")
                        }
                    }
                }.start()
            }
            refresh.setOnClickListener { load() }
            panel.addView(heading)
            panel.addView(status)
            panel.addView(refresh)
            root.addView(panel)
            root.addView(View(context).apply {
                layoutParams = LinearLayout.LayoutParams(1, dp(12))
            })
            load()
        }
        return root
    }
}
