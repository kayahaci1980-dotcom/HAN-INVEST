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
    private val blue = Color.rgb(76, 67, 245)
    private val orange = Color.rgb(255, 157, 45)
    private val purple = Color.rgb(177, 66, 255)
    private val dark = Color.rgb(242, 244, 255)
    private val muted = Color.rgb(158, 169, 197)
    private val page = Color.rgb(8, 12, 30)

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
        button.background = GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT, intArrayOf(color, purple)).apply { cornerRadius = dp(12).toFloat() }
        button.isAllCaps = false
    }

    fun createView(): View {
        val section = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        section.addView(label("₿ Криптосканер HAN INVEST", 21f, dark, true))
        section.addView(label(
            "Четыре биржи анализируются отдельно: Binance, Bybit, Coinbase Exchange и OKX.",
            13f, muted
        ))

        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = rounded(Color.rgb(13, 21, 46))
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
            gravity = android.view.Gravity.CENTER
            setPadding(0, dp(14), 0, dp(10))
        }
        val content = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(12), dp(12), dp(12))
            background = rounded(Color.rgb(17, 25, 52))
        }
        val scroll = ScrollView(context).apply {
            isFillViewport = true
            addView(content)
        }
        val status = label("Загрузка данных четырёх криптобирж...", 14f, dark)
        val refresh = Button(context).apply {
            text = "Обновить обе биржи"
            buttonStyle(this, blue)
        }
        panel.addView(status)
        panel.addView(refresh)
        panel.visibility = View.GONE
        section.addView(tabs)
        section.addView(scroll)
        section.addView(panel)

        var selectedTab = "HOME"
        lateinit var render: () -> Unit
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
        var marketExchangeFilter = "ALL"
        var marketMoveFilter = "ALL"
        var loadingCount = 0

        fun addTab(title: String, key: String, color: Int) {
            val b = Button(context).apply {
                text = when (key) {
                    "HOME" -> "⌂\n$title"
                    "MARKET" -> "▥\n$title"
                    "ANALYSIS" -> "◎\n$title"
                    "PAPER" -> "▣\n$title"
                    else -> "◷\n$title"
                }
                textSize = 11f
                setTypeface(null, Typeface.BOLD)
                buttonStyle(this, if (selectedTab == key) color else Color.rgb(28, 38, 69))
                layoutParams = LinearLayout.LayoutParams(0, dp(62), 1f).apply {
                    marginEnd = dp(5)
                }
                setOnClickListener {
                    selectedTab = key
                    render()
                    if (key == "PAPER" || key == "HISTORY") refreshPaperServer()
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

        fun coinCard(coin: JSONObject, exchange: String, result: ExchangeResult, maxRisk: Double) {
            val symbol = coin.optString("symbol", "—")
            val shortSymbol = symbol.removeSuffix("-USDT").removeSuffix("USDT")
            val coinName = coin.optString("name").takeIf { it.isNotBlank() } ?: shortSymbol
            val price = coin.optDouble("price", Double.NaN)
            val score = coin.optInt("score", 0)
            val serverSignal = coin.optString("signal", "WATCH")
            val buyAllowed = score >= 83 && tradeReady(coin, result, maxRisk)
            val early = coin.optString("early_signal") == "EARLY BUY"
            val card = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), dp(11), dp(12), dp(11))
                background = rounded(Color.rgb(20, 29, 59), 12)
            }
            val signalText = if (buyAllowed) "АВТОПОКУПКА: оценка 83+"
                else if (early) "EARLY BUY — наблюдение" else "WATCH — наблюдение"
            card.addView(label("$coinName ($shortSymbol)  •  $exchange", 16f, dark, true))
            card.addView(label(
                "Оценка: $score/100   |   Сигнал: $signalText",
                13f, if (buyAllowed) Color.rgb(20, 145, 90) else muted, true
            ))
            val change24 = coin.optString("change_24h_pct", coin.optString("price_change_percent_24h", coin.optString("change_24h", "—")))
            val rsi = coin.optString("rsi_14", coin.optString("rsi", "—"))
            val atrValue = coin.optString("atr_14", coin.optString("atr", "—"))
            val ema20 = coin.optString("ema20", coin.optString("ema_20", coin.optString("ma20", "—")))
            card.addView(label(
                "Цена: ${if (price.isNaN()) "—" else formatNumber(price, 8)} USDT   •   24 ч.: $change24%",
                13f, dark
            ))
            val metrics = LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(8), 0, dp(8))
            }
            listOf("RSI" to rsi, "ATR" to atrValue, "EMA20" to ema20).forEach { (name, value) ->
                val metric = LinearLayout(context).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(dp(8), dp(7), dp(8), dp(7))
                    background = rounded(Color.rgb(10, 17, 39), 9)
                    layoutParams = LinearLayout.LayoutParams(0, dp(54), 1f).apply { marginEnd = dp(5) }
                }
                metric.addView(label(name, 10f, muted, true))
                metric.addView(label(value, 12f, dark, true))
                metrics.addView(metric)
            }
            card.addView(metrics)
            val detailsButton = Button(context).apply {
                text = "ⓘ  Подробнее об активе"
                textSize = 11f
                buttonStyle(this, blue)
                setOnClickListener {
                    val dialogContent = LinearLayout(context).apply {
                        orientation = LinearLayout.VERTICAL
                        setPadding(dp(18), dp(10), dp(18), dp(16))
                        background = rounded(Color.rgb(10, 17, 39), 18)
                    }
                    val dialogHeader = LinearLayout(context).apply {
                        orientation = LinearLayout.HORIZONTAL
                        gravity = android.view.Gravity.CENTER_VERTICAL
                    }
                    dialogHeader.addView(label("$coinName ($shortSymbol)", 19f, Color.WHITE, true),
                        LinearLayout.LayoutParams(0, -2, 1f))
                    val close = TextView(context).apply {
                        text = "✕"
                        textSize = 23f
                        setTextColor(Color.WHITE)
                        gravity = android.view.Gravity.CENTER
                        setPadding(dp(12), dp(5), dp(5), dp(5))
                    }
                    dialogHeader.addView(close)
                    dialogContent.addView(dialogHeader)
                    dialogContent.addView(label("Биржа: $exchange", 13f, muted))
                    dialogContent.addView(label("Оценка: $score / 100", 16f, purple, true))
                    dialogContent.addView(label("Сигнал: $signalText", 13f, dark, true))
                    dialogContent.addView(label("Цена: ${if (price.isNaN()) "—" else formatNumber(price, 8)} USDT", 14f, dark))
                    dialogContent.addView(label("Изменение за 24 часа: $change24%", 14f, dark))
                    dialogContent.addView(label("RSI (14): $rsi", 14f, dark))
                    dialogContent.addView(label("ATR (14): $atrValue", 14f, dark))
                    dialogContent.addView(label("EMA20: $ema20", 14f, dark))
                    dialogContent.addView(label("MA20: ${coin.optString("ma20", "—")}", 14f, dark))
                    dialogContent.addView(label("MA50: ${coin.optString("ma50", "—")}", 14f, dark))
                    dialogContent.addView(label("ATR дневной: ${coin.optString("atr_14_1d", "—")}", 14f, dark))
                    dialogContent.addView(label("Сила против BTC: ${coin.optString("relative_strength_vs_btc_24h_pct", "—")}%", 14f, dark))
                    dialogContent.addView(label("Объём к среднему: ${coin.optString("volume_ratio", "—")}x", 14f, dark))
                    if (buyAllowed) {
                        val sizing = coin.optJSONObject("position_sizing")
                        dialogContent.addView(label("Вход: ${coin.optString("entry_price", "—")}", 14f, dark))
                        dialogContent.addView(label("Stop Loss: ${coin.optString("stop_loss", "—")}", 14f, dark))
                        dialogContent.addView(label("Take Profit: ${coin.optString("take_profit", "—")}", 14f, dark))
                        dialogContent.addView(label("Количество: ${sizing?.optString("quantity", "—")}", 14f, dark))
                        dialogContent.addView(label("Размер позиции: ${sizing?.optString("position_value_usdt", "—")} USDT", 14f, dark))
                        dialogContent.addView(label("Риск при Stop Loss: ${sizing?.optString("potential_loss_at_stop_usdt", "—")} USDT", 14f, dark))
                    }
                    val detailDialog = android.app.AlertDialog.Builder(context)
                        .setView(dialogContent)
                        .create()
                    close.setOnClickListener { detailDialog.dismiss() }
                    detailDialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
                    detailDialog.show()
                    detailDialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
                    detailDialog.window?.setLayout(
                        (resources.displayMetrics.widthPixels * 0.92).toInt(),
                        android.view.WindowManager.LayoutParams.WRAP_CONTENT
                    )
                }
            }
            card.addView(detailsButton)
            card.addView(label(
                "MA20: ${coin.optString("ma20", "—")}   |   MA50: ${coin.optString("ma50", "—")}   |   ATR(14): ${coin.optString("atr_14_1d", coin.optString("atr_14", "—"))}",
                11f, muted
            ))
            card.addView(label(
                "Сила против BTC: ${coin.optString("relative_strength_vs_btc_24h_pct", "—")}%   •   Объём к среднему: ${coin.optString("volume_ratio", "—")}x",
                11f, muted
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
            addTab("Главная", "HOME", purple)
            addTab("Рынок LIVE", "MARKET", blue)
            addTab("Анализ", "ANALYSIS", blue)
            addTab("PAPER", "PAPER", Color.rgb(20, 145, 90))
            addTab("История", "HISTORY", purple)
            content.removeAllViews()
            content.addView(label(
                when (selectedTab) {
                    "MARKET" -> "РЫНОК LIVE — BINANCE • BYBIT • COINBASE • OKX"
                    "ANALYSIS" -> "АНАЛИЗ — ТЕХНИЧЕСКИЕ ПОКАЗАТЕЛИ"
                    "PAPER" -> "PAPER TRADING — ВИРТУАЛЬНЫЙ СЧЁТ"
                    "HISTORY" -> "ИСТОРИЯ СДЕЛОК"
                    else -> "HAN INVEST — ГЛАВНАЯ"
                }, 17f, dark, true
            ))
            if (selectedTab == "HISTORY") {
                val remoteHistory = serverPaperState?.optJSONArray("history") ?: JSONArray()
                if (remoteHistory.length() == 0) content.addView(label("История серверных сделок пока пуста или ещё загружается.", 13f, muted))
                for (i in remoteHistory.length() - 1 downTo maxOf(0, remoteHistory.length() - 100)) {
                    val trade = remoteHistory.optJSONObject(i) ?: continue
                    content.addView(label(trade.optString("side") + " • " + trade.optString("symbol") + " • " + trade.optString("exchange"), 14f, dark, true))
                    content.addView(label("Цена: " + formatNumber(trade.optDouble("price"), 8) + " • Результат: " + formatNumber(trade.optDouble("pnl_usdt"), 2) + " USDT", 12f, muted))
                }
                val reloadHistory = Button(context).apply { text = "Обновить историю"; buttonStyle(this, blue); setOnClickListener { refreshPaperServer() } }
                content.addView(reloadHistory)
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
                        val symbol = p.optString("symbol")
                        val exchange = p.optString("exchange")
                        content.addView(label(
                            symbol + " • " + exchange +
                                " • Вход " + formatNumber(p.optDouble("entry_price"), 8) +
                                " • Сейчас " + formatNumber(p.optDouble("last_price"), 8),
                            12f, dark, true
                        ))
                        content.addView(label(
                            "Количество: " + formatNumber(p.optDouble("quantity"), 8) +
                                " • Стоимость: " + formatNumber(p.optDouble("quantity") * p.optDouble("last_price"), 2) + " USDT",
                            11f, muted
                        ))
                        val manualSell = Button(context).apply {
                            text = "Продать эту виртуальную позицию"
                            buttonStyle(this, orange)
                            isEnabled = !serverPaperLoading
                            setOnClickListener {
                                isEnabled = false
                                Thread {
                                    var error: String? = null
                                    var updated: JSONObject? = null
                                    try {
                                        val connection = URL("https://han-invest-api-test.onrender.com/api/v1/paper/sell")
                                            .openConnection() as HttpURLConnection
                                        try {
                                            connection.requestMethod = "POST"
                                            connection.doOutput = true
                                            connection.connectTimeout = 15000
                                            connection.readTimeout = 20000
                                            connection.setRequestProperty("Content-Type", "application/json")
                                            connection.setRequestProperty("Accept", "application/json")
                                            val body = JSONObject().put("exchange", exchange).put("symbol", symbol).toString()
                                            connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                                            val code = connection.responseCode
                                            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                                            val raw = stream?.bufferedReader()?.use { it.readText() } ?: ""
                                            if (code !in 200..299) throw Exception("HTTP $code: $raw")
                                            updated = JSONObject(raw)
                                        } finally { connection.disconnect() }
                                    } catch (e: Exception) { error = e.message ?: "Ошибка продажи" }
                                    Handler(Looper.getMainLooper()).post {
                                        if (updated != null) {
                                            serverPaperState = updated
                                            serverPaperError = null
                                        } else {
                                            serverPaperError = error
                                        }
                                        refreshPaperServer()
                                    }
                                }.start()
                            }
                        }
                        content.addView(manualSell)
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
                    text = if (remoteEnabled) "ЭКСТРЕННЫЙ СТОП" else "Запустить авто"
                    buttonStyle(this, if (remoteEnabled) Color.rgb(200, 35, 45) else Color.rgb(20, 145, 90))
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
            if (selectedTab == "HOME" || selectedTab == "MARKET" || selectedTab == "ANALYSIS") {
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
                val combined = mutableListOf<Pair<JSONObject, ExchangeResult>>()
                if (b.error == null) b.assets.forEach { combined.add(it to b) }
                if (y.error == null) y.assets.forEach { combined.add(it to y) }
                if (c.error == null) c.assets.forEach { combined.add(it to c) }
                if (o.error == null) o.assets.forEach { combined.add(it to o) }
                val ranked = combined.sortedWith(
                    compareByDescending<Pair<JSONObject, ExchangeResult>> { it.first.optInt("score", 0) }
                        .thenByDescending { it.first.optDouble("volume_ratio", 0.0) }
                )
                fun moveValue(coin: JSONObject): Double {
                    return coin.optDouble("change_24h_pct",
                        coin.optDouble("price_change_percent_24h", coin.optDouble("change_24h", 0.0)))
                }
                if (selectedTab == "HOME") {
                    val stats = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                    val remote = serverPaperState
                    val balance = remote?.optDouble("equity_usdt", 1000.0) ?: 1000.0
                    val holdings = remote?.optJSONArray("open_positions")?.length() ?: 0
                    listOf("ВИРТУАЛЬНЫЙ СЧЁТ" to "$" + formatNumber(balance, 2),
                        "ОТКРЫТЫХ ПОЗИЦИЙ" to "$holdings / 3").forEach { item ->
                        val tile = LinearLayout(context).apply {
                            orientation = LinearLayout.VERTICAL
                            setPadding(dp(10), dp(12), dp(10), dp(12))
                            background = rounded(Color.rgb(24, 34, 68), 13)
                            layoutParams = LinearLayout.LayoutParams(0, dp(72), 1f).apply { marginEnd = dp(7) }
                        }
                        tile.addView(label(item.first, 9f, muted, true))
                        tile.addView(label(item.second, 17f, dark, true))
                        stats.addView(tile)
                    }
                    content.addView(stats)
                    content.addView(label("● РЫНОК LIVE", 17f, Color.rgb(20, 220, 110), true))
                    content.addView(label("ТОП-3 ЛУЧШИХ РЕЗУЛЬТАТА", 16f, dark, true))
                }
                if (selectedTab == "MARKET") {
                    val totalScanned = listOf(b, y, c, o).filter { it.error == null }.sumOf { it.scanned }
                    val allScannedAssets = combined.map { it.first }
                    val gainers = allScannedAssets.count { moveValue(it) > 0.0 }
                    val losers = allScannedAssets.count { moveValue(it) < 0.0 }
                    val breadth = LinearLayout(context).apply {
                        orientation = LinearLayout.HORIZONTAL
                        setPadding(dp(10), dp(12), dp(10), dp(12))
                        background = rounded(Color.rgb(20, 30, 62), 13)
                    }
                    listOf("Всего" to totalScanned.toString(),
                        "Рост ↑" to gainers.toString(),
                        "Падение ↓" to losers.toString()).forEachIndexed { index, item ->
                        val cell = LinearLayout(context).apply {
                            orientation = LinearLayout.VERTICAL
                            layoutParams = LinearLayout.LayoutParams(0, -2, 1f)
                        }
                        cell.addView(label(item.first, 11f, muted, true))
                        cell.addView(label(item.second, 20f,
                            when (index) { 1 -> Color.rgb(25, 220, 125); 2 -> Color.rgb(245, 75, 95); else -> Color.WHITE }, true))
                        breadth.addView(cell)
                    }
                    content.addView(breadth)
                    content.addView(label("Автоматическое обновление каждые 60 секунд", 11f, muted))
                    val exchangeRow = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                    listOf("ALL" to "Все", "Binance" to "Binance", "Bybit" to "Bybit",
                        "Coinbase Exchange" to "Coinbase", "OKX" to "OKX").forEach { (key, title) ->
                        val filter = Button(context).apply {
                            text = title
                            textSize = 10f
                            buttonStyle(this, if (marketExchangeFilter == key) purple else Color.rgb(28, 38, 69))
                            layoutParams = LinearLayout.LayoutParams(0, dp(40), 1f).apply { marginEnd = dp(3) }
                            setOnClickListener { marketExchangeFilter = key; render() }
                        }
                        exchangeRow.addView(filter)
                    }
                    content.addView(exchangeRow)
                    val moveRow = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                    listOf("ALL" to "Все", "UP" to "Рост ↑", "DOWN" to "Падение ↓").forEach { (key, title) ->
                        val filter = Button(context).apply {
                            text = title
                            textSize = 11f
                            buttonStyle(this, if (marketMoveFilter == key) blue else Color.rgb(28, 38, 69))
                            layoutParams = LinearLayout.LayoutParams(0, dp(40), 1f).apply { marginEnd = dp(4) }
                            setOnClickListener { marketMoveFilter = key; render() }
                        }
                        moveRow.addView(filter)
                    }
                    content.addView(moveRow)
                    content.addView(label("ПАРА / БИРЖА        ЦЕНА          24 Ч.       ОЦЕНКА", 10f, muted, true))
                }
                var shown = when (selectedTab) { "HOME" -> ranked.take(3); "ANALYSIS" -> ranked.take(30); else -> ranked }
                if (selectedTab == "MARKET") {
                    shown = shown.filter { (_, ex) ->
                        marketExchangeFilter == "ALL" || ex.exchange.equals(marketExchangeFilter, true)
                    }.filter { (coin, _) ->
                        when (marketMoveFilter) { "UP" -> moveValue(coin) > 0; "DOWN" -> moveValue(coin) < 0; else -> true }
                    }.sortedByDescending { (coin, _) -> if (marketMoveFilter == "DOWN") -moveValue(coin) else moveValue(coin) }
                }
                if (shown.isEmpty()) content.addView(label("Нет доступных результатов. Проверьте соединение.", 14f, muted))
                shown.forEach { (coin, exchange) -> coinCard(coin, exchange.exchange, exchange, maxRisk) }
                if (selectedTab == "HOME") {
                    val remote = serverPaperState
                    val paper = LinearLayout(context).apply {
                        orientation = LinearLayout.VERTICAL
                        setPadding(dp(14), dp(14), dp(14), dp(14))
                        background = rounded(Color.rgb(13, 25, 55), 16)
                    }
                    val paperTitle = LinearLayout(context).apply {
                        orientation = LinearLayout.HORIZONTAL
                        gravity = android.view.Gravity.CENTER_VERTICAL
                    }
                    paperTitle.addView(label("▣  PAPER TRADING", 16f, Color.WHITE, true),
                        LinearLayout.LayoutParams(0, -2, 1f))
                    val enabled = remote?.optBoolean("enabled", false) ?: false
                    paperTitle.addView(label(
                        if (enabled) "● АВТО-ТОРГОВЛЯ: АКТИВНА" else "● АВТО-ТОРГОВЛЯ: ОСТАНОВЛЕНА",
                        9f, if (enabled) Color.rgb(20, 220, 120) else muted, true
                    ))
                    paper.addView(paperTitle)
                    val cash = remote?.optDouble("cash_usdt", 1000.0) ?: 1000.0
                    val equity = remote?.optDouble("equity_usdt", 1000.0) ?: 1000.0
                    val count = remote?.optJSONArray("open_positions")?.length() ?: 0
                    val metrics = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                    listOf("Баланс" to "$" + formatNumber(cash, 2),
                        "Прибыль (общая)" to (if (equity >= 1000) "+" else "") + "$" + formatNumber(equity - 1000, 2),
                        "Открытых позиций" to "$count / 3").forEach { pair ->
                        val tile = LinearLayout(context).apply {
                            orientation = LinearLayout.VERTICAL
                            setPadding(dp(7), dp(9), dp(5), dp(9))
                            background = rounded(Color.rgb(25, 37, 75), 10)
                            layoutParams = LinearLayout.LayoutParams(0, dp(70), 1f).apply { marginEnd = dp(5) }
                        }
                        tile.addView(label(pair.first, 8f, muted, true))
                        tile.addView(label(pair.second, 12f, Color.WHITE, true))
                        metrics.addView(tile)
                    }
                    paper.addView(metrics)
                    val controls = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
                    val scanButton = Button(context).apply {
                        text = "Сканировать"
                        textSize = 10f
                        buttonStyle(this, blue)
                        layoutParams = LinearLayout.LayoutParams(0, dp(48), 1f).apply { marginEnd = dp(5) }
                        setOnClickListener { refresh.performClick() }
                    }
                    val stopButton = Button(context).apply {
                        text = if (enabled) "Экстренный стоп" else "Запустить авто"
                        textSize = 10f
                        buttonStyle(this, if (enabled) Color.rgb(195, 25, 48) else Color.rgb(20, 145, 90))
                        layoutParams = LinearLayout.LayoutParams(0, dp(48), 1f)
                        isEnabled = remote != null && !serverPaperLoading &&
                            (enabled || remote.optBoolean("storage_persistent_configured", false))
                        setOnClickListener { togglePaperServer(!enabled) }
                    }
                    controls.addView(scanButton)
                    controls.addView(stopButton)
                    paper.addView(controls)
                    val positions = remote?.optJSONArray("open_positions") ?: JSONArray()
                    for (i in 0 until positions.length()) {
                        val p = positions.optJSONObject(i) ?: continue
                        paper.addView(label(
                            p.optString("symbol") + " • " + p.optString("exchange") +
                                " • P/L " + formatNumber(p.optDouble("unrealized_pnl_usdt"), 2) + " USDT",
                            11f, dark, true
                        ))
                    }
                    content.addView(paper)
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
                    result.assets.sortedByDescending { it.optInt("score", 0) }
                        .forEach { coinCard(it, result.exchange, result, maxRisk) }
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
            "Экран обновляет рыночные данные каждые 60 секунд; серверная виртуальная торговля работает независимо от открытого экрана.",
            11f, muted
        ))
        val handler = Handler(Looper.getMainLooper())
        val task = object : Runnable {
            override fun run() {
                if (!section.isAttachedToWindow) return
                loadAll()
                handler.postDelayed(this, 60L * 1000L)
            }
        }
        section.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(view: View) {
                handler.removeCallbacks(task)
                handler.postDelayed(task, 60L * 1000L)
            }
            override fun onViewDetachedFromWindow(view: View) {
                handler.removeCallbacks(task)
            }
        })
        loadAll()
        return section
    }
}
