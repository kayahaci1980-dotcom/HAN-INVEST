package com.haninvest.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class CryptoScanner(private val context: Context) {
    private val blue = Color.rgb(55, 105, 245)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)

    private fun dp(v: Int) = (v * context.resources.displayMetrics.density).toInt()

    private fun fetchBybit(): String {
        val connection = URL("https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=100&min_volume=1000000")
            .openConnection() as HttpURLConnection
        val body = try {
            connection.connectTimeout = 20000
            connection.readTimeout = 90000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.8")
            if (connection.responseCode !in 200..299) throw IllegalStateException("HTTP ${connection.responseCode}")
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally { connection.disconnect() }

        val json = JSONObject(body)
        val assets = json.optJSONArray("assets") ?: throw IllegalStateException("Bybit не вернула список монет")
        val qualified = (0 until assets.length()).mapNotNull { assets.optJSONObject(it) }
            .filter { it.optInt("score", 0) > 83 }
            .sortedByDescending { it.optInt("score", 0) }

        if (qualified.isEmpty()) {
            return "BYBIT SPOT\n\nСейчас среди проверенных 100 наиболее ликвидных USDT-пар нет монет с оценкой выше 83.\n\nПроверено пар: ${assets.length()}\nМинимальный оборот: 1 млн USDT за 24 часа. Нажмите «ОБНОВИТЬ BYBIT» позже."
        }

        val out = StringBuilder("BYBIT SPOT — ВСЕ МОНЕТЫ С ОЦЕНКОЙ ВЫШЕ 83\n")
        out.append("Найдено: ").append(qualified.size).append(" из ").append(assets.length()).append(" проверенных пар\n")
        for ((index, coin) in qualified.withIndex()) {
            out.append("\n").append(index + 1).append(". ").append(coin.optString("symbol")).append("\n")
            out.append("Оценка: ").append(coin.optInt("score")).append("/100\n")
            out.append(String.format(Locale.US, "Цена: $%,.8f\n", coin.optDouble("price")))
            out.append(String.format(Locale.US, "RSI(14): %.1f\n", coin.optDouble("rsi_14")))
            out.append(String.format(Locale.US, "ATR(14): %.2f%%\n", coin.optDouble("atr_pct")))
            out.append(String.format(Locale.US, "EMA20: $%,.8f\n", coin.optDouble("ema20")))
            out.append(String.format(Locale.US, "Изменение 24ч: %+.2f%%\n", coin.optDouble("change_24h_pct")))
            out.append("Сигнал: ").append(coin.optString("signal", "WATCH")).append("\n")
            val blockers = coin.optJSONArray("buy_blockers")
            val reasons = mutableListOf<String>()
            if (blockers != null) {
                for (j in 0 until blockers.length()) reasons.add(blockers.optString(j))
            } else {
                if (!(coin.optDouble("price") > coin.optDouble("ema20") && coin.optDouble("ema20") > coin.optDouble("ema50")))
                    reasons.add("Нет подтверждения тренда EMA20/EMA50")
                if (coin.optDouble("momentum_6h_pct") < 1.0) reasons.add("Рост за 6 часов меньше 1%")
                if (!coin.optBoolean("volume_confirmed", false)) reasons.add("Объём ниже 1.2×")
                if (coin.optDouble("quote_volume_24h") < 1000000.0) reasons.add("Оборот за 24 часа меньше 1 млн USDT")
                if (!coin.optBoolean("trade_ready", false)) reasons.add("Пара не активна для торговли")
                if (!coin.optBoolean("btc_buy_allowed", false)) reasons.add("BTC не в бычьем тренде")
            }
            if (coin.optString("signal") == "BUY" && reasons.isEmpty()) {
                out.append("Покупка: все фильтры пройдены\\n")
            } else if (reasons.isNotEmpty()) {
                out.append("Почему пока нет BUY:\\n")
                for (reason in reasons) out.append(" • ").append(reason).append("\\n")
            } else {
                out.append("Почему пока нет BUY: нет подтверждённого сигнала\\n")
            }
        }
        out.append("\nОбновлено: ").append(SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date()))
        out.append("\nСканируются 100 наиболее ликвидных USDT-пар Bybit Spot с оборотом от 1 млн USDT. Это не весь листинг биржи.")
        out.append("\nВсе сделки PAPER виртуальные; реальные ордера не отправляются.")
        return out.toString()
    }

    fun createView(): View {
        val root = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
        root.addView(TextView(context).apply {
            text = "₿ Крипторынок — BYBIT SPOT"
            textSize = 21f
            setTextColor(dark)
            setTypeface(null, Typeface.BOLD)
        })
        root.addView(TextView(context).apply {
            text = "Все найденные монеты с оценкой 84–100. Причины отсутствия BUY показываются отдельно."
            textSize = 13f
            setTextColor(muted)
        })
        val status = TextView(context).apply {
            text = "Загрузка Bybit..."
            textSize = 14f
            setTextColor(dark)
            setPadding(0, dp(12), 0, dp(12))
        }
        root.addView(status)
        val refresh = Button(context).apply {
            text = "ОБНОВИТЬ BYBIT"
            setTextColor(Color.WHITE)
            background = GradientDrawable().apply {
                setColor(blue)
                cornerRadius = dp(12).toFloat()
            }
        }
        root.addView(refresh)
        fun load() {
            refresh.isEnabled = false
            status.text = "Сканируем до 100 ликвидных пар Bybit Spot..."
            Thread {
                val result = try { fetchBybit() } catch (e: Exception) {
                    "Bybit Spot временно недоступна: ${e.message ?: "ошибка соединения"}"
                }
                (context as? android.app.Activity)?.runOnUiThread {
                    status.text = result
                    refresh.isEnabled = true
                }
            }.start()
        }
        refresh.setOnClickListener { load() }
        load()
        return root
    }
}
