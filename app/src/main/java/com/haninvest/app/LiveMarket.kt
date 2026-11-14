package com.haninvest.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class LiveMarket(private val context: Context) {
    private val blue = Color.rgb(55, 105, 245)
    private val green = Color.rgb(20, 160, 105)
    private val red = Color.rgb(220, 65, 75)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)
    private val handler = Handler(Looper.getMainLooper())
    private var filter = "ALL"
    private var rows = mutableListOf<Row>()
    private lateinit var list: LinearLayout
    private lateinit var updated: TextView
    private val buttons = mutableMapOf<String, Button>()
    private var running = false

    private data class Row(val exchange: String, val symbol: String, val price: Double, val change: Double)

    private fun dp(v: Int) = (v * context.resources.displayMetrics.density).toInt()
    private fun request(url: String): String {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 12000
            c.readTimeout = 15000
            c.setRequestProperty("Accept", "application/json")
            c.setRequestProperty("User-Agent", "HAN-INVEST-Live/1.0")
            if (c.responseCode !in 200..299) throw IllegalStateException("HTTP ${c.responseCode}")
            return c.inputStream.bufferedReader().use { it.readText() }
        } finally { c.disconnect() }
    }
    private fun loadAll(): List<Row> {
        val result = mutableListOf<Row>()
        try {
            val a = JSONArray(request("https://api.binance.com/api/v3/ticker/24hr"))
            for (i in 0 until a.length()) {
                val x = a.getJSONObject(i); val s = x.optString("symbol")
                if (s.endsWith("USDT")) result.add(Row("Binance", s, x.optDouble("lastPrice"), x.optDouble("priceChangePercent")))
            }
        } catch (_: Exception) {}
        try {
            val a = JSONObject(request("https://api.bybit.com/v5/market/tickers?category=spot")).optJSONObject("result")?.optJSONArray("list")
            if (a != null) for (i in 0 until a.length()) {
                val x = a.getJSONObject(i); val s = x.optString("symbol")
                if (s.endsWith("USDT")) result.add(Row("Bybit", s, x.optDouble("lastPrice"), x.optDouble("price24hPcnt") * 100.0))
            }
        } catch (_: Exception) {}
        try {
            val a = JSONObject(request("https://www.okx.com/api/v5/market/tickers?instType=SPOT")).optJSONArray("data")
            if (a != null) for (i in 0 until a.length()) {
                val x = a.getJSONObject(i); val s = x.optString("instId")
                if (s.endsWith("-USDT")) result.add(Row("OKX", s.replace("-", ""), x.optDouble("last"), x.optDouble("sodUtc8") .let { open -> if (open > 0) (x.optDouble("last") / open - 1) * 100 else 0.0 }))
            }
        } catch (_: Exception) {}
        // Coinbase Exchange public ticker endpoint: retrieve the most actively traded USD/USDT products.
        try {
            val products = JSONArray(request("https://api.exchange.coinbase.com/products"))
            val candidates = mutableListOf<String>()
            for (i in 0 until products.length()) {
                val p = products.getJSONObject(i)
                val quote = p.optString("quote_currency")
                if ((quote == "USD" || quote == "USDT") && p.optString("status") == "online") candidates.add(p.optString("id"))
            }
            for (symbol in candidates.take(30)) {
                try {
                    val t = JSONObject(request("https://api.exchange.coinbase.com/products/$symbol/ticker"))
                    val price = t.optDouble("price")
                    val open = t.optDouble("open")
                    if (price > 0 && open > 0) result.add(Row("Coinbase", symbol, price, (price / open - 1) * 100))
                } catch (_: Exception) {}
            }
        } catch (_: Exception) {}
        return result
    }

    private fun render() {
        list.removeAllViews()
        val shown = rows.filter { filter == "ALL" || (filter == "UP" && it.change > 0) || (filter == "DOWN" && it.change < 0) }
            .sortedByDescending { if (filter == "DOWN") -it.change else it.change }.take(100)
        val counts = "Всего: ${rows.size}   Рост: ${rows.count { it.change > 0 }}   Падение: ${rows.count { it.change < 0 }}"
        updated.text = counts + "\nОбновление автоматически каждые 15 секунд"
        if (shown.isEmpty()) {
            list.addView(TextView(context).apply { text = "Загрузка котировок или данные временно недоступны."; setTextColor(muted); textSize = 14f })
        }
        for (r in shown) {
            val line = LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(10), dp(9), dp(10), dp(9))
                background = GradientDrawable().apply { setColor(Color.WHITE); cornerRadius = dp(10).toFloat() }
            }
            val left = TextView(context).apply {
                text = "${r.symbol}\n${r.exchange}"
                textSize = 13f; setTextColor(dark); setTypeface(null, Typeface.BOLD)
            }
            val right = TextView(context).apply {
                text = String.format(Locale.US, "$%,.8f\n%+.2f%%", r.price, r.change)
                textSize = 13f; setTextColor(if (r.change >= 0) green else red); gravity = Gravity.RIGHT
            }
            line.addView(left, LinearLayout.LayoutParams(0, -2, 1f))
            line.addView(right)
            list.addView(line)
            val gap = View(context); list.addView(gap, LinearLayout.LayoutParams(1, dp(5)))
        }
    }

    private fun select(key: String) {
        filter = key
        for ((k, b) in buttons) {
            val active = k == key
            b.background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(if (!active) Color.WHITE else when (k) { "UP" -> green; "DOWN" -> red; else -> blue })
                setStroke(dp(1), if (active) Color.TRANSPARENT else 0xFFDCE2EF.toInt())
            }
            b.setTextColor(if (active) Color.WHITE else dark)
        }
        render()
    }

    private fun refresh() {
        Thread {
            val fresh = loadAll()
            handler.post {
                if (fresh.isNotEmpty()) rows = fresh.toMutableList()
                render()
            }
        }.start()
    }

    private val ticker = object : Runnable {
        override fun run() {
            if (!running) return
            refresh()
            handler.postDelayed(this, 15000)
        }
    }

    fun createView(): View {
        val root = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
        root.addView(TextView(context).apply {
            text = "РЫНОК LIVE"
            textSize = 20f; setTextColor(dark); setTypeface(null, Typeface.BOLD)
        })
        root.addView(TextView(context).apply {
            text = "Движение криптовалют на Binance, Bybit, OKX и Coinbase"
            textSize = 13f; setTextColor(muted)
        })
        val filters = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(0, dp(10), 0, dp(10))
        }
        val specs = listOf("ALL" to "Все", "UP" to "Рост", "DOWN" to "Падение")
        for ((key, title) in specs) {
            val b = Button(context).apply {
                text = title; isAllCaps = false; textSize = 12f
                setPadding(0, 0, 0, 0)
                layoutParams = LinearLayout.LayoutParams(dp(68), dp(68)).apply {
                    marginStart = dp(8); marginEnd = dp(8)
                }
                setOnClickListener { select(key) }
            }
            buttons[key] = b
            filters.addView(b)
        }
        root.addView(filters)
        updated = TextView(context).apply { textSize = 12f; setTextColor(muted); setPadding(0, 0, 0, dp(8)) }
        root.addView(updated)
        list = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
        root.addView(list)
        select("ALL")
        running = true
        handler.post(ticker)
        return root
    }
}
