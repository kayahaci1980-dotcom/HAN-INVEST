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
        val connection = URL("https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000")
            .openConnection() as HttpURLConnection
        val body = try {
            connection.connectTimeout = 20000
            connection.readTimeout = 45000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.7")
            if (connection.responseCode !in 200..299) throw IllegalStateException("HTTP ${connection.responseCode}")
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally { connection.disconnect() }

        val json = JSONObject(body)
        val assets = json.optJSONArray("assets") ?: throw IllegalStateException("Bybit не вернула список монет")
        if (assets.length() == 0) return "Bybit Spot: подходящих торговых пар пока нет."
        val out = StringBuilder("BYBIT SPOT — ТОП-3 ПО ТЕХНИЧЕСКОЙ ОЦЕНКЕ\n")
        for (i in 0 until minOf(3, assets.length())) {
            val coin = assets.optJSONObject(i) ?: continue
            out.append("\n").append(i + 1).append(". ").append(coin.optString("symbol")).append("\n")
            out.append(String.format(Locale.US, "Цена: $%,.8f\n", coin.optDouble("price")))
            out.append("Оценка: ").append(coin.optInt("score")).append("/100\n")
            out.append(String.format(Locale.US, "RSI(14): %.1f\n", coin.optDouble("rsi_14")))
            out.append(String.format(Locale.US, "ATR(14): %.2f%%\n", coin.optDouble("atr_pct")))
            out.append(String.format(Locale.US, "EMA20: $%,.8f\n", coin.optDouble("ema20")))
            out.append(String.format(Locale.US, "Изменение 24ч: %+.2f%%\n", coin.optDouble("change_24h_pct")))
            out.append("Сигнал: ").append(coin.optString("signal", "—")).append("\n")
            out.append("PAPER-покупка: ").append(if (coin.optInt("score") > 83) "приоритет по оценке >83" else "ожидание оценки выше 83").append("\n")
        }
        out.append("\nОбновлено: ").append(SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date()))
        out.append("\nИсточник котировок: Bybit Spot. Все сделки виртуальные; реальные ордера не отправляются.")
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
            text = "Только Bybit: топ-3 монеты, единые показатели RSI, ATR, EMA20 и оценка из 100 баллов."
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
            status.text = "Получаем данные Bybit Spot..."
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
