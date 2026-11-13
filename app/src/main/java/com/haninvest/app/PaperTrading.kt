package com.haninvest.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class PaperTrading(private val context: Context) {
    private val prefs = context.getSharedPreferences("han_paper_trading", Context.MODE_PRIVATE)
    private val blue = Color.rgb(55, 105, 245)
    private val dark = Color.rgb(30, 42, 65)
    private val green = Color.rgb(25, 160, 105)
    private val red = Color.rgb(205, 65, 65)

    private fun money(v: Double) = String.format(Locale.US, "$%,.2f", v)
    private fun readArray(key: String) = try { JSONArray(prefs.getString(key, "[]")) } catch (_: Exception) { JSONArray() }
    private fun saveArray(key: String, value: JSONArray) { prefs.edit().putString(key, value.toString()).apply() }

    private fun balance(): Double = prefs.getFloat("cash", 1000f).toDouble()
    private fun positions() = readArray("positions")
    private fun history() = readArray("history")

    private fun assetBlockKey(exchange: String, symbol: String): String {
        val venue = if (exchange.contains("Bybit", ignoreCase = true)) "Bybit" else "Binance"
        return venue + ":" + symbol.trim().uppercase(Locale.US)
    }

    private fun blockAssetAfterLoss(exchange: String, symbol: String, entry: Double, exit: Double): Boolean {
        if (entry <= 0.0 || exit <= 0.0) return false
        val lossPercent = ((exit - entry) / entry) * 100.0
        if (lossPercent > -2.0) return false
        val blocked = prefs.getStringSet("paper_loss_blocked_assets", emptySet())
            ?.toMutableSet() ?: mutableSetOf()
        val added = blocked.add(assetBlockKey(exchange, symbol))
        if (added) prefs.edit().putStringSet("paper_loss_blocked_assets", blocked).apply()
        return added
    }

    private fun blockedLossAssets(): Set<String> =
        prefs.getStringSet("paper_loss_blocked_assets", emptySet())?.toSet() ?: emptySet()

    // Получает реальные публичные Spot-котировки обеих бирж и обновляет только уже открытые PAPER-позиции.
    // При недоступности одной биржи её старые цены сохраняются; данные не подменяются нулями.
    fun refreshOpenPositionsFromExchanges(): String {
        val open = positions()
        if (open.length() == 0) return "Нет открытых PAPER-позиций для обновления."

        var needBinance = false
        var needBybit = false
        for (i in 0 until open.length()) {
            val exchangeName = open.optJSONObject(i)?.optString("exchange", "Binance Spot") ?: "Binance Spot"
            if (exchangeName.contains("Bybit", ignoreCase = true)) {
                needBybit = true
            } else {
                needBinance = true
            }
        }
        val prices = mutableMapOf<String, MutableMap<String, Double>>()
        val errors = mutableListOf<String>()

        fun fetchText(address: String): String {
            val connection = URL(address).openConnection() as HttpURLConnection
            return try {
                connection.requestMethod = "GET"
                connection.connectTimeout = 12000
                connection.readTimeout = 20000
                connection.setRequestProperty("Accept", "application/json")
                connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.7")
                if (connection.responseCode !in 200..299) {
                    throw IllegalStateException("HTTP ${connection.responseCode}")
                }
                connection.inputStream.bufferedReader().use { it.readText() }
            } finally {
                connection.disconnect()
            }
        }

        if (needBinance) {
            try {
                val list = JSONArray(fetchText("https://api.binance.com/api/v3/ticker/price"))
                val map = mutableMapOf<String, Double>()
                for (i in 0 until list.length()) {
                    val item = list.optJSONObject(i) ?: continue
                    val symbol = item.optString("symbol")
                    val price = item.optString("price").toDoubleOrNull() ?: continue
                    if (symbol.isNotBlank() && price > 0.0) map[symbol] = price
                }
                prices["Binance"] = map
            } catch (e: Exception) {
                errors.add("Binance: ${e.message ?: "нет данных"}")
            }
        }

        if (needBybit) {
            try {
                val response = JSONObject(fetchText("https://api.bybit.com/v5/market/tickers?category=spot"))
                if (response.optInt("retCode", -1) != 0) {
                    throw IllegalStateException(response.optString("retMsg", "ошибка API"))
                }
                val list = response.optJSONObject("result")?.optJSONArray("list") ?: JSONArray()
                val map = mutableMapOf<String, Double>()
                for (i in 0 until list.length()) {
                    val item = list.optJSONObject(i) ?: continue
                    val symbol = item.optString("symbol")
                    val price = item.optString("lastPrice").toDoubleOrNull() ?: continue
                    if (symbol.isNotBlank() && price > 0.0) map[symbol] = price
                }
                prices["Bybit"] = map
            } catch (e: Exception) {
                errors.add("Bybit: ${e.message ?: "нет данных"}")
            }
        }

        var cash = balance()
        val remaining = JSONArray()
        val log = history()
        val now = System.currentTimeMillis()
        var updated = 0
        var closed = 0
        for (i in 0 until open.length()) {
            val position = open.optJSONObject(i) ?: continue
            val exchange = position.optString("exchange", "Binance Spot")
            val symbol = position.optString("symbol")
            val exchangeKey = if (exchange.contains("Bybit", true)) "Bybit" else "Binance"
            val price = prices[exchangeKey]?.get(symbol)
            if (price == null || price <= 0.0 || price.isNaN()) {
                remaining.put(position)
                continue
            }

            updated++
            position.put("last_price", price)
            position.put("last_updated", now)
            val entry = position.optDouble("entry")
            val quantity = position.optDouble("quantity")
            val stop = position.optDouble("stop")
            val reason = when {
                stop > 0.0 && price <= stop -> "STOP LOSS"
                price >= entry * 1.10 -> "TAKE PROFIT +10%"
                else -> ""
            }
            if (reason.isNotEmpty()) {
                blockAssetAfterLoss(exchange, symbol, entry, price)
                cash += quantity * price
                log.put(JSONObject()
                    .put("type", "SELL")
                    .put("symbol", symbol)
                    .put("exchange", exchange)
                    .put("price", price)
                    .put("quantity", quantity)
                    .put("pnl", (price - entry) * quantity)
                    .put("reason", reason)
                    .put("time", now))
                closed++
            } else {
                remaining.put(position)
            }
        }

        prefs.edit().putFloat("cash", cash.toFloat()).apply()
        saveArray("positions", remaining)
        saveArray("history", log)
        val errorText = if (errors.isEmpty()) "" else " Ошибки: " + errors.joinToString("; ")
        return "Котировки бирж обновлены: $updated из ${open.length()} позиций; закрыто по Stop/Take: $closed.$errorText"
    }

    fun processScan(json: JSONObject): String {
        var cash = balance()
        val open = positions()
        val log = history()
        val assets = json.optJSONArray("assets") ?: throw Exception("Сканер не вернул список активов")
        val now = System.currentTimeMillis()

        // Сначала проверяем уже открытые виртуальные позиции.
        val remaining = JSONArray()
        for (i in 0 until open.length()) {
            val p = open.getJSONObject(i)
            val symbol = p.optString("symbol")
            // Старые позиции без exchange считаем Binance, поскольку прежний PAPER-сканер был Binance-only.
            val exchange = p.optString("exchange", "Binance Spot")
            val coin = (0 until assets.length()).mapNotNull { assets.optJSONObject(it) }
                .firstOrNull {
                    it.optString("symbol") == symbol &&
                    it.optString("exchange", "Binance Spot") == exchange
                }
            val price = coin?.optDouble("price", Double.NaN) ?: Double.NaN
            val score = coin?.optInt("score", 0) ?: 0
            val entry = p.optDouble("entry")
            val qty = p.optDouble("quantity")
            val stop = p.optDouble("stop")
            val take = entry * 1.10
            val reason = when {
                !price.isNaN() && price <= stop -> "STOP LOSS"
                !price.isNaN() && price >= take -> "TAKE PROFIT +10%"
                coin != null && score < 83 -> "Сигнал ниже 83"
                else -> ""
            }
            if (reason.isNotEmpty() && !price.isNaN()) {
                blockAssetAfterLoss(exchange, symbol, entry, price)
                val proceeds = qty * price
                cash += proceeds
                log.put(JSONObject().put("type", "SELL").put("symbol", symbol)
                    .put("exchange", exchange).put("price", price).put("quantity", qty)
                    .put("pnl", (price - entry) * qty).put("reason", reason).put("time", now))
            } else {
                if (!price.isNaN()) p.put("last_price", price)
                remaining.put(p)
            }
        }

        // После закрытия позиции с убытком 2% или больше актив навсегда блокируется
        // от повторной PAPER-покупки, независимо от последующего score.
        val blocked = blockedLossAssets()
        var skippedAfterLoss = 0

        // Покупаем активы со score >= 83, если они не находятся в списке блокировки.
        // На каждую новую позицию выделяется фиксированно до 100 USDT.
        val held = mutableSetOf<String>()
        for (index in 0 until remaining.length()) {
            val position = remaining.optJSONObject(index) ?: continue
            held.add(position.optString("exchange", "Binance Spot") + ":" + position.optString("symbol"))
        }
        for (i in 0 until assets.length()) {
            val coin = assets.optJSONObject(i) ?: continue
            val symbol = coin.optString("symbol")
            val exchange = coin.optString("exchange")
            val price = coin.optDouble("price", Double.NaN)
            val score = coin.optInt("score", 0)
            val positionKey = "$exchange:$symbol"
            // score >= 83 недостаточно, если актив ранее закрылся с убытком >= 2%.
            if (assetBlockKey(exchange, symbol) in blocked) {
                skippedAfterLoss++
                continue
            }
            if (symbol.isBlank() || exchange.isBlank() || positionKey in held ||
                price <= 0 || price.isNaN() || score < 83) continue
            val spend = minOf(100.0, cash)
            if (spend < 5.0) continue
            val stopValue = coin.optDouble("stop_loss", Double.NaN)
            val stop = if (!stopValue.isNaN() && stopValue > 0 && stopValue < price) stopValue else price * 0.97
            val qty = spend / price
            cash -= spend
            remaining.put(JSONObject().put("symbol", symbol).put("exchange", exchange).put("entry", price)
                .put("last_price", price).put("stop", stop).put("quantity", qty)
                .put("cost", spend).put("opened_at", now).put("score", score))
            held.add(positionKey)
            log.put(JSONObject().put("type", "BUY").put("symbol", symbol)
                .put("exchange", exchange).put("price", price).put("quantity", qty).put("cost", spend)
                .put("score", score).put("time", now))
        }
        prefs.edit().putFloat("cash", cash.toFloat()).putLong("last_scan", now).apply()
        saveArray("positions", remaining)
        saveArray("history", log)
        return "Сканирование обработано. Найдено активов: ${assets.length()}. Заблокировано после убытка 2%: ${blocked.size}; пропущено сейчас: ${skippedAfterLoss}."
    }

    private fun remainingValue(open: JSONArray): Double {
        var total = 0.0
        for (i in 0 until open.length()) {
            val p = open.optJSONObject(i) ?: continue
            total += p.optDouble("quantity") * p.optDouble("last_price", p.optDouble("entry"))
        }
        return total
    }

    private fun render(target: TextView) {
        val open = positions()
        val log = history()
        val cash = balance()
        val invested = remainingValue(open)
        val equity = cash + invested
        val pnl = logPnl(log) + openPnl(open)
        val out = StringBuilder()
        out.append("ДЕНЬГИ: ").append(money(cash)).append("\n")
        out.append("В открытых позициях: ").append(money(invested)).append("\n")
        out.append("ОБЩИЙ КАПИТАЛ: ").append(money(equity)).append("\n")
        out.append("Общий результат: ").append(if (pnl >= 0) "+" else "").append(money(pnl)).append("\n")
        out.append("Открытых позиций: ").append(open.length()).append("\n\n")
        out.append("ОТКРЫТЫЕ ПОЗИЦИИ\n")
        if (open.length() == 0) out.append("Пока нет виртуальных покупок.\n")
        for (i in 0 until open.length()) {
            val p = open.getJSONObject(i)
            val price = p.optDouble("last_price", p.optDouble("entry"))
            out.append(p.optString("exchange", "Binance Spot")).append(" ").append(p.optString("symbol")).append(" | вход ").append(money(p.optDouble("entry")))
                .append(" | сейчас ").append(money(price))
                .append(" | P/L ").append(money((price-p.optDouble("entry"))*p.optDouble("quantity")))
                .append("\n")
        }
        out.append("\nПОСЛЕДНИЕ СДЕЛКИ\n")
        if (log.length() == 0) out.append("История пока пустая.\n")
        for (i in (log.length()-1 downTo maxOf(0, log.length()-10))) {
            val t = log.optJSONObject(i) ?: continue
            out.append(t.optString("type")).append(" ").append(t.optString("exchange", "Binance Spot"))
                .append(" ").append(t.optString("symbol")).append(" @ ").append(money(t.optDouble("price")))
            if (t.optString("type") == "SELL") out.append(" | ").append(t.optString("reason"))
                .append(" | P/L ").append(money(t.optDouble("pnl")))
            out.append("\n")
        }
        target.text = out.toString()
    }

    private fun logPnl(log: JSONArray): Double {
        var total = 0.0
        for (i in 0 until log.length()) total += log.optJSONObject(i)?.optDouble("pnl", 0.0) ?: 0.0
        return total
    }
    private fun openPnl(open: JSONArray): Double {
        var total = 0.0
        for (i in 0 until open.length()) {
            val p = open.getJSONObject(i)
            total += (p.optDouble("last_price", p.optDouble("entry")) - p.optDouble("entry")) * p.optDouble("quantity")
        }
        return total
    }

    fun createView(): View {
        val box = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(18, 18, 18, 18)
            setBackgroundColor(Color.WHITE)
        }
        val title = TextView(context).apply {
            text = "PAPER TRADING — ВИРТУАЛЬНЫЙ СЧЁТ"
            textSize = 19f
            setTextColor(dark)
            setTypeface(null, Typeface.BOLD)
        }
        box.addView(title)
        val details = TextView(context).apply {
            textSize = 14f
            setTextColor(dark)
            setPadding(0, 12, 0, 12)
        }
        box.addView(details)
        val status = TextView(context).apply { text = "Режим симуляции. Реальные деньги не используются."; textSize = 12f; setTextColor(Color.GRAY) }
        box.addView(status)
        val scan = Button(context).apply {
            text = "СКАНИРОВАТЬ И ВЫПОЛНИТЬ PAPER-СДЕЛКИ"
            setTextColor(Color.WHITE)
            setBackgroundColor(blue)
            setOnClickListener {
                isEnabled = false
                status.text = "Получаем сигналы рынка..."
                Thread {
                    try {
                        val urls = listOf(
                            "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?limit=30&universe_limit=100&min_volume=1000000&risk_pct=1&capital_usdt=1000",
                            "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000"
                        )
                        val combined = JSONObject()
                        val combinedAssets = JSONArray()
                        val errors = mutableListOf<String>()
                        for (url in urls) {
                            try {
                                val conn = URL(url).openConnection() as HttpURLConnection
                                val body = try {
                                    conn.connectTimeout = 20000
                                    conn.readTimeout = 45000
                                    if (conn.responseCode !in 200..299) throw Exception("HTTP ${conn.responseCode}")
                                    conn.inputStream.bufferedReader().use { it.readText() }
                                } finally { conn.disconnect() }
                                val response = JSONObject(body)
                                val list = response.optJSONArray("assets") ?: continue
                                for (i in 0 until list.length()) {
                                    val coin = list.optJSONObject(i) ?: continue
                                    if (!coin.has("exchange")) {
                                        coin.put("exchange", if (url.contains("/bybit/")) "Bybit Spot" else "Binance Spot")
                                    }
                                    combinedAssets.put(coin)
                                }
                            } catch (e: Exception) {
                                errors.add(e.message ?: "ошибка соединения")
                            }
                        }
                        if (combinedAssets.length() == 0) throw Exception("Обе биржи недоступны: " + errors.joinToString("; "))
                        combined.put("assets", combinedAssets)
                        val scanMessage = processScan(combined) + " Binance + Bybit."
                        val quoteMessage = refreshOpenPositionsFromExchanges()
                        val message = scanMessage + "\n" + quoteMessage
                        
                        (context as? android.app.Activity)?.runOnUiThread {
                            status.text = message
                            isEnabled = true
                            render(details)
                        }
                    } catch (e: Exception) {
                        (context as? android.app.Activity)?.runOnUiThread {
                            status.text = "Ошибка сканирования: ${e.message}"
                            isEnabled = true
                        }
                    }
                }.start()
            }
        }
        box.addView(scan)
        val refreshQuotes = Button(context).apply {
            text = "ОБНОВИТЬ ЦЕНЫ PAPER С BINANCE + BYBIT"
            setOnClickListener {
                isEnabled = false
                status.text = "Получаем актуальные котировки напрямую с бирж..."
                Thread {
                    val message = try {
                        refreshOpenPositionsFromExchanges()
                    } catch (e: Exception) {
                        "Не удалось обновить котировки: ${e.message}"
                    }
                    (context as? android.app.Activity)?.runOnUiThread {
                        status.text = message
                        isEnabled = true
                        render(details)
                    }
                }.start()
            }
        }
        box.addView(refreshQuotes)
        val reset = Button(context).apply {
            text = "СБРОСИТЬ СЧЁТ ДО \\$1 000"
            setOnClickListener {
                prefs.edit().putFloat("cash", 1000f).putString("positions", "[]")
                    .putString("history", "[]").putLong("last_scan", 0L).apply()
                status.text = "Виртуальный счёт сброшен до $1 000."
                render(details)
            }
        }
        box.addView(reset)
        if (!prefs.contains("cash")) prefs.edit().putFloat("cash", 1000f).apply()
        render(details)
        return box
    }
}
