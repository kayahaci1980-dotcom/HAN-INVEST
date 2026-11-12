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
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class CryptoScanner(private val context: Context) {
    private val blue = Color.rgb(55, 105, 245)
    private val orange = Color.rgb(230, 135, 25)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)
    private val page = Color.rgb(246, 248, 253)

    private data class ExchangeResult(
        val exchange: String,
        val source: String,
        val scanned: Int,
        val eligible: Int,
        val btcState: String,
        val btcAvailable: Boolean,
        val assets: List<JSONObject>,
        val error: String? = null
    )

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private fun rounded(color: Int, radius: Int = 14) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(radius).toFloat()
    }

    private fun label(value: String, size: Float, color: Int, bold: Boolean = false) =
        TextView(context).apply {
            text = value
            textSize = size
            setTextColor(color)
            if (bold) setTypeface(null, Typeface.BOLD)
        }

    private fun buttonStyle(button: Button, color: Int) {
        button.setTextColor(Color.WHITE)
        button.background = rounded(color, 12)
        button.isAllCaps = false
    }

    fun createView(): View {
        val section = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        section.addView(label("₿ Криптосканер HAN INVEST", 21f, dark, true))
        section.addView(label(
            "Три лучших результата на главной. Binance и Bybit анализируются отдельно.",
            13f, muted
        ))

        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = rounded(Color.WHITE)
            elevation = dp(2).toFloat()
        }
        panel.addView(label("РАСЧЁТ РАЗМЕРА ПОЗИЦИИ", 13f, muted, true))
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
        panel.addView(capitalInput)
        panel.addView(riskInput)
        panel.addView(label(
            "По умолчанию риск 1% капитала. Только Spot, без кредитного плеча.",
            12f, muted
        ))

        val tabs = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(12), 0, dp(8))
        }
        val content = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(12), dp(12), dp(12))
            background = rounded(Color.WHITE)
        }
        val scroll = ScrollView(context).apply {
            isFillViewport = true
            addView(content)
        }
        val status = label("Загрузка независимых данных Binance и Bybit...", 14f, dark)
        val refresh = Button(context).apply {
            text = "Обновить обе биржи"
            buttonStyle(this, blue)
        }
        panel.addView(status)
        panel.addView(refresh)
        section.addView(panel)
        section.addView(tabs)
        section.addView(scroll)

        var selectedTab = "HOME"
        var capital = 100.0
        var risk = 1.0
        var binance: ExchangeResult? = null
        var bybit: ExchangeResult? = null
        var marketContext: JSONObject? = null
        var loadingCount = 0

        lateinit var render: () -> Unit

        fun addTab(title: String, key: String, color: Int) {
            val b = Button(context).apply {
                text = title
                textSize = 12f
                buttonStyle(this, if (selectedTab == key) color else muted)
                layoutParams = LinearLayout.LayoutParams(0, dp(44), 1f).apply {
                    marginEnd = dp(5)
                }
                setOnClickListener {
                    selectedTab = key
                    render()
                }
            }
            tabs.addView(b)
        }

        fun formatNumber(value: Double, digits: Int = 6): String =
            if (value.isNaN() || value.isInfinite()) "—"
            else String.format(Locale.US, "%,.${digits}f", value)

        fun tradeReady(coin: JSONObject, result: ExchangeResult, maxRisk: Double): Boolean {
            val price = coin.optDouble("price", Double.NaN)
            val ma20 = coin.optDouble("ma20", Double.NaN)
            val ma50 = coin.optDouble("ma50", Double.NaN)
            val atr = coin.optDouble("atr_14", Double.NaN)
            val momentum = coin.optDouble("momentum_6h_pct", Double.NaN)
            val volumeRatio = coin.optDouble("volume_ratio", Double.NaN)
            val relativeStrength = coin.optDouble("relative_strength_vs_btc_24h_pct", Double.NaN)
            val quoteVolume = coin.optDouble("quote_volume_24h", 0.0)
            val sizing = coin.optJSONObject("position_sizing")
            val plannedLoss = sizing?.optDouble("potential_loss_at_stop_usdt", Double.NaN) ?: Double.NaN
            val btcOk = result.btcAvailable &&
                (result.btcState == "BULLISH" || result.btcState == "SIDEWAYS")
            return result.error == null &&
                coin.optBoolean("trade_ready", false) &&
                coin.optInt("score", 0) >= 80 &&
                !price.isNaN() && !ma20.isNaN() && !ma50.isNaN() &&
                price > ma20 && ma20 > ma50 &&
                !atr.isNaN() && atr > 0.0 &&
                !momentum.isNaN() && momentum >= 1.0 &&
                !volumeRatio.isNaN() && volumeRatio >= 1.2 &&
                !relativeStrength.isNaN() && relativeStrength > 0.0 &&
                quoteVolume >= 1_000_000.0 &&
                coin.optBoolean("volume_confirmed", false) &&
                btcOk &&
                sizing != null && sizing.optBoolean("available", false) &&
                !plannedLoss.isNaN() && plannedLoss > 0.0 && plannedLoss <= maxRisk + 0.01
        }

        fun coinCard(coin: JSONObject, exchange: String, result: ExchangeResult, maxRisk: Double) {
            val symbol = coin.optString("symbol", "—")
            val shortSymbol = symbol.removeSuffix("USDT")
            val coinName = coin.optString("name").takeIf { it.isNotBlank() } ?: shortSymbol
            val price = coin.optDouble("price", Double.NaN)
            val score = coin.optInt("score", 0)
            val serverSignal = coin.optString("signal", "WATCH")
            val buyAllowed = serverSignal == "BUY" && tradeReady(coin, result, maxRisk)
            val early = coin.optString("early_signal") == "EARLY BUY"
            val card = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), dp(11), dp(12), dp(11))
                background = rounded(page, 12)
            }
            val signalText = if (buyAllowed) "BUY — условия подтверждены"
                else if (early) "EARLY BUY — только наблюдение" else "WATCH — наблюдение"
            card.addView(label("$coinName ($shortSymbol)  •  $exchange", 16f, dark, true))
            card.addView(label(
                "Оценка: $score/100   |   Сигнал: $signalText",
                13f, if (buyAllowed) Color.rgb(20, 145, 90) else muted, true
            ))
            card.addView(label(
                "Цена: ${if (price.isNaN()) "—" else formatNumber(price, 8)} USDT",
                13f, dark
            ))
            card.addView(label(
                "MA20: ${coin.optString("ma20", "—")}   |   MA50: ${coin.optString("ma50", "—")}",
                13f, dark
            ))
            card.addView(label("ATR(14) дневной (1D): ${coin.optString("atr_14_1d", coin.optString("atr_14", "—"))}", 13f, dark))
            card.addView(label("Сила против BTC за 24 ч.: ${coin.optString("relative_strength_vs_btc_24h_pct", "—")}%", 13f, dark))
            card.addView(label(
                "Объём к среднему: ${coin.optString("volume_ratio", "—")}x   |   Подтверждён: ${if (coin.optBoolean("volume_confirmed")) "ДА" else "НЕТ"}",
                13f, dark
            ))
            if (buyAllowed) {
                card.addView(label(
                    "Вход: ${coin.optString("entry_price", "—")}   |   Stop Loss: ${coin.optString("stop_loss", "—")}   |   Take Profit: ${coin.optString("take_profit", "—")}",
                    13f, dark
                ))
                val sizing = coin.optJSONObject("position_sizing")!!
                card.addView(label(
                    "Количество: ${sizing.optString("quantity", "—")}   |   Позиция: ${sizing.optString("position_value_usdt", "—")} USDT",
                    13f, dark
                ))
                card.addView(label(
                    "Риск при Stop Loss: ${sizing.optString("potential_loss_at_stop_usdt", "—")} USDT (лимит ${formatNumber(maxRisk, 2)} USDT)",
                    13f, dark, true
                ))
            } else if (serverSignal == "BUY") {
                card.addView(label(
                    "BUY заблокирован: не все проверки подтверждены или расчёт риска превышает лимит.",
                    12f, Color.rgb(190, 70, 55), true
                ))
            }
            content.addView(card)
            content.addView(View(context).apply {
                layoutParams = LinearLayout.LayoutParams(1, dp(9))
            })
        }

        render = {
            tabs.removeAllViews()
            addTab("Главная · TOP 3", "HOME", blue)
            addTab("Binance", "BINANCE", blue)
            addTab("Bybit", "BYBIT", orange)
            content.removeAllViews()
            content.addView(label(
                when (selectedTab) {
                    "BINANCE" -> "🟡 BINANCE SPOT — отдельные данные биржи"
                    "BYBIT" -> "🟠 BYBIT SPOT — отдельные данные биржи"
                    else -> "ТОП-3 ЛУЧШИХ РЕЗУЛЬТАТА"
                }, 17f, dark, true
            ))
            val b = binance
            val y = bybit
            if (b == null || y == null) {
                content.addView(label("Ожидаем ответы обеих бирж.\n${status.text}", 14f, muted))
            } else {
            val maxRisk = capital * risk / 100.0
            if (selectedTab == "HOME") {
                marketContext?.let { context ->
                    val market = context.optJSONObject("market")
                    if (market != null && market.optBoolean("available")) {
                        content.addView(label(
                            "ОБЩИЙ КРИПТОРЫНОК  •  ${market.optString("market_state_label", "—")}",
                            14f, dark, true
                        ))
                        content.addView(label(
                            "Доминирование BTC: ${market.optString("btc_dominance_pct", "—")}%  |  Изменение капитализации за 24 ч.: ${market.optString("market_cap_change_24h_pct", "—")}%",
                            12f, muted
                        ))
                        content.addView(label(
                            "Капитализация: ${formatNumber(market.optDouble("total_market_cap_usd", 0.0), 0)} USD",
                            12f, muted
                        ))
                    } else {
                        content.addView(label("Общий рынок: данные временно недоступны.", 12f, muted))
                    }
                    val news = context.optJSONObject("news")
                    if (news != null && news.optBoolean("available")) {
                        content.addView(label("ПОСЛЕДНИЕ КРИПТОНОВОСТИ", 14f, dark, true))
                        val articles = news.optJSONArray("articles") ?: JSONArray()
                        for (i in 0 until minOf(articles.length(), 5)) {
                            val article = articles.optJSONObject(i) ?: continue
                            content.addView(label(
                                "• ${article.optString("title", "")}",
                                12f, dark
                            ))
                            content.addView(label(
                                "${article.optString("source", "Источник")}  |  ${article.optString("sentiment", "Без оценки")}",
                                10f, muted
                            ))
                        }
                    } else {
                        content.addView(label("Новости: источник временно недоступен.", 12f, muted))
                    }
                }
                content.addView(label(
                    "Сравнение Binance и Bybit. В рейтинге учитывается оценка, а не порядок ответа API.",
                    12f, muted
                ))
                val combined = mutableListOf<Pair<JSONObject, ExchangeResult>>()
                if (b.error == null) b.assets.forEach { combined.add(it to b) }
                if (y.error == null) y.assets.forEach { combined.add(it to y) }
                val top = combined.sortedWith(
                    compareByDescending<Pair<JSONObject, ExchangeResult>> { it.first.optInt("score", 0) }
                        .thenByDescending { it.first.optDouble("volume_ratio", 0.0) }
                ).take(3)
                if (top.isEmpty()) content.addView(label("Нет доступных результатов. Проверьте соединение.", 14f, muted))
                top.forEach { (coin, exchange) -> coinCard(coin, exchange.exchange, exchange, maxRisk) }
                content.addView(label(
                    "Binance: ${b.assets.size} кандидатов • Bybit: ${y.assets.size} кандидатов",
                    12f, muted
                ))
            } else {
                val result = if (selectedTab == "BINANCE") b else y
                content.addView(label(
                    "Источник: ${result.source}\nПроверено пар: ${result.scanned} • Подходящих Spot-пар: ${result.eligible}\nBTC market state: ${result.btcState}",
                    12f, muted
                ))
                if (result.error != null) {
                    content.addView(label("Ошибка загрузки: ${result.error}", 14f, Color.RED))
                } else if (result.assets.isEmpty()) {
                    content.addView(label("Кандидаты не найдены.", 14f, muted))
                } else {
                    result.assets.sortedByDescending { it.optInt("score", 0) }
                        .forEach { coinCard(it, result.exchange, result, maxRisk) }
                }
            }
            }
        }

        fun fetchExchange(exchange: String, address: String, done: (ExchangeResult) -> Unit) {
            Thread {
                val result = try {
                    val connection = URL(address).openConnection() as HttpURLConnection
                    val raw = try {
                        connection.requestMethod = "GET"
                        connection.connectTimeout = 20000
                        connection.readTimeout = 30000
                        connection.setRequestProperty("Accept", "application/json")
                        connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.5")
                        if (connection.responseCode !in 200..299) throw Exception("HTTP ${connection.responseCode}")
                        connection.inputStream.bufferedReader().use { it.readText() }
                    } finally {
                        connection.disconnect()
                    }
                    val json = JSONObject(raw)
                    val array = json.optJSONArray("assets") ?: JSONArray()
                    val list = (0 until array.length()).map { array.getJSONObject(it) }
                    val btc = json.optJSONObject("btc_market_filter")
                    val source = json.optString("source",
                        if (exchange == "Binance") "Binance public market data" else "Bybit public market data")
                    ExchangeResult(
                        exchange, source, json.optInt("scanned", list.size),
                        json.optInt("eligible_pairs", 0),
                        btc?.optString("market_state", "UNKNOWN") ?: "UNKNOWN",
                        btc?.optBoolean("available", false) ?: false,
                        list
                    )
                } catch (e: Exception) {
                    ExchangeResult(exchange, "Недоступен", 0, 0, "UNKNOWN", false, emptyList(),
                        e.message ?: "Ошибка сети")
                }
                Handler(Looper.getMainLooper()).post { done(result) }
            }.start()
        }

        fun fetchMarketContext() {
            Thread {
                val result = try {
                    val connection = URL("https://han-invest-api-test.onrender.com/api/v1/crypto/market-context").openConnection() as HttpURLConnection
                    try {
                        connection.requestMethod = "GET"
                        connection.connectTimeout = 15000
                        connection.readTimeout = 20000
                        connection.setRequestProperty("Accept", "application/json")
                        if (connection.responseCode !in 200..299) throw Exception("HTTP ${connection.responseCode}")
                        JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
                    } finally {
                        connection.disconnect()
                    }
                } catch (_: Exception) {
                    null
                }
                Handler(Looper.getMainLooper()).post {
                    marketContext = result
                    render()
                }
            }.start()
        }

        fun loadAll() {
            capital = capitalInput.text.toString().trim().toDoubleOrNull() ?: 0.0
            risk = riskInput.text.toString().trim().toDoubleOrNull() ?: 0.0
            if (capital <= 0.0) {
                capitalInput.error = "Введите капитал больше нуля"
                return
            }
            if (risk !in 0.1..5.0) {
                riskInput.error = "Допустимый риск: от 0,1% до 5%"
                return
            }
            binance = null
            bybit = null
            marketContext = null
            fetchMarketContext()
            loadingCount = 2
            status.text = "Загружаем Binance и Bybit независимо..."
            render()
            val query = "limit=30&universe_limit=100&min_volume=1000000&capital_usdt=$capital&risk_pct=$risk"
            fetchExchange("Binance",
                "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?$query") {
                binance = it
                loadingCount--
                status.text = if (loadingCount == 0) "Данные обеих бирж обновлены."
                    else "Binance получен. Ожидаем Bybit..."
                render()
            }
            fetchExchange("Bybit",
                "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000&capital_usdt=$capital&risk_pct=$risk") {
                bybit = it
                loadingCount--
                status.text = if (loadingCount == 0) "Данные обеих бирж обновлены."
                    else "Bybit получен. Ожидаем Binance..."
                render()
            }
        }

        refresh.setOnClickListener { loadAll() }
        section.addView(label(
            "Автообновление каждые 15 минут при открытом экране.",
            11f, muted
        ))
        val handler = Handler(Looper.getMainLooper())
        val task = object : Runnable {
            override fun run() {
                if (!section.isAttachedToWindow) return
                loadAll()
                handler.postDelayed(this, 15L * 60L * 1000L)
            }
        }
        section.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(view: View) {
                handler.removeCallbacks(task)
                handler.postDelayed(task, 15L * 60L * 1000L)
            }
            override fun onViewDetachedFromWindow(view: View) {
                handler.removeCallbacks(task)
            }
        })
        loadAll()
        return section
    }
}
