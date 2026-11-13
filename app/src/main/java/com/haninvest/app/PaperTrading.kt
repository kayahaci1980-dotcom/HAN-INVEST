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
    private val paperFeeRate = 0.001 // 0.1% estimated taker fee per side.
    @Volatile private var scanLimit = 100

    private fun money(v: Double) = String.format(Locale.US, "$%,.2f", v)
    private fun readArray(key: String) = try { JSONArray(prefs.getString(key, "[]")) } catch (_: Exception) { JSONArray() }
    private fun saveArray(key: String, value: JSONArray) { prefs.edit().putString(key, value.toString()).apply() }

    private fun balance(): Double {
        val precise = prefs.getString("cash_usdt", null)?.toDoubleOrNull()
        return precise ?: prefs.getFloat("cash", 1000f).toDouble()
    }
    private fun saveBalance(value: Double) {
        prefs.edit().putString("cash_usdt", value.toString()).remove("cash").apply()
    }
    fun isTradingPaused(): Boolean = prefs.getBoolean("paper_trading_paused", false)
    fun setTradingPaused(paused: Boolean) { prefs.edit().putBoolean("paper_trading_paused", paused).apply() }
    private fun positions() = readArray("positions")
    private fun history() = readArray("history")

    private fun assetBlockKey(exchange: String, symbol: String): String =
        "Bybit:" + symbol.trim().uppercase(Locale.US)

    private val lossBlockDurationMs = 2L * 60L * 60L * 1000L

    // Храним время окончания блокировки для каждой пары биржа:монета.
    private fun readLossBlocks(): JSONObject = try {
        JSONObject(prefs.getString("paper_loss_blocked_until", "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    private fun direction(position: JSONObject): String =
        if (position.optString("direction", "LONG").equals("SHORT", true)) "SHORT" else "LONG"

    private fun grossPnl(position: JSONObject, price: Double): Double {
        val delta = if (direction(position) == "SHORT") position.optDouble("entry") - price
                   else price - position.optDouble("entry")
        return delta * position.optDouble("quantity")
    }

    private fun netOpenPnl(position: JSONObject, price: Double): Double =
        grossPnl(position, price) - position.optDouble("entry_fee", 0.0) -
            position.optDouble("quantity") * price * paperFeeRate

    private fun blockAssetAfterLoss(exchange: String, symbol: String, entry: Double, exit: Double, side: String = "LONG"): Boolean {
        if (entry <= 0.0 || exit <= 0.0) return false
        val move = if (side == "SHORT") (entry - exit) / entry else (exit - entry) / entry
        if ((move - 2.0 * paperFeeRate) * 100.0 > -2.0) return false
        val blocks = readLossBlocks()
        blocks.put(assetBlockKey(exchange, symbol), System.currentTimeMillis() + lossBlockDurationMs)
        prefs.edit().putString("paper_loss_blocked_until", blocks.toString()).apply()
        return true
    }

    // Общая защита прибыли для LONG и SHORT.
    private fun shouldCloseAfterProfitGiveback(position: JSONObject, price: Double): Boolean {
        if (position.optDouble("quantity") <= 0.0 || price <= 0.0 || !price.isFinite()) return false
        val currentProfit = netOpenPnl(position, price)
        val peakProfit = maxOf(position.optDouble("peak_profit_usdt", 0.0), currentProfit)
        position.put("peak_profit_usdt", peakProfit)
        return peakProfit >= 2.0 && currentProfit <= peakProfit * 0.5
    }

    private fun blockAssetForCooldown(exchange: String, symbol: String) {
        val blocks = readLossBlocks()
        blocks.put(assetBlockKey(exchange, symbol), System.currentTimeMillis() + lossBlockDurationMs)
        prefs.edit().putString("paper_loss_blocked_until", blocks.toString()).apply()
    }

    private fun blockedLossAssets(): Set<String> {
        val now = System.currentTimeMillis()
        val blocks = readLossBlocks()
        val active = mutableSetOf<String>()
        val expired = mutableListOf<String>()
        val iterator = blocks.keys()
        while (iterator.hasNext()) {
            val key = iterator.next()
            if (blocks.optLong(key, 0L) > now) active.add(key) else expired.add(key)
        }
        for (key in expired) blocks.remove(key)

        // Переносим ранее сохранённые постоянные блокировки на 2 часа с момента обновления.
        if (!prefs.getBoolean("paper_loss_blocks_migrated", false)) {
            val legacy = prefs.getStringSet("paper_loss_blocked_assets", emptySet())?.toSet() ?: emptySet()
            for (key in legacy) {
                if (!active.contains(key)) {
                    blocks.put(key, now + lossBlockDurationMs)
                    active.add(key)
                }
            }
            prefs.edit().putBoolean("paper_loss_blocks_migrated", true).remove("paper_loss_blocked_assets").apply()
        }
        prefs.edit().putString("paper_loss_blocked_until", blocks.toString()).apply()
        return active
    }

    // Обновляем PAPER-позиции только по публичным котировкам Bybit Spot.
    // Реальные ордера не отправляются.
    fun refreshOpenPositionsFromExchanges(): String {
        val open = positions()
        if (open.length() == 0) return "Нет открытых PAPER-позиций для обновления."

        val responseText = run {
            val connection = URL("https://api.bybit.com/v5/market/tickers?category=spot")
                .openConnection() as HttpURLConnection
            try {
                connection.requestMethod = "GET"
                connection.connectTimeout = 12000
                connection.readTimeout = 20000
                connection.setRequestProperty("Accept", "application/json")
                connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/3.0")
                if (connection.responseCode !in 200..299) throw IllegalStateException("Bybit HTTP ${connection.responseCode}")
                connection.inputStream.bufferedReader().use { it.readText() }
            } finally { connection.disconnect() }
        }
        val response = JSONObject(responseText)
        if (response.optInt("retCode", -1) != 0) throw IllegalStateException(response.optString("retMsg", "Bybit API error"))
        val list = response.optJSONObject("result")?.optJSONArray("list") ?: JSONArray()
        val prices = mutableMapOf<String, Double>()
        for (i in 0 until list.length()) {
            val item = list.optJSONObject(i) ?: continue
            val symbol = item.optString("symbol")
            val price = item.optString("lastPrice").toDoubleOrNull() ?: continue
            if (symbol.isNotBlank() && price > 0.0 && price.isFinite()) prices[symbol] = price
        }

        var cash = balance()
        val remaining = JSONArray()
        val log = history()
        val now = System.currentTimeMillis()
        var updated = 0
        var closed = 0
        var legacy = 0
        for (i in 0 until open.length()) {
            val position = open.optJSONObject(i) ?: continue
            val exchange = position.optString("exchange", "Bybit Spot")
            if (!exchange.contains("Bybit", true)) {
                // Сохраняем старые позиции без удаления и без подмены цены другой биржей.
                remaining.put(position)
                legacy++
                continue
            }
            val symbol = position.optString("symbol")
            val price = prices[symbol]
            if (price == null) {
                remaining.put(position)
                continue
            }
            updated++
            position.put("last_price", price)
            position.put("last_updated", now)
            val entry = position.optDouble("entry")
            val quantity = position.optDouble("quantity")
            val stop = position.optDouble("stop")
            val target = position.optDouble("target")
            val side = direction(position)
            val profitGiveback = shouldCloseAfterProfitGiveback(position, price)
            val hitStop = stop > 0.0 && if (side == "SHORT") price >= stop else price <= stop
            val hitTarget = target > 0.0 && if (side == "SHORT") price <= target else price >= target
            val reason = when {
                isTradingPaused() -> ""
                profitGiveback -> "ЗАЩИТА ПРИБЫЛИ: ОТДАТЬ 50% ОТ МАКСИМУМА"
                hitStop -> "STOP LOSS"
                hitTarget -> "TAKE PROFIT"
                else -> ""
            }
            if (reason.isNotEmpty()) {
                if (profitGiveback) blockAssetForCooldown(exchange, symbol)
                else blockAssetAfterLoss(exchange, symbol, entry, price, side)
                val fee = quantity * price * paperFeeRate
                val cost = position.optDouble("cost", quantity * entry)
                val pnl = netOpenPnl(position, price)
                if (side == "SHORT") cash += cost + grossPnl(position, price) - fee
                else cash += quantity * price * (1.0 - paperFeeRate)
                log.put(JSONObject().put("type", if (side == "SHORT") "COVER" else "SELL")
                    .put("direction", side).put("symbol", symbol).put("exchange", exchange)
                    .put("price", price).put("quantity", quantity).put("fee", fee)
                    .put("pnl", pnl).put("reason", reason).put("time", now))
                closed++
            } else remaining.put(position)
        }
        saveBalance(cash)
        saveArray("positions", remaining)
        saveArray("history", log)
        val legacyNote = if (legacy > 0) " Старых позиций других бирж сохранено без изменений: $legacy." else ""
        return "Bybit: котировки обновлены для $updated из ${open.length()} PAPER-позиций; закрыто по защите прибыли/Stop/Take: $closed.$legacyNote"
    }

    fun processScan(json: JSONObject): String {
        if (isTradingPaused()) return "ЭКСТРЕННЫЙ СТОП: автоматические сделки остановлены."
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
            val exchange = p.optString("exchange", "Bybit Spot")
            val coin = (0 until assets.length()).mapNotNull { assets.optJSONObject(it) }
                .firstOrNull {
                    it.optString("symbol") == symbol &&
                    it.optString("exchange", "Bybit Spot") == exchange
                }
            val price = coin?.optDouble("price", Double.NaN) ?: Double.NaN
            val score = coin?.let {
                if (direction(p) == "SHORT") it.optInt("short_score", it.optInt("score_short", it.optInt("shortScore", 0)))
                else it.optInt("score", 0)
            } ?: 0
            val entry = p.optDouble("entry")
            val qty = p.optDouble("quantity")
            val stop = p.optDouble("stop")
            val take = p.optDouble("target")
            val side = direction(p)
            val profitGiveback = !price.isNaN() && shouldCloseAfterProfitGiveback(p, price)
            val hitStop = !price.isNaN() && stop > 0.0 && if (side == "SHORT") price >= stop else price <= stop
            val hitTarget = !price.isNaN() && take > 0.0 && if (side == "SHORT") price <= take else price >= take
            val reason = when {
                profitGiveback -> "ЗАЩИТА ПРИБЫЛИ: ОТДАТЬ 50% ОТ МАКСИМУМА"
                hitStop -> "STOP LOSS"
                hitTarget -> "TAKE PROFIT"
                coin != null && score < 83 -> "Сигнал ниже 83"
                else -> ""
            }
            if (reason.isNotEmpty() && !price.isNaN()) {
                if (profitGiveback) blockAssetForCooldown(exchange, symbol)
                else blockAssetAfterLoss(exchange, symbol, entry, price, side)
                val fee = qty * price * paperFeeRate
                val cost = p.optDouble("cost", qty * entry)
                val pnl = netOpenPnl(p, price)
                if (side == "SHORT") cash += cost + grossPnl(p, price) - fee
                else cash += qty * price * (1.0 - paperFeeRate)
                log.put(JSONObject().put("type", if (side == "SHORT") "COVER" else "SELL")
                    .put("direction", side).put("symbol", symbol).put("exchange", exchange)
                    .put("price", price).put("quantity", qty).put("fee", fee)
                    .put("pnl", pnl).put("reason", reason).put("time", now))
            } else {
                if (!price.isNaN()) p.put("last_price", price)
                remaining.put(p)
            }
        }

        // После защиты прибыли повторная покупка этой монеты блокируется на 2 часа.
        // После убытка 2% или больше также действует блокировка на 2 часа.
        val blocked = blockedLossAssets()
        val held = mutableSetOf<String>()
        for (index in 0 until remaining.length()) {
            val position = remaining.optJSONObject(index) ?: continue
            held.add(position.optString("exchange", "Bybit Spot") + ":" + position.optString("symbol"))
        }

        // Диагностика: для каждого актива фиксируем первую причину, по которой покупка пропущена.
        val reasons = linkedMapOf<String, Int>()
        fun skipped(reason: String) { reasons[reason] = (reasons[reason] ?: 0) + 1 }
        var bought = 0
        var qualified = 0

        for (i in 0 until assets.length()) {
            val coin = assets.optJSONObject(i) ?: continue
            val symbol = coin.optString("symbol")
            val exchange = coin.optString("exchange", "Bybit Spot")
            val price = coin.optDouble("price", Double.NaN)
            val longScore = coin.optInt("score", 0)
            val declaredSide = coin.optString("direction", coin.optString("signal", coin.optString("side", ""))).uppercase(Locale.US)
            val shortScore = when {
                coin.has("short_score") -> coin.optInt("short_score", 0)
                coin.has("score_short") -> coin.optInt("score_short", 0)
                coin.has("shortScore") -> coin.optInt("shortScore", 0)
                declaredSide.contains("SHORT") -> longScore
                else -> 0
            }
            val side = if (shortScore > 83 && (shortScore > longScore || declaredSide.contains("SHORT"))) "SHORT" else "LONG"
            val score = if (side == "SHORT") shortScore else longScore
            val positionKey = "$exchange:$symbol"

            // При оценке строго выше 83 баллов оценка становится главным сигналом:
            // объём, BTC-тренд и trade_ready не блокируют PAPER-вход; активная блокировка монеты учитывается отдельно.
            // Оставляем только технически обязательные условия исполнения: корректная пара/цена,
            // отсутствие уже открытой позиции по этой паре и наличие виртуального USDT.
            val reason = when {
                !exchange.contains("Bybit", true) -> "Доступна только биржа Bybit"
                symbol.isBlank() || price <= 0.0 || !price.isFinite() ->
                    "Нет корректной пары или цены"
                positionKey in held -> "Позиция уже открыта"
                assetBlockKey(exchange, symbol) in blocked -> "Монета заблокирована на 2 часа после закрытия"
                held.size >= 3 -> "Достигнут лимит: максимум 3 открытые позиции"
                score <= 83 -> if (side == "SHORT") "SHORT-оценка должна быть выше 83" else "Оценка должна быть выше 83"
                else -> ""
            }
            if (reason.isNotEmpty()) {
                skipped(reason)
                continue
            }

            qualified++
            val spend = minOf(100.0, cash)
            if (spend < 5.0) {
                skipped("Недостаточно свободного USDT (нужно минимум \$5)")
                continue
            }
            val stopValue = coin.optDouble(if (side == "SHORT") "short_stop_loss" else "stop_loss", Double.NaN)
            val targetValue = coin.optDouble(if (side == "SHORT") "short_take_profit" else "take_profit", Double.NaN)
            val stop = if (side == "SHORT") {
                if (!stopValue.isNaN() && stopValue > price) stopValue else price * 1.03
            } else {
                if (!stopValue.isNaN() && stopValue > 0 && stopValue < price) stopValue else price * 0.97
            }
            val target = if (side == "SHORT") {
                if (!targetValue.isNaN() && targetValue > 0 && targetValue < price) targetValue else price * 0.94
            } else {
                if (!targetValue.isNaN() && targetValue > price) targetValue else price * 1.06
            }
            val qty = if (side == "SHORT") spend / price else spend / (price * (1.0 + paperFeeRate))
            val entryFee = qty * price * paperFeeRate
            cash -= if (side == "SHORT") spend + entryFee else spend
            remaining.put(JSONObject().put("symbol", symbol).put("exchange", exchange).put("direction", side).put("entry", price)
                .put("last_price", price).put("stop", stop).put("target", target).put("quantity", qty)
                .put("cost", spend).put("entry_fee", entryFee)
                .put("peak_profit_usdt", 0.0).put("opened_at", now).put("score", score))
            held.add(positionKey)
            log.put(JSONObject().put("type", if (side == "SHORT") "SHORT" else "BUY")
                .put("direction", side).put("symbol", symbol)
                .put("exchange", exchange).put("price", price).put("quantity", qty).put("cost", spend)
                .put("fee", entryFee).put("score", score).put("time", now))
            bought++
        }
        saveBalance(cash)
        prefs.edit().putLong("last_scan", now).apply()
        saveArray("positions", remaining)
        saveArray("history", log)

        val reasonSummary = if (reasons.isEmpty()) {
            "Все проверенные активы прошли фильтры."
        } else {
            reasons.entries.sortedByDescending { it.value }.take(5)
                .joinToString("; ") { "${it.key}: ${it.value}" }
        }
        return "Сканирование: активов ${assets.length()}; куплено ${bought}; прошло все фильтры до проверки баланса ${qualified}.\nПочему пропущены: $reasonSummary."
    }

    private fun fetchManualSellPrice(exchange: String, symbol: String): Double {
        if (!exchange.contains("Bybit", true)) {
            throw IllegalStateException("В этой версии поддерживается только Bybit Spot")
        }
        val url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=$symbol"
        val connection = URL(url).openConnection() as HttpURLConnection
        val body = try {
            connection.connectTimeout = 10000
            connection.readTimeout = 15000
            if (connection.responseCode !in 200..299) throw Exception("Bybit HTTP ${connection.responseCode}")
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally { connection.disconnect() }
        val response = JSONObject(body)
        if (response.optInt("retCode", -1) != 0) throw Exception(response.optString("retMsg", "Ошибка Bybit"))
        return response.optJSONObject("result")?.optJSONArray("list")?.optJSONObject(0)
            ?.optString("lastPrice")?.toDoubleOrNull()
            ?: throw Exception("Нет цены Bybit для $symbol")
    }
    fun sellPosition(exchange: String, symbol: String): String {
        val open=positions()
        val pos=(0 until open.length()).mapNotNull{open.optJSONObject(it)}.firstOrNull{it.optString("exchange")==exchange && it.optString("symbol")==symbol} ?: return "Позиция уже закрыта."
        val price=fetchManualSellPrice(exchange,symbol); val entry=pos.optDouble("entry"); val qty=pos.optDouble("quantity")
        val side=direction(pos); val fee=qty*price*paperFeeRate; val cost=pos.optDouble("cost",qty*entry)
        val pnl=netOpenPnl(pos,price)
        val returnedCash=if(side=="SHORT") cost+grossPnl(pos,price)-fee else qty*price*(1.0-paperFeeRate)
        val remaining=JSONArray()
        for(i in 0 until open.length()){val item=open.optJSONObject(i)?:continue;if(!(item.optString("exchange")==exchange&&item.optString("symbol")==symbol))remaining.put(item)}
        val log=history()
        log.put(JSONObject().put("type",if(side=="SHORT")"COVER" else "SELL").put("direction",side).put("symbol",symbol).put("exchange",exchange).put("price",price).put("quantity",qty).put("fee",fee).put("pnl",pnl).put("reason","MANUAL CLOSE").put("time",System.currentTimeMillis()))
        blockAssetAfterLoss(exchange,symbol,entry,price,side)
        if (pos.optDouble("peak_profit_usdt",0.0)>=2.0 && pnl <= pos.optDouble("peak_profit_usdt")*0.5) blockAssetForCooldown(exchange,symbol)
        saveBalance(balance()+returnedCash)
        saveArray("positions",remaining);saveArray("history",log)
        return "Продано $symbol по "+money(price)+". P/L: "+money(pnl)+"."
    }

    private fun remainingValue(open: JSONArray): Double {
        var total = 0.0
        for (i in 0 until open.length()) {
            val p = open.optJSONObject(i) ?: continue
            val price=p.optDouble("last_price",p.optDouble("entry"))
            total += if(direction(p)=="SHORT") p.optDouble("cost")+grossPnl(p,price)-p.optDouble("quantity")*price*paperFeeRate
                     else p.optDouble("quantity")*price*(1.0-paperFeeRate)
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
            out.append(direction(p)).append(" ").append(p.optString("exchange", "Bybit Spot")).append(" ").append(p.optString("symbol")).append(" | вход ").append(money(p.optDouble("entry")))
                .append(" | сейчас ").append(money(price))
                .append(" | P/L ").append(money(netOpenPnl(p,price)))
                .append(" | SL ").append(money(p.optDouble("stop"))).append(" | TP ").append(money(p.optDouble("target")))
                .append("\n")
        }
        out.append("\nПОСЛЕДНИЕ СДЕЛКИ\n")
        if (log.length() == 0) out.append("История пока пустая.\n")
        for (i in (log.length()-1 downTo maxOf(0, log.length()-10))) {
            val t = log.optJSONObject(i) ?: continue
            out.append(t.optString("type")).append(" ").append(t.optString("exchange", "Bybit Spot"))
                .append(" ").append(t.optString("symbol")).append(" @ ").append(money(t.optDouble("price")))
            if (t.optString("type") == "SELL" || t.optString("type") == "COVER") out.append(" | ").append(t.optString("reason"))
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
            total += netOpenPnl(p,p.optDouble("last_price",p.optDouble("entry")))
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
        val sellActions = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
        box.addView(sellActions)
        fun showSellActions() {
            sellActions.removeAllViews()
            val heading = TextView(context).apply {
                text = "РУЧНАЯ ПРОДАЖА BYBIT — ДОСТУПНА ДАЖЕ ПРИ ЭКСТРЕННОМ СТОПЕ"
                textSize = 14f
                setTypeface(null, Typeface.BOLD)
                setTextColor(dark)
                setPadding(0, 10, 0, 8)
            }
            sellActions.addView(heading)
            val list = positions()
            if (list.length() == 0) {
                sellActions.addView(TextView(context).apply {
                    text = "Открытых Bybit-позиций нет. Кнопки продажи появятся после виртуальной покупки."
                    textSize = 12f
                    setTextColor(Color.GRAY)
                    setPadding(0, 0, 0, 10)
                })
                return
            }
            for(i in 0 until list.length()) {
                val pos=list.optJSONObject(i)?:continue
                val ex=pos.optString("exchange","Bybit Spot"); val sym=pos.optString("symbol")
                if (!ex.contains("Bybit", true)) {
                    sellActions.addView(TextView(context).apply {
                        text = "Старая позиция $sym ($ex) сохранена. Автоматическое управление биржами кроме Bybit отключено."
                        textSize = 12f
                        setTextColor(Color.GRAY)
                    })
                    continue
                }
                sellActions.addView(Button(context).apply {
                    text=if(direction(pos)=="SHORT") "ЗАКРЫТЬ SHORT $sym — $ex" else "ПРОДАТЬ $sym — $ex";setTextColor(Color.WHITE);setBackgroundColor(red)
                    setOnClickListener {
                        isEnabled=false;status.text="Получаем текущую цену для продажи..."
                        Thread {
                            val result=try{sellPosition(ex,sym)}catch(e:Exception){"Продажа не выполнена: "+e.message+". Позиция сохранена."}
                            (context as? android.app.Activity)?.runOnUiThread{status.text=result;render(details);showSellActions()}
                        }.start()
                    }
                })
            }
        }
        val scan = Button(context).apply {
            text = "СКАНИРОВАТЬ 100 ПАР И ВЫПОЛНИТЬ PAPER-СДЕЛКИ"
            setTextColor(Color.WHITE)
            setBackgroundColor(blue)
            setOnClickListener {
                isEnabled = false
                status.text = "Получаем сигналы рынка..."
                Thread {
                    try {
                        val urls = listOf(
                            "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=$scanLimit&min_volume=1000000"
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
                                val btcAllowed = response.optJSONObject("btc_market_filter")
                                    ?.optBoolean("buy_allowed", false) == true
                                val defaultExchange = "Bybit Spot"
                                for (i in 0 until list.length()) {
                                    val coin = list.optJSONObject(i) ?: continue
                                    if (!coin.has("exchange")) coin.put("exchange", defaultExchange)
                                    coin.put("btc_buy_allowed", btcAllowed)
                                    combinedAssets.put(coin)
                                }
                            } catch (e: Exception) {
                                errors.add(e.message ?: "ошибка соединения")
                            }
                        }
                        if (combinedAssets.length() == 0) throw Exception("Bybit Spot временно недоступна: " + errors.joinToString("; "))
                        combined.put("assets", combinedAssets)
                        Thread.sleep(2000)
                        val scanMessage = processScan(combined) + " Только Bybit Spot."
                        val quoteMessage = refreshOpenPositionsFromExchanges()
                        val message = scanMessage + "\n" + quoteMessage
                        
                        (context as? android.app.Activity)?.runOnUiThread {
                            status.text = message
                            isEnabled = true
                            render(details)
                            showSellActions()
                        }
                    } catch (e: Exception) {
                        (context as? android.app.Activity)?.runOnUiThread {
                            status.text = "Ошибка сканирования: ${e.message}"
                            isEnabled = true
                            render(details)
                            showSellActions()
                        }
                    }
                }.start()
            }
        }
        box.addView(scan)
        val expandedScan = Button(context).apply {
            text = "РАСШИРЕННЫЙ ПОИСК — ДО 300 ПАР"
            setTextColor(Color.WHITE)
            setBackgroundColor(green)
            setOnClickListener {
                scanLimit = 300
                scan.text = "РАСШИРЕННЫЙ ПОИСК: 300 ПАР — PAPER-СДЕЛКИ"
                scan.performClick()
            }
        }
        scan.setOnClickListener {
            scanLimit = if (scan.text.toString().contains("РАСШИРЕННЫЙ")) 300 else 100
            scan.text = "СКАНИРОВАНИЕ ${scanLimit} ПАР — PAPER-СДЕЛКИ"
            scan.isEnabled = false
            status.text = "Сканируем до ${scanLimit} наиболее ликвидных пар Bybit Spot..."
            Thread {
                try {
                    val url = "https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=$scanLimit&min_volume=1000000"
                    val conn = URL(url).openConnection() as HttpURLConnection
                    val body = try {
                        conn.connectTimeout = 20000; conn.readTimeout = 180000
                        if (conn.responseCode !in 200..299) throw Exception("HTTP ${conn.responseCode}")
                        conn.inputStream.bufferedReader().use { it.readText() }
                    } finally { conn.disconnect() }
                    val response = JSONObject(body)
                    val assets = response.optJSONArray("assets") ?: throw Exception("Нет списка активов")
                    val combined = JSONObject().put("assets", assets)
                    Thread.sleep(2000)
                    val scanMessage = processScan(combined) + " Только Bybit Spot."
                    val quoteMessage = refreshOpenPositionsFromExchanges()
                    (context as? android.app.Activity)?.runOnUiThread {
                        status.text = scanMessage + "\\n" + quoteMessage
                        scan.isEnabled = true; expandedScan.isEnabled = true
                        scan.text = "СКАНИРОВАТЬ 100 ПАР И ВЫПОЛНИТЬ PAPER-СДЕЛКИ"
                        render(details); showSellActions()
                    }
                } catch (e: Exception) {
                    (context as? android.app.Activity)?.runOnUiThread {
                        status.text = "Ошибка сканирования: ${e.message}"
                        scan.isEnabled = true; expandedScan.isEnabled = true
                        scan.text = "СКАНИРОВАТЬ 100 ПАР И ВЫПОЛНИТЬ PAPER-СДЕЛКИ"
                        render(details); showSellActions()
                    }
                }
            }.start()
        }
        expandedScan.setOnClickListener {
            scanLimit = 300
            scan.text = "РАСШИРЕННЫЙ ПОИСК: ДО 300 ПАР — PAPER-СДЕЛКИ"
            scan.performClick()
        }
        box.addView(expandedScan)
        val emergency = Button(context).apply {
            text = if (isTradingPaused()) "ВОЗОБНОВИТЬ PAPER-ТОРГОВЛЮ" else "ЭКСТРЕННЫЙ СТОП — ОСТАНОВИТЬ"
            setTextColor(Color.WHITE)
            setBackgroundColor(if (isTradingPaused()) green else red)
            setOnClickListener {
                val paused = !isTradingPaused()
                setTradingPaused(paused)
                text = if (paused) "ВОЗОБНОВИТЬ PAPER-ТОРГОВЛЮ" else "ЭКСТРЕННЫЙ СТОП — ОСТАНОВИТЬ"
                setBackgroundColor(if (paused) green else red)
                status.text = if (paused) "Автоматические покупки и продажи остановлены. Позиции сохранены. Ручная продажа доступна." else "Автоматическая PAPER-торговля возобновлена."
                render(details)
                showSellActions()
            }
        }
        box.addView(emergency)
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
                        showSellActions()
                    }
                }.start()
            }
        }
        box.addView(refreshQuotes)
        val reset = Button(context).apply {
            text = "СБРОСИТЬ СЧЁТ ДО \\$1 000"
            setOnClickListener {
                saveBalance(1000.0)
                prefs.edit().putString("positions", "[]")
                    .putString("history", "[]").putLong("last_scan", 0L).apply()
                status.text = "Виртуальный счёт сброшен до $1 000."
                render(details)
                showSellActions()
            }
        }
        box.addView(reset)
        if (!prefs.contains("cash_usdt") && !prefs.contains("cash")) saveBalance(1000.0)
        render(details)
        showSellActions()
        return box
    }
}
