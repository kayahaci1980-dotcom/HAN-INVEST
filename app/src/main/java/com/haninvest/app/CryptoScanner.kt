package com.haninvest.app

import android.content.Context
import android.app.Dialog
import android.view.ViewGroup
import android.view.WindowManager
import android.os.Handler
import android.os.Looper
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.view.Gravity
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
    // Modern HAN INVEST palette — visual-only; trading calculations and rules are unchanged.
    private val blue = Color.rgb(0, 174, 255)
    private val orange = Color.rgb(255, 151, 35)
    private val purple = Color.rgb(171, 91, 255)
    private val dark = Color.rgb(232, 240, 250)
    private val muted = Color.rgb(151, 169, 190)
    private val page = Color.rgb(9, 22, 39)
    private val surface = Color.rgb(5, 13, 26)
    private val card = Color.rgb(18, 38, 62)
    private val border = Color.rgb(0, 153, 230)

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

    private fun formatNumber(value: Double, digits: Int = 6): String =
        if (value.isNaN() || value.isInfinite()) "—"
        else String.format(Locale.US, "%,.${digits}f", value)

    private fun formatBishkekTime(value: String?): String {
        if (value.isNullOrBlank() || value == "null") return "—"
        val patterns = listOf(
            "yyyy-MM-dd'T'HH:mm:ss.SSSXXX",
            "yyyy-MM-dd'T'HH:mm:ssXXX",
            "yyyy-MM-dd HH:mm:ss"
        )
        for (pattern in patterns) {
            try {
                val parser = java.text.SimpleDateFormat(pattern, Locale.US)
                parser.timeZone = java.util.TimeZone.getTimeZone("UTC")
                val date = parser.parse(value) ?: continue
                val formatter = java.text.SimpleDateFormat("dd.MM.yyyy HH:mm", Locale("ru", "RU"))
                formatter.timeZone = java.util.TimeZone.getTimeZone("Asia/Bishkek")
                return formatter.format(date)
            } catch (_: Exception) { }
        }
        return value
    }

    private fun rounded(color: Int, radius: Int = 14) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(radius).toFloat()
        setStroke(dp(1), border)
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
            setPadding(dp(10), dp(10), dp(10), dp(10))
            background = rounded(surface, 18)
        }
        section.addView(label("₿ Криптосканер HAN INVEST", 21f, dark, true))
        section.addView(label(
            "Четыре биржи анализируются отдельно: Binance, Bybit, Coinbase Exchange и OKX.",
            13f, muted
        ))

        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = rounded(card)
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
            background = rounded(card)
        }
        val scroll = ScrollView(context).apply {
            isFillViewport = true
            addView(content)
        }
        var selectedTab = "HOME"
        lateinit var render: () -> Unit
        val floatingDialog = Dialog(context)
        val floatingRoot = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(12))
            background = rounded(surface, 22)
        }
        val floatingHeader = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        floatingHeader.addView(label("HAN INVEST  •  РАЗДЕЛЫ", 15f, blue, true),
            LinearLayout.LayoutParams(0, dp(42), 1f))
        val closeFloating = Button(context).apply {
            text = "✕"
            textSize = 17f
            buttonStyle(this, Color.rgb(220, 48, 72))
            setOnClickListener { floatingDialog.dismiss() }
        }
        floatingHeader.addView(closeFloating, LinearLayout.LayoutParams(dp(46), dp(42)))
        floatingRoot.addView(floatingHeader)
        floatingRoot.addView(tabs)
        floatingRoot.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        floatingDialog.setContentView(floatingRoot)
        floatingDialog.setOnShowListener {
            floatingDialog.window?.apply {
                setBackgroundDrawableResource(android.R.color.transparent)
                setLayout(WindowManager.LayoutParams.MATCH_PARENT,
                    (context.resources.displayMetrics.heightPixels * 0.86f).toInt())
                addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND)
                attributes = attributes.apply { dimAmount = 0.62f }
            }
        }
        val openSections = Button(context).apply {
            text = "ОТКРЫТЬ РАЗДЕЛЫ"
            textSize = 14f
            buttonStyle(this, blue)
            setOnClickListener {
                selectedTab = "HOME"
                render()
                if (!floatingDialog.isShowing) floatingDialog.show()
            }
        }
        val status = label("Загрузка данных четырёх криптобирж...", 14f, dark)
        val refresh = Button(context).apply {
            text = "Обновить все биржи"
            buttonStyle(this, blue)
        }
        panel.addView(status)
        panel.addView(refresh)
        section.addView(panel)
        section.addView(openSections)

        val paperPrefs = context.getSharedPreferences("han_invest_paper", Context.MODE_PRIVATE)
        var paperCash = paperPrefs.getFloat("cash_usdt", 1000f).toDouble()
        var paperPositions = try { JSONObject(paperPrefs.getString("positions", "{}") ?: "{}") } catch (_: Exception) { JSONObject() }
        var paperHistory = try { JSONArray(paperPrefs.getString("history", "[]") ?: "[]") } catch (_: Exception) { JSONArray() }
        var serverPaperState: JSONObject? = null
        var serverPaperError: String? = null
        var serverPaperLoading = false
        lateinit var refreshPaperServer: () -> Unit
        lateinit var togglePaperServer: (Boolean) -> Unit
        fun savePaper() {
            paperPrefs.edit().putFloat("cash_usdt", paperCash.toFloat())
                .putString("positions", paperPositions.toString())
                .putString("history", paperHistory.toString()).apply()
        }
        fun paperTrade(exchange: String, symbol: String, price: Double, buyAmount: Double?, sellAll: Boolean) {
            if (!price.isFinite() || price <= 0.0) return
            val key = exchange + ":" + symbol
            val old = paperPositions.optJSONObject(key) ?: JSONObject()
            val oldQty = old.optDouble("quantity", 0.0)
            val oldCost = old.optDouble("cost_usdt", 0.0)
            val feeRate = 0.001
            if (buyAmount != null) {
                val amount = buyAmount
                val fee = amount * feeRate
                if (amount < 1.0 || amount + fee > paperCash) {
                    status.text = "Недостаточно виртуальных USDT или сумма меньше 1 USDT."
                    return
                }
                val qty = amount / price
                paperCash -= amount + fee
                paperPositions.put(key, JSONObject().put("exchange", exchange).put("symbol", symbol)
                    .put("quantity", oldQty + qty).put("cost_usdt", oldCost + amount + fee))
                paperHistory.put(JSONObject().put("side", "BUY").put("exchange", exchange).put("symbol", symbol)
                    .put("price", price).put("quantity", qty).put("value_usdt", amount).put("fee_usdt", fee)
                    .put("time", System.currentTimeMillis()))
                status.text = "Виртуальная покупка " + symbol + " на " + formatNumber(amount, 2) + " USDT."
            } else if (sellAll) {
                if (oldQty <= 0.0) {
                    status.text = "Нет открытой позиции " + symbol + "."
                    return
                }
                val gross = oldQty * price
                val fee = gross * feeRate
                val net = gross - fee
                val pnl = net - oldCost
                paperCash += net
                paperPositions.remove(key)
                paperHistory.put(JSONObject().put("side", "SELL").put("exchange", exchange).put("symbol", symbol)
                    .put("price", price).put("quantity", oldQty).put("value_usdt", gross).put("fee_usdt", fee)
                    .put("pnl_usdt", pnl).put("time", System.currentTimeMillis()))
                status.text = "Продажа " + symbol + ". Результат: " + formatNumber(pnl, 2) + " USDT."
            }
            savePaper()
            render()
        }

        var capital = 100.0
        var risk = 1.0
        var binance: ExchangeResult? = null
        var bybit: ExchangeResult? = null
        var coinbase: ExchangeResult? = null
        var okx: ExchangeResult? = null
        var marketContext: JSONObject? = null
        var globalAssets: List<JSONObject>? = null
        var globalMarketError: String? = null
        var globalPage = 0
        val globalPageSize = 50
        var loadingCount = 0

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
                    if (key == "PAPER") refreshPaperServer()
                    if (!floatingDialog.isShowing) floatingDialog.show()
                }
            }
            tabs.addView(b)
        }

        fun tradeReady(coin: JSONObject, result: ExchangeResult, maxRisk: Double): Boolean {
            val price = coin.optDouble("price", Double.NaN)
            val atr = coin.optDouble("atr_14", Double.NaN)
            val quoteVolume = coin.optDouble("quote_volume_24h", 0.0)
            return result.error == null &&
                coin.optBoolean("trade_ready", false) &&
                coin.optInt("score", 0) >= 83 &&
                price.isFinite() && price > 0.0 &&
                atr.isFinite() && atr > 0.0 &&
                quoteVolume >= 1_000_000.0
        }

        fun coinCard(coin: JSONObject, exchange: String, result: ExchangeResult, maxRisk: Double, allowTrading: Boolean = true) {
            val symbol = coin.optString("symbol", "—")
            val shortSymbol = symbol.removeSuffix("-USDT").removeSuffix("USDT")
            val coinName = coin.optString("name").takeIf { it.isNotBlank() } ?: shortSymbol
            val price = coin.optDouble("price", Double.NaN)
            val score = coin.optInt("score", 0)
            val serverSignal = coin.optString("signal", "WATCH")
            val buyAllowed = allowTrading && score >= 83 && tradeReady(coin, result, maxRisk)
            val early = coin.optString("early_signal") == "EARLY BUY"
            val card = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), dp(11), dp(12), dp(11))
                background = rounded(page, 12)
            }
            val signalText = if (buyAllowed) "АВТОПОКУПКА: оценка 83+"
                else if (early) "EARLY BUY — наблюдение" else "WATCH — наблюдение"
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
                val sizing = coin.optJSONObject("position_sizing")
                if (sizing != null) {
                    card.addView(label(
                        "Количество: ${sizing.optString("quantity", "—")}   |   Позиция: ${sizing.optString("position_value_usdt", "—")} USDT",
                        13f, dark
                    ))
                    card.addView(label(
                        "Риск при Stop Loss: ${sizing.optString("potential_loss_at_stop_usdt", "—")} USDT (лимит ${formatNumber(maxRisk, 2)} USDT)",
                        13f, dark, true
                    ))
                } else {
                    card.addView(label(
                        "Расчёт размера позиции временно недоступен. Обновите данные.",
                        12f, orange, true
                    ))
                }
            } else if (serverSignal == "BUY") {
                card.addView(label(
                    "BUY заблокирован: не все проверки подтверждены или расчёт риска превышает лимит.",
                    12f, Color.rgb(190, 70, 55), true
                ))
            }
            if (allowTrading) {
                val tradeRow = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                val amountInput = EditText(context).apply {
                    hint = "Сумма USDT"
                    setText("100")
                    textSize = 13f
                    inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
                    layoutParams = LinearLayout.LayoutParams(0, dp(48), 1f)
                }
                val buyButton = Button(context).apply {
                    text = "Купить"
                    buttonStyle(this, Color.rgb(20, 145, 90))
                }
                buyButton.setOnClickListener {
                    val amount = amountInput.text.toString().replace(",", ".").toDoubleOrNull()
                    if (amount == null || amount <= 0.0) status.text = "Введите сумму в USDT."
                    else paperTrade(exchange, symbol, price, amount, false)
                }
                tradeRow.addView(amountInput)
                tradeRow.addView(buyButton)
                card.addView(tradeRow)
                val ownedQty = paperPositions.optJSONObject(exchange + ":" + symbol)?.optDouble("quantity", 0.0) ?: 0.0
                if (ownedQty > 0.0) {
                    val sellButton = Button(context).apply {
                        text = "Продать всю позицию"
                        buttonStyle(this, orange)
                    }
                    sellButton.setOnClickListener { paperTrade(exchange, symbol, price, null, true) }
                    card.addView(sellButton)
                }
            }
            content.addView(card)
            content.addView(View(context).apply {
                layoutParams = LinearLayout.LayoutParams(1, dp(9))
            })
        }

        fun loadGlobalMarket() {
            globalAssets = null
            globalMarketError = null
            render()
            Thread {
                var loaded: List<JSONObject>? = null
                var error: String? = null
                try {
                    val connection = URL("https://han-invest-api-test.onrender.com/api/v1/crypto/market-assets?limit=2000")
                        .openConnection() as HttpURLConnection
                    try {
                        connection.requestMethod = "GET"
                        connection.connectTimeout = 15000
                        connection.readTimeout = 30000
                        connection.setRequestProperty("Accept", "application/json")
                        if (connection.responseCode !in 200..299) throw Exception("HTTP ${connection.responseCode}")
                        val json = JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
                        val array = json.optJSONArray("assets") ?: JSONArray()
                        loaded = (0 until array.length()).mapNotNull { array.optJSONObject(it) }
                    } finally { connection.disconnect() }
                } catch (e: Exception) { error = e.message ?: "Ошибка загрузки" }
                Handler(Looper.getMainLooper()).post {
                    globalAssets = loaded
                    globalMarketError = error
                    globalPage = 0
                    render()
                }
            }.start()
        }

        render = {
            tabs.removeAllViews()
            addTab("Главная · TOP 3", "HOME", blue)
            addTab("Рынок LIVE", "MARKET", purple)
            addTab("Binance", "BINANCE", blue)
            addTab("Bybit", "BYBIT", orange)
            addTab("Coinbase", "COINBASE", blue)
            addTab("OKX", "OKX", purple)
            addTab("Вирт. счёт", "PAPER", Color.rgb(20, 145, 90))
            content.removeAllViews()
            content.addView(label(
                when (selectedTab) {
                    "BINANCE" -> "🟡 BINANCE SPOT — отдельные данные биржи"
                    "BYBIT" -> "🟠 BYBIT SPOT — отдельные данные биржи"
                    "COINBASE" -> "🔵 COINBASE EXCHANGE — отдельные данные биржи"
                    "OKX" -> "⚫ OKX SPOT — отдельные данные биржи"
                    "PAPER" -> "ВИРТУАЛЬНЫЙ СЧЁТ — PAPER TRADING"
                    else -> "ТОП-3 ЛУЧШИХ РЕЗУЛЬТАТА"
                }, 17f, dark, true
            ))
            if (selectedTab == "HOME") {
                val totalPnl = (0 until paperHistory.length()).sumOf { i ->
                    paperHistory.optJSONObject(i)?.optDouble("pnl_usdt", 0.0) ?: 0.0
                }
                val dayStart = java.util.Calendar.getInstance().apply {
                    set(java.util.Calendar.HOUR_OF_DAY, 0); set(java.util.Calendar.MINUTE, 0)
                    set(java.util.Calendar.SECOND, 0); set(java.util.Calendar.MILLISECOND, 0)
                }.timeInMillis
                val todayPnl = (0 until paperHistory.length()).sumOf { i ->
                    val trade = paperHistory.optJSONObject(i) ?: return@sumOf 0.0
                    if (trade.optLong("time", 0L) >= dayStart) trade.optDouble("pnl_usdt", 0.0) else 0.0
                }
                val stats = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                fun stat(title: String, value: String, color: Int) = LinearLayout(context).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(dp(9), dp(10), dp(9), dp(10))
                    background = rounded(page, 12)
                    addView(label(title, 11f, muted, true))
                    addView(label(value, 16f, color, true))
                    layoutParams = LinearLayout.LayoutParams(0, dp(74), 1f).apply { marginEnd = dp(5) }
                }
                stats.addView(stat("Баланс (PAPER)", formatNumber(paperCash, 2) + " USDT", dark))
                stats.addView(stat("Сегодня (P/L)", (if (todayPnl >= 0) "+" else "") + formatNumber(todayPnl, 2), if (todayPnl >= 0) Color.rgb(0, 220, 105) else Color.RED))
                stats.addView(stat("Всего (P/L)", (if (totalPnl >= 0) "+" else "") + formatNumber(totalPnl, 2), if (totalPnl >= 0) Color.rgb(0, 220, 105) else Color.RED))
                content.addView(stats)
                val modeRow = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                modeRow.addView(label("▱ PAPER", 14f, Color.rgb(0, 220, 105), true), LinearLayout.LayoutParams(0, dp(42), 1f))
                modeRow.addView(label("Виртуальная торговля", 11f, muted), LinearLayout.LayoutParams(0, dp(42), 1f))
                content.addView(modeRow)
            }
            if (selectedTab == "MARKET") {
                content.addView(label("ГЛОБАЛЬНЫЙ СПИСОК КРИПТОВАЛЮТ", 14f, dark, true))
                content.addView(label("CoinPaprika • уникальные активы без стейблкоинов • список не является торговым сигналом Bybit", 11f, muted))
                val all = globalAssets
                if (all == null) {
                    content.addView(label(globalMarketError ?: "Загрузка глобального списка до 2 000 активов...", 13f, muted))
                } else {
                    content.addView(label("Доступно: ${all.size} активов • Страница ${globalPage + 1}", 12f, muted))
                    val start = globalPage * globalPageSize
                    val end = minOf(start + globalPageSize, all.size)
                    for (i in start until end) {
                        val coin = all[i]
                        val name = coin.optString("name", coin.optString("symbol"))
                        val symbol = coin.optString("symbol", "—")
                        val price = coin.optDouble("price_usd", Double.NaN)
                        val change = if (coin.isNull("change_24h_pct")) null else coin.optDouble("change_24h_pct")
                        val row = LinearLayout(context).apply {
                            orientation = LinearLayout.VERTICAL
                            setPadding(dp(9), dp(7), dp(9), dp(7))
                            background = rounded(page, 9)
                        }
                        row.addView(label("${i + 1}. $name ($symbol)", 13f, dark, true))
                        row.addView(label("Цена: ${if (price.isFinite()) formatNumber(price, 8) + " USD" else "—"}  •  24ч: ${change?.let { formatNumber(it, 2) + "%" } ?: "нет данных"}", 11f, muted))
                        row.addView(label("Ранг: ${coin.optString("rank", "—")}  •  Объём 24ч: ${formatNumber(coin.optDouble("volume_24h_usd", 0.0), 0)} USD", 10f, muted))
                        content.addView(row)
                        content.addView(View(context).apply { layoutParams = LinearLayout.LayoutParams(1, dp(5)) })
                    }
                    val pager = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                    val prev = Button(context).apply {
                        text = "Назад"
                        buttonStyle(this, muted)
                        isEnabled = globalPage > 0
                        setOnClickListener { globalPage--; render() }
                        layoutParams = LinearLayout.LayoutParams(0, dp(44), 1f).apply { marginEnd = dp(5) }
                    }
                    val next = Button(context).apply {
                        text = "Далее"
                        buttonStyle(this, blue)
                        isEnabled = end < all.size
                        setOnClickListener { globalPage++; render() }
                        layoutParams = LinearLayout.LayoutParams(0, dp(44), 1f)
                    }
                    pager.addView(prev); pager.addView(next); content.addView(pager)
                    val reload = Button(context).apply {
                        text = "Обновить глобальный список"
                        buttonStyle(this, purple)
                        setOnClickListener { loadGlobalMarket() }
                    }
                    content.addView(reload)
                }
            } else if (selectedTab == "PAPER") {
                content.addView(label("ПРАВИЛА АВТОМАТИЧЕСКОЙ ТОРГОВЛИ", 13f, dark, true))
                content.addView(label(
                    "Покупка: оценка 83+ без ожидания BUY.\\n" +
                    "Продажа: убыток −2% или оценка ниже 83.\\n" +
                    "При +10%: продать и сразу купить ту же криптовалюту заново, даже если оценка ниже 83.",
                    12f, muted
                ))
                content.addView(label("СЕРВЕРНЫЙ АВТОМАТИЧЕСКИЙ СЧЁТ", 14f, Color.rgb(20, 145, 90), true))
                val remote = serverPaperState
                if (remote == null) {
                    content.addView(label(
                        serverPaperError ?: "Нажмите «Обновить серверный счёт», чтобы проверить круглосуточный режим.",
                        12f, if (serverPaperError == null) muted else Color.RED
                    ))
                } else {
                    val serverEnabled = remote.optBoolean("enabled", false)
                    val persistent = remote.optBoolean("storage_persistent_configured", false)
                    content.addView(label(
                        if (serverEnabled) "Автоматический режим: ВКЛЮЧЁН" else "Автоматический режим: ВЫКЛЮЧЕН",
                        14f, if (serverEnabled) Color.rgb(20, 145, 90) else muted, true
                    ))
                    content.addView(label(
                        "Серверный баланс: " + formatNumber(remote.optDouble("cash_usdt"), 2) + " USDT",
                        16f, dark, true
                    ))
                    content.addView(label(
                        "Открытые позиции: " + formatNumber(remote.optDouble("holdings_value_usdt"), 2) +
                            " USDT • Всего: " + formatNumber(remote.optDouble("equity_usdt"), 2) + " USDT",
                        13f, dark
                    ))
                    content.addView(label(
                        if (persistent) "Хранилище: постоянный диск подключён"
                        else "Хранилище временное — запуск заблокирован до настройки постоянного диска.",
                        12f, if (persistent) Color.rgb(20, 145, 90) else Color.rgb(190, 90, 45)
                    ))
                    val remotePositions = remote.optJSONArray("open_positions") ?: JSONArray()
                    for (i in 0 until remotePositions.length()) {
                        val p = remotePositions.optJSONObject(i) ?: continue
                        content.addView(label(
                            p.optString("symbol") + " • " + p.optString("exchange") +
                                " • SL " + formatNumber(p.optDouble("stop_loss"), 6) +
                                " • TP " + formatNumber(p.optDouble("take_profit"), 6),
                            11f, muted
                        ))
                    }
                    val lastScan = remote.optString("last_scan_at", "")
                    if (lastScan.isNotBlank() && lastScan != "null") {
                        content.addView(label("Последняя проверка: $lastScan", 10f, muted))
                    }
                    val remoteError = remote.optString("last_error", "")
                    if (remoteError.isNotBlank() && remoteError != "null") {
                        content.addView(label("Ошибка сканирования: $remoteError", 11f, Color.RED))
                    }
                }
                if (remote != null) {
                    content.addView(label("ИСТОРИЯ СЕРВЕРНЫХ СДЕЛОК", 14f, dark, true))
                    val serverHistory = remote.optJSONArray("history") ?: JSONArray()
                    if (serverHistory.length() == 0) {
                        content.addView(label("Пока нет записей о сделках.", 12f, muted))
                    } else {
                        for (i in 0 until serverHistory.length()) {
                            val trade = serverHistory.optJSONObject(i) ?: continue
                            val side = trade.optString("side", "—")
                            val symbol = trade.optString("symbol", "—")
                            val exchange = trade.optString("exchange", "—")
                            val direction = if (side.startsWith("SHORT")) "SHORT" else "LONG"
                            val openedAt = formatBishkekTime(trade.optString("opened_at", ""))
                            val closedAt = formatBishkekTime(trade.optString("closed_at", ""))
                            val pnl = if (trade.isNull("pnl_usdt")) null else trade.optDouble("pnl_usdt")
                            val reasonLabel = trade.optString("close_reason_label", "")
                            val eventTime = formatBishkekTime(trade.optString("time", ""))
                            val entry = LinearLayout(context).apply {
                                orientation = LinearLayout.VERTICAL
                                setPadding(dp(9), dp(8), dp(9), dp(8))
                                background = rounded(page, 10)
                            }
                            entry.addView(label("$symbol • $exchange • $direction", 13f, dark, true))
                            entry.addView(label("Открытие: $openedAt", 11f, muted))
                            if (closedAt != "—") {
                                entry.addView(label("Закрытие: $closedAt", 11f, muted))
                                val closeReason = if (reasonLabel.isBlank()) trade.optString("reason", "—") else reasonLabel
                                entry.addView(label("Причина: $closeReason", 11f, dark))
                            } else if (side == "BUY" || side == "SHORT_OPEN") {
                                entry.addView(label("Статус события: открытие / добавление позиции", 11f, muted))
                            } else {
                                entry.addView(label("Время события: $eventTime • " + trade.optString("reason", "—"), 11f, muted))
                            }
                            if (pnl != null) {
                                entry.addView(label(
                                    "Результат: " + formatNumber(pnl, 2) + " USDT",
                                    12f, if (pnl >= 0) Color.rgb(20,145,90) else Color.RED, true
                                ))
                            }
                            content.addView(entry)
                            content.addView(View(context).apply {
                                layoutParams = LinearLayout.LayoutParams(1, dp(6))
                            })
                        }
                    }
                }
                if (serverPaperError != null) {
                    content.addView(label("Связь с сервером: $serverPaperError", 11f, Color.RED))
                }
                val serverButtons = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                val serverRefresh = Button(context).apply {
                    text = if (serverPaperLoading) "Загрузка..." else "Обновить сервер"
                    buttonStyle(this, blue)
                    isEnabled = !serverPaperLoading
                    setOnClickListener { refreshPaperServer() }
                    layoutParams = LinearLayout.LayoutParams(0, dp(46), 1f).apply { marginEnd = dp(5) }
                }
                serverButtons.addView(serverRefresh)
                val remoteEnabled = remote?.optBoolean("enabled", false) ?: false
                val storageReady = remote?.optBoolean("storage_persistent_configured", false) ?: false
                val serverToggle = Button(context).apply {
                    text = if (remoteEnabled) "Остановить авто" else "Запустить авто"
                    buttonStyle(this, if (remoteEnabled) orange else Color.rgb(20, 145, 90))
                    isEnabled = !serverPaperLoading && remote != null && (remoteEnabled || storageReady)
                    setOnClickListener { togglePaperServer(!remoteEnabled) }
                    layoutParams = LinearLayout.LayoutParams(0, dp(46), 1f)
                }
                serverButtons.addView(serverToggle)
                content.addView(serverButtons)
                content.addView(label(
                    "Сервер торгует только виртуально: Spot, без плеча. Реальные ордера не отправляются.",
                    11f, muted
                ))
                content.addView(label("ЛОКАЛЬНЫЙ ТЕСТОВЫЙ СЧЁТ (ручной)", 13f, dark, true))
                val livePrices = mutableMapOf<String, Double>()
                listOf(binance, bybit, coinbase, okx).filterNotNull().forEach { result ->
                    result.assets.forEach { coin ->
                        livePrices[result.exchange + ":" + coin.optString("symbol")] = coin.optDouble("price", 0.0)
                    }
                }
                val keys = paperPositions.keys().asSequence().toList()
                var holdingsValue = 0.0
                keys.forEach { key ->
                    val p = paperPositions.optJSONObject(key) ?: return@forEach
                    holdingsValue += p.optDouble("quantity", 0.0) * (livePrices[key] ?: 0.0)
                }
                content.addView(label("Доступно: " + formatNumber(paperCash, 2) + " USDT", 18f, dark, true))
                content.addView(label("Открытые позиции: " + formatNumber(holdingsValue, 2) + " USDT", 14f, dark))
                content.addView(label("Всего на счёте: " + formatNumber(paperCash + holdingsValue, 2) + " USDT", 17f, dark, true))
                content.addView(label("Стартовый депозит $1 000 • комиссия 0,1% на каждую сторону", 12f, muted))
                content.addView(label("ОТКРЫТЫЕ ПОЗИЦИИ", 14f, dark, true))
                if (keys.isEmpty()) content.addView(label("Пока нет покупок. Автоматический серверный счёт работает отдельно от ручного тестового счёта.", 13f, muted))
                keys.forEach { key ->
                    val p = paperPositions.optJSONObject(key) ?: return@forEach
                    val price = livePrices[key] ?: 0.0
                    val qty = p.optDouble("quantity", 0.0)
                    val value = qty * price
                    val pnl = value - p.optDouble("cost_usdt", 0.0)
                    content.addView(label(p.optString("symbol") + " • " + p.optString("exchange"), 15f, dark, true))
                    content.addView(label("Количество: " + formatNumber(qty, 8) + " • Стоимость: " + formatNumber(value, 2) + " USDT", 12f, muted))
                    content.addView(label("Прибыль/убыток: " + formatNumber(pnl, 2) + " USDT", 12f, if (pnl >= 0) Color.rgb(20,145,90) else Color.RED))
                    val sell = Button(context).apply { text = "Продать всю позицию"; buttonStyle(this, orange) }
                    sell.setOnClickListener { paperTrade(p.optString("exchange"), p.optString("symbol"), price, null, true) }
                    content.addView(sell)
                }
                content.addView(label("ИСТОРИЯ СДЕЛОК", 14f, dark, true))
                for (i in (paperHistory.length() - 1 downTo maxOf(0, paperHistory.length() - 20))) {
                    val h = paperHistory.optJSONObject(i) ?: continue
                    content.addView(label(h.optString("side") + " " + h.optString("symbol") + " • " + formatNumber(h.optDouble("price"), 8) + " USDT", 12f, dark))
                    if (h.has("pnl_usdt")) content.addView(label("Результат: " + formatNumber(h.optDouble("pnl_usdt"), 2) + " USDT", 12f, muted))
                }
                val reset = Button(context).apply { text = "Сбросить виртуальный счёт до $1 000"; buttonStyle(this, muted) }
                reset.setOnClickListener {
                    paperCash = 1000.0
                    paperPositions = JSONObject()
                    paperHistory = JSONArray()
                    savePaper()
                    render()
                }
                content.addView(reset)
            } else {
            val b = binance
            val y = bybit
            val c = coinbase
            val o = okx
            if (b == null || y == null || c == null || o == null) {
                content.addView(label("Ожидаем ответы четырёх бирж.\n${status.text}", 14f, muted))
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
                    "Сравнение Binance, Bybit, Coinbase Exchange и OKX. В рейтинге учитывается оценка, а не порядок ответа API.",
                    12f, muted
                ))
                val combined = (y.assets).map { it to y }
                val top = combined.sortedWith(
                    compareByDescending<Pair<JSONObject, ExchangeResult>> { it.first.optInt("score", 0) }
                        .thenByDescending { it.first.optDouble("volume_ratio", 0.0) }
                ).take(3)
                if (top.isEmpty()) content.addView(label("Нет доступных результатов. Проверьте соединение.", 14f, muted))
                val table = LinearLayout(context).apply {
                    orientation = LinearLayout.VERTICAL
                    background = rounded(page, 12)
                    setPadding(dp(7), dp(7), dp(7), dp(7))
                }
                fun tableRow(values: List<String>, header: Boolean = false, positive: Boolean = false) {
                    val row = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                    val weights = listOf(1.25f, 1.15f, 0.8f, 0.65f, 0.55f, 0.55f, 0.85f)
                    values.forEachIndexed { index, value ->
                        val color = if (header) muted else if (index == 2 && positive) Color.rgb(0, 220, 105) else dark
                        val cell = label(value, if (header) 9f else 10f, color, !header)
                        cell.gravity = Gravity.CENTER
                        row.addView(cell, LinearLayout.LayoutParams(0, dp(if (header) 32 else 48), weights[index]))
                    }
                    table.addView(row)
                    table.addView(View(context).apply { setBackgroundColor(border); layoutParams = LinearLayout.LayoutParams(-1, dp(1)) })
                }
                tableRow(listOf("Пара", "Цена", "24ч", "Оценка", "RSI", "ATR", "EMA20"), true)
                top.forEach { (coin, exchange) ->
                    val symbol = coin.optString("symbol", "—").replace("-USDT", "/USDT")
                    val change = coin.optDouble("change_24h_pct", coin.optDouble("price_change_24h_pct", 0.0))
                    tableRow(listOf(
                        symbol, formatNumber(coin.optDouble("price", 0.0), 4),
                        (if (change >= 0) "+" else "") + formatNumber(change, 2) + "%",
                        coin.optInt("score", 0).toString(),
                        coin.optString("rsi", coin.optString("rsi_14", "—")),
                        coin.optString("atr_14", "—"),
                        coin.optString("ema20", coin.optString("ma20", "—"))
                    ), false, change >= 0)
                }
                content.addView(table)
                if (top.isNotEmpty()) {
                    val first = top.first()
                    val coin = first.first
                    val symbol = coin.optString("symbol", "")
                    val price = coin.optDouble("price", 0.0)
                    val tradePanel = LinearLayout(context).apply {
                        orientation = LinearLayout.HORIZONTAL
                        gravity = Gravity.CENTER_VERTICAL
                        setPadding(dp(8), dp(9), dp(8), dp(9))
                        background = rounded(card, 12)
                    }
                    val amount = EditText(context).apply {
                        setText("100"); hint = "USDT"; textSize = 13f
                        inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
                        setTextColor(dark); setSingleLine(true)
                        layoutParams = LinearLayout.LayoutParams(dp(72), dp(44))
                    }
                    val buy = Button(context).apply { text = "Купить"; buttonStyle(this, Color.rgb(0, 165, 85)) }
                    buy.setOnClickListener {
                        val value = amount.text.toString().replace(",", ".").toDoubleOrNull()
                        if (value == null || value <= 0) status.text = "Введите сумму USDT."
                        else paperTrade("Bybit", symbol, price, value, false)
                    }
                    val sell = Button(context).apply { text = "Продать"; buttonStyle(this, Color.rgb(225, 48, 60)) }
                    sell.setOnClickListener { paperTrade("Bybit", symbol, price, null, true) }
                    tradePanel.addView(label("Bybit", 12f, dark, true), LinearLayout.LayoutParams(0, dp(44), 1f))
                    tradePanel.addView(amount)
                    tradePanel.addView(buy)
                    tradePanel.addView(sell)
                    content.addView(tradePanel)
                }
                content.addView(label(
                    "Binance: ${b.assets.size} • Bybit: ${y.assets.size} • Coinbase: ${c.assets.size} • OKX: ${o.assets.size}",
                    12f, muted
                ))
            } else {
                val result = when (selectedTab) { "BINANCE" -> b; "BYBIT" -> y; "COINBASE" -> c; else -> o }
                content.addView(label(
                    "Источник: ${result.source}\nПроверено пар: ${result.scanned} • Подходящих Spot-пар: ${result.eligible}\nBTC market state: ${result.btcState}",
                    12f, muted
                ))
                if (result.error != null) {
                    content.addView(label("Ошибка загрузки: ${result.error}", 14f, Color.RED))
                } else if (result.assets.isEmpty()) {
                    content.addView(label("Кандидаты не найдены.", 14f, muted))
                } else {
                    fun baseSymbol(asset: JSONObject): String = asset.optString("symbol", "")
                        .uppercase(Locale.US).removeSuffix("-USDT").removeSuffix("USDT").removeSuffix("-USD").removeSuffix("USD")
                    val bybitSymbols = y.assets.map { baseSymbol(it) }.toSet()
                    val binanceSymbols = b.assets.map { baseSymbol(it) }.toSet()
                    val coinbaseSymbols = c.assets.map { baseSymbol(it) }.toSet()
                    val excluded = when (selectedTab) {
                        "BINANCE" -> bybitSymbols
                        "COINBASE" -> bybitSymbols + binanceSymbols
                        "OKX" -> bybitSymbols + binanceSymbols + coinbaseSymbols
                        else -> emptySet()
                    }
                    val uniqueAssets = result.assets.filter { baseSymbol(it) !in excluded }
                    content.addView(label("Уникальных монет этого раздела: ${uniqueAssets.size}. Приоритет: Bybit → Binance → Coinbase → OKX.", 11f, muted))
                    uniqueAssets.sortedByDescending { it.optInt("score", 0) }
                        .forEach { coinCard(it, result.exchange, result, maxRisk, allowTrading = selectedTab == "BYBIT") }
                }
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

        refreshPaperServer = {
            if (!serverPaperLoading) {
                serverPaperLoading = true
                serverPaperError = null
                render()
                Thread {
                    var state: JSONObject? = null
                    var error: String? = null
                    try {
                        val connection = URL("https://han-invest-api-test.onrender.com/api/v1/paper/state")
                            .openConnection() as HttpURLConnection
                        try {
                            connection.requestMethod = "GET"
                            connection.connectTimeout = 15000
                            connection.readTimeout = 20000
                            connection.setRequestProperty("Accept", "application/json")
                            val code = connection.responseCode
                            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                            val raw = stream?.bufferedReader()?.use { it.readText() } ?: ""
                            if (code !in 200..299) throw Exception("HTTP $code: $raw")
                            state = JSONObject(raw)
                        } finally {
                            connection.disconnect()
                        }
                    } catch (e: Exception) {
                        error = e.message ?: "Ошибка соединения"
                    }
                    Handler(Looper.getMainLooper()).post {
                        serverPaperState = state
                        serverPaperError = error
                        serverPaperLoading = false
                        render()
                    }
                }.start()
            }
        }

        togglePaperServer = { enable ->
            if (!serverPaperLoading) {
                serverPaperLoading = true
                serverPaperError = null
                render()
                Thread {
                    var state: JSONObject? = null
                    var error: String? = null
                    try {
                        val action = if (enable) "enable" else "disable"
                        val connection = URL("https://han-invest-api-test.onrender.com/api/v1/paper/$action")
                            .openConnection() as HttpURLConnection
                        try {
                            connection.requestMethod = "POST"
                            connection.connectTimeout = 15000
                            connection.readTimeout = 20000
                            connection.setRequestProperty("Accept", "application/json")
                            val code = connection.responseCode
                            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                            val raw = stream?.bufferedReader()?.use { it.readText() } ?: ""
                            if (code !in 200..299) throw Exception("HTTP $code: $raw")
                            state = JSONObject(raw)
                        } finally {
                            connection.disconnect()
                        }
                    } catch (e: Exception) {
                        error = e.message ?: "Ошибка соединения"
                    }
                    Handler(Looper.getMainLooper()).post {
                        serverPaperState = state ?: serverPaperState
                        serverPaperError = error
                        serverPaperLoading = false
                        render()
                    }
                }.start()
            }
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
            coinbase = null
            okx = null
            marketContext = null
            fetchMarketContext()
            loadGlobalMarket()
            loadingCount = 4
            status.text = "Загружаем Binance, Bybit, Coinbase Exchange и OKX независимо..."
            render()
            val query = "limit=30&universe_limit=100&min_volume=1000000&capital_usdt=$capital&risk_pct=$risk"
            fetchExchange("Binance",
                "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?$query") {
                binance = it
                loadingCount--
                status.text = if (loadingCount == 0) "Данные всех четырёх бирж обновлены."
                    else "Binance получен. Остальные биржи загружаются..."
                render()
            }
            fetchExchange("Bybit",
                "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000&capital_usdt=$capital&risk_pct=$risk") {
                bybit = it
                loadingCount--
                status.text = if (loadingCount == 0) "Данные всех четырёх бирж обновлены."
                    else "Bybit получен. Остальные биржи загружаются..."
                render()
            }
            fetchExchange("Coinbase Exchange",
                "https://han-invest-api-test.onrender.com/api/v1/crypto/coinbase/scan?limit=30&min_volume=1000000") {
                coinbase = it
                loadingCount--
                status.text = if (loadingCount == 0) "Данные всех четырёх бирж обновлены."
                    else "Coinbase Exchange получен. Остальные биржи загружаются..."
                render()
            }
            fetchExchange("OKX",
                "https://han-invest-api-test.onrender.com/api/v1/crypto/okx/scan?limit=30&min_volume=1000000") {
                okx = it
                loadingCount--
                status.text = if (loadingCount == 0) "Данные всех четырёх бирж обновлены."
                    else "OKX получен. Остальные биржи загружаются..."
                render()
            }
        }

        refresh.setOnClickListener { loadAll() }
        section.addView(label(
            "Экран обновляет список каждые 15 минут; серверная виртуальная торговля работает независимо от открытого экрана.",
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
