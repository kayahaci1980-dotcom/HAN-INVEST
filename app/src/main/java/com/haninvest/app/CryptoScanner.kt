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
import java.util.Locale

class CryptoScanner(private val context: Context) {
    private val blue = Color.rgb(55, 105, 245)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private fun background(color: Int) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(14).toFloat()
    }

    private fun text(value: String, size: Float, color: Int, bold: Boolean = false) =
        TextView(context).apply {
            text = value
            textSize = size
            setTextColor(color)
            if (bold) setTypeface(null, Typeface.BOLD)
        }

    fun createView(): View {
        val section = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        section.addView(text("₿ Криптосканер HAN INVEST", 21f, dark, true))
        section.addView(text(
            "Рейтинг монет по рыночной активности. WATCH — наблюдать, не команда покупать.",
            13f, muted
        ))
        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(15), dp(16), dp(15))
            background = background(Color.WHITE)
            elevation = dp(2).toFloat()
        }
        val status = text("Нажмите «Обновить», чтобы загрузить рейтинг.", 14f, dark)
        val refresh = Button(context).apply {
            this.text = "ОБНОВИТЬ КРИПТОСКАНЕР"
            setTextColor(Color.WHITE)
            background = background(blue)
            setOnClickListener { load(status) }
        }
        panel.addView(status)
        panel.addView(refresh)
        section.addView(panel)
        section.addView(text(
            "Источник: HAN INVEST API. Резервные агрегированные данные не подтверждают торговую пару на бирже.",
            11f, muted
        ))
        load(status)
        return section
    }

    private fun load(target: TextView) {
        target.text = "Загрузка рейтинга криптовалют..."
        Thread {
            try {
                val connection = URL(
                    "https://han-invest-api-test.onrender.com/api/v1/crypto/scan?limit=30&min_volume=1000000"
                ).openConnection() as HttpURLConnection
                val jsonText = try {
                    connection.requestMethod = "GET"
                    connection.connectTimeout = 20000
                    connection.readTimeout = 25000
                    connection.setRequestProperty("Accept", "application/json")
                    connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.4")
                    if (connection.responseCode !in 200..299) {
                        throw Exception("HTTP ${connection.responseCode}")
                    }
                    connection.inputStream.bufferedReader().use { it.readText() }
                } finally {
                    connection.disconnect()
                }
                val json = JSONObject(jsonText)
                val assets = json.optJSONArray("assets")
                    ?: throw Exception("Нет списка активов")
                val out = StringBuilder()
                out.append("Источник: ${json.optString("source")}\n")
                out.append("Монет: ${assets.length()}\n\n")
                for (i in 0 until assets.length()) {
                    val coin = assets.getJSONObject(i)
                    val symbol = coin.optString("symbol")
                    val name = coin.optString("name", symbol)
                    val price = coin.optDouble("price", Double.NaN)
                    val change = coin.optDouble("change_24h_pct", Double.NaN)
                    val score = coin.optInt("score", 0)
                    val signal = coin.optString("signal", "WATCH")
                    out.append("${i + 1}. $name ($symbol)\n")
                    if (!price.isNaN()) {
                        out.append(String.format(Locale.US, "Цена: \\$%.6f USD\n", price))
                    }
                    if (!change.isNaN()) {
                        out.append(String.format(Locale.US, "Изменение 24ч: %.2f%%\n", change))
                    }
                    out.append("Оценка: $score/100  |  Сигнал: $signal\n")
                    if (coin.optBoolean("trade_ready", false)) {
                        out.append("Торговая пара подтверждена\n")
                    } else {
                        out.append("Пара не подтверждена — не готово к сделке\n")
                    }
                    out.append("\n")
                }
                val notice = json.optString("notice")
                if (notice.isNotBlank()) out.append("\n$notice")
                (context as? android.app.Activity)?.runOnUiThread {
                    target.text = out.toString()
                }
            } catch (error: Exception) {
                (context as? android.app.Activity)?.runOnUiThread {
                    target.text = "Не удалось загрузить рейтинг. Проверьте интернет и повторите попытку."
                }
            }
        }.start()
    }
}
