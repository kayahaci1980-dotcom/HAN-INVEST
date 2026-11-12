package com.haninvest.app

import android.content.Context
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
    private val orange = Color.rgb(245, 135, 35)
    private val green = Color.rgb(25, 160, 105)
    private val red = Color.rgb(205, 65, 65)
    private val pale = Color.rgb(241, 245, 252)

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private fun rounded(color: Int) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(13).toFloat()
    }

    private fun text(value: String, size: Float, color: Int, bold: Boolean = false) =
        TextView(context).apply {
            text = value
            textSize = size
            setTextColor(color)
            if (bold) setTypeface(null, Typeface.BOLD)
        }

    private fun button(title: String, color: Int, onClick: () -> Unit) =
        Button(context).apply {
            text = title
            textSize = 13f
            isAllCaps = false
            setTextColor(Color.WHITE)
            background = rounded(color)
            setOnClickListener { onClick() }
        }

    fun createView(): View {
        val section = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        section.addView(text("₿ Криптосканер HAN INVEST", 21f, dark, true))
        section.addView(text(
            "Выбери биржу. Списки и показатели каждой площадки рассчитываются отдельно.",
            13f, muted
        ))

        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(14), dp(14), dp(14))
            background = rounded(Color.WHITE)
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
            hint = "Риск на сделку, %"
            setText("1")
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            textSize = 14f
            setSingleLine(true)
        }
        panel.addView(text("РАЗМЕР ПОЗИЦИИ", 12f, muted, true))
        panel.addView(capitalInput)
        panel.addView(riskInput)
        panel.addView(text(
            "По умолчанию риск 1% капитала. Только Spot, без плеча.",
            12f, muted
        ))

        val tabs = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        val bybitTab = button("🟠 BYBIT", orange) {}
        val binanceTab = button("🟡 BINANCE", Color.rgb(190, 145, 0)) {}
        val tabParams = LinearLayout.LayoutParams(0, dp(46), 1f).apply {
            marginEnd = dp(5)
        }
        tabs.addView(bybitTab, tabParams)
        tabs.addView(binanceTab, LinearLayout.LayoutParams(0, dp(46), 1f))
        panel.addView(tabs)

        val status = text("Загрузка Binance Spot...", 14f, dark)
        status.setPadding(0, dp(12), 0, dp(8))
        panel.addView(status)

        val showAll = button("Показать все монеты", blue) {}
        showAll.visibility = View.GONE
        panel.addView(showAll)

        val refresh = button("ОБНОВИТЬ ВЫБРАННУЮ БИРЖУ", blue) {}
        panel.addView(refresh)
        section.addView(panel)
        section.addView(text(
            "🟡 Binance и 🟠 Bybit — независимые источники. Нельзя переносить цену или индикаторы с одной биржи на другую.",
            11f, muted
        ))

        var selectedExchange = "binance"
        var expanded = false
        var cachedText = ""
        var currentJson: JSONObject? = null

        fun render(json: JSONObject, exchange: String) {
            currentJson = json
            val assets = json.optJSONArray("assets")
            if (assets == null || assets.length() == 0) {
                status.text = "Биржа не вернула подходящих спотовых пар. Попробуй обновить позже."
                showAll.visibility = View.GONE
                return
            }
            val source = if (exchange == "bybit") "🟠 BYBIT SPOT" else "🟡 BINANCE SPOT"
            val out = StringBuilder()
            out.append(source).append("\n")
            out.append("Проверено монет: ").append(json.optInt("scanned", assets.length())).append("\n")
            out.append("Торговых пар Spot USDT: ").append(json.optInt("eligible_pairs", 0)).append("\n")

            val btc = json.optJSONObject("btc_market_filter")
            val btcState = btc?.optString("market_state", "UNKNOWN") ?: "UNKNOWN"
            out.append("Фильтр BTC: ").append(btcState).append("\n")
            out.append("BUY разрешён рынком: ")
                .append(if (btc?.optBoolean("buy_allowed", false) == true) "ДА" else "НЕТ")
                .append("\n\n")

            val count = if (expanded) assets.length() else minOf(3, assets.length())
            for (i in 0 until count) {
                val coin = assets.optJSONObject(i) ?: continue
                val symbol = coin.optString("symbol", "—")
                val name = coin.optString("name", symbol)
                val price = coin.optDouble("price", Double.NaN)
                val change = coin.optDouble("change_24h_pct", Double.NaN)
                val score = coin.optInt("score", 0)
                val signal = coin.optString("signal", "WATCH")
                val ma20 = coin.optString("ma20", "—").takeUnless { it == "null" || it.isBlank() } ?: "—"
                val ma50 = coin.optString("ma50", "—").takeUnless { it == "null" || it.isBlank() } ?: "—"
                val atr = coin.optString("atr_14", "—").takeUnless { it == "null" || it.isBlank() } ?: "—"
                val volume = coin.optDouble("volume_ratio", Double.NaN)
                val signalColor = if (signal == "BUY") green else if (signal == "WATCH") orange else muted

                out.append("№ ").append(coin.optInt("market_rank", i + 1))
                    .append(". ").append(name).append(" (").append(symbol).append(")\n")
                if (!price.isNaN()) out.append(String.format(Locale.US, "Цена: $%.8f USD\n", price))
                if (!change.isNaN()) out.append(String.format(Locale.US, "Изменение 24ч: %.2f%%\n", change))
                out.append("Оценка: ").append(score).append("/100 | Сигнал: ").append(signal).append("\n")
                out.append("MA20: ").append(ma20).append(" | MA50: ").append(ma50).append("\n")
                out.append("Объём: ")
                out.append(if (volume.isNaN()) "нет данных" else String.format(Locale.US, "%.2fx", volume))
                out.append(" | Подтверждён: ").append(if (coin.optBoolean("volume_confirmed")) "ДА" else "НЕТ").append("\n")
                out.append("ATR(14): ").append(atr).append("\n")
                out.append("Пара Spot: ").append(if (coin.optBoolean("trade_ready")) "ПОДТВЕРЖДЕНА" else "НЕ ПОДТВЕРЖДЕНА").append("\n")

                val failures = mutableListOf<String>()
                val ma20Value = coin.optDouble("ma20", Double.NaN)
                val ma50Value = coin.optDouble("ma50", Double.NaN)
                val currentPrice = coin.optDouble("price", Double.NaN)
                if (ma20Value.isNaN() || ma50Value.isNaN() || currentPrice.isNaN() ||
                    !(currentPrice > ma20Value && ma20Value > ma50Value)) {
                    failures.add("цена должна быть выше MA20, а MA20 выше MA50")
                }
                if (!coin.optBoolean("volume_confirmed")) failures.add("объём ниже 1.2x")
                if (!coin.optBoolean("trade_ready")) failures.add("спотовая пара не подтверждена")
                if (btcState == "BEARISH" || btcState == "UNKNOWN") failures.add("фильтр BTC не разрешает BUY")
                if (signal != "BUY") {
                    out.append("ПОЧЕМУ НЕТ BUY: ")
                    out.append(if (failures.isEmpty()) "все условия BUY не подтверждены" else failures.joinToString("; "))
                    out.append("\n")
                }
                val early = coin.optString("early_signal", "")
                if (early == "EARLY BUY") out.append("EARLY BUY — раннее предупреждение, не подтверждённая покупка\n")
                if (signal == "BUY" && coin.optBoolean("trade_ready")) {
                    out.append("Условия сканера выполнены. Проверь ликвидность и риск перед сделкой.\n")
                }
                out.append("\n")
            }
            if (!expanded && assets.length() > 3) {
                out.append("Показаны 3 лучших кандидата из ").append(assets.length()).append(".\n")
            }
            out.append("\nВремя обновления: ").append(json.optString("updated_at", "не указано"))
            status.text = out.toString()
            showAll.visibility = if (assets.length() > 3) View.VISIBLE else View.GONE
            showAll.text = if (expanded) "Свернуть до топ-3" else "Показать все монеты (" + assets.length() + ")"
        }

        fun load(exchange: String) {
            selectedExchange = exchange
            expanded = false
            status.text = if (exchange == "bybit") "Загрузка Bybit Spot..." else "Загрузка Binance Spot..."
            showAll.visibility = View.GONE
            val capital = capitalInput.text.toString().trim().toDoubleOrNull()
            val risk = riskInput.text.toString().trim().toDoubleOrNull() ?: 1.0
            if (capitalInput.text.isNotBlank() && (capital == null || capital <= 0.0)) {
                capitalInput.error = "Введите капитал больше нуля"
                return
            }
            if (risk !in 0.1..5.0) {
                riskInput.error = "Допустимый риск: 0,1–5%"
                return
            }
            Thread {
                try {
                    val url = if (exchange == "bybit") {
                        "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000"
                    } else {
                        val query = "limit=30&universe_limit=100&min_volume=1000000&risk_pct=" + risk +
                            (if (capital != null) "&capital_usdt=" + capital else "")
                        "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?" + query
                    }
                    val connection = URL(url).openConnection() as HttpURLConnection
                    val response = try {
                        connection.requestMethod = "GET"
                        connection.connectTimeout = 20000
                        connection.readTimeout = 30000
                        connection.setRequestProperty("Accept", "application/json")
                        connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.5")
                        if (connection.responseCode !in 200..299) throw Exception("HTTP " + connection.responseCode)
                        connection.inputStream.bufferedReader().use { it.readText() }
                    } finally {
                        connection.disconnect()
                    }
                    val json = JSONObject(response)
                    (context as? android.app.Activity)?.runOnUiThread {
                        if (selectedExchange == exchange) render(json, exchange)
                    }
                } catch (error: Exception) {
                    (context as? android.app.Activity)?.runOnUiThread {
                        if (selectedExchange == exchange) {
                            status.text = (if (exchange == "bybit") "Bybit" else "Binance") +
                                " временно недоступен. Данные другой биржи не подставляются. Нажми «Обновить» позже."
                        }
                    }
                }
            }.start()
        }

        bybitTab.setOnClickListener { load("bybit") }
        binanceTab.setOnClickListener { load("binance") }
        refresh.setOnClickListener { load(selectedExchange) }
        showAll.setOnClickListener {
            expanded = !expanded
            val json = currentJson
            if (json != null) render(json, selectedExchange)
        }

        load("binance")
        return section
    }
}
