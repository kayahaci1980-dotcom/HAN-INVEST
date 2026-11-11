
package com.haninvest.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.view.View
import android.widget.*
import android.text.InputType
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.Locale
import kotlin.math.sqrt

class StockScanner(private val context: Context) {

    private val prefs = context.getSharedPreferences(
        "han_invest_settings", Context.MODE_PRIVATE
    )

    private val dark = Color.rgb(30, 42, 65)
    private val blue = Color.rgb(55, 105, 245)
    private val muted = Color.rgb(125, 137, 158)

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private fun text(
        value: String,
        size: Float,
        color: Int,
        bold: Boolean = false
    ) = TextView(context).apply {
        text = value
        textSize = size
        setTextColor(color)
        if (bold) setTypeface(null, Typeface.BOLD)
    }

    private fun request(symbol: String, key: String): String {
        val encoded = URLEncoder.encode(symbol, "UTF-8")
        val address =
            "https://www.alphavantage.co/query" +
            "?function=TIME_SERIES_DAILY" +
            "&symbol=$encoded&outputsize=compact&apikey=$key"

        val connection =
            URL(address).openConnection() as HttpURLConnection

        try {
            connection.connectTimeout = 20000
            connection.readTimeout = 20000
            connection.setRequestProperty(
                "User-Agent", "HAN-INVEST-Android/2.1"
            )

            if (connection.responseCode !in 200..299) {
                throw Exception("Ошибка соединения")
            }

            val body = connection.inputStream
                .bufferedReader().use { it.readText() }

            val json = JSONObject(body)

            for (field in listOf(
                "Error Message", "Note", "Information"
            )) {
                if (json.has(field)) {
                    throw Exception(json.optString(field))
                }
            }

            val series = json.optJSONObject(
                "Time Series (Daily)"
            ) ?: throw Exception(
                "Нет истории цен. Проверьте тикер и API-ключ."
            )

            val dates = series.keys().asSequence()
                .sortedDescending().toList()

            if (dates.size < 21) {
                throw Exception("Недостаточно исторических данных")
            }

            val prices = dates.map {
                series.getJSONObject(it).getDouble("4. close")
            }

            val latest = prices[0]
            val old20 = prices[20]
            val old60 = prices.getOrNull(60) ?: prices.last()

            val return20 = (latest / old20 - 1) * 100
            val return60 = (latest / old60 - 1) * 100

            val dailyReturns = (0 until minOf(60, prices.size - 1))
                .map { prices[it] / prices[it + 1] - 1 }

            val average = dailyReturns.average()
            val variance = dailyReturns.map {
                (it - average) * (it - average)
            }.average()

            val volatility = sqrt(variance) * sqrt(252.0) * 100

            var peak = 0.0
            var maxDrawdown = 0.0

            for (price in prices.asReversed()) {
                if (price > peak) peak = price
                if (peak > 0) {
                    val drawdown = (price / peak - 1) * 100
                    if (drawdown < maxDrawdown) {
                        maxDrawdown = drawdown
                    }
                }
            }

            val risk = when {
                volatility < 15 -> "Относительно низкая"
                volatility < 30 -> "Средняя"
                volatility < 50 -> "Высокая"
                else -> "Очень высокая"
            }

            return String.format(
                Locale.US,
                "АКТИВ: %s\n" +
                "Дата последней цены: %s\n" +
                "Последняя цена закрытия: $%.2f\n\n" +
                "Изменение за 20 торговых дней: %.2f%%\n" +
                "Изменение примерно за 60 дней: %.2f%%\n\n" +
                "Годовая волатильность: %.2f%%\n" +
                "Максимальная просадка в выборке: %.2f%%\n" +
                "Оценка риска: %s\n\n" +
                "Источник: Alpha Vantage.\n" +
                "Используются дневные цены закрытия. " +
                "Это исторический анализ, не прогноз.",
                symbol, dates[0], latest, return20,
                return60, volatility, maxDrawdown, risk
            )
        } finally {
            connection.disconnect()
        }
    }

    fun createView(): View {
        val box = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(17), dp(15), dp(17), dp(15))
            setBackgroundColor(Color.WHITE)
        }

        box.addView(text(
            "Сканер акций и ETF", 20f, dark, true
        ))
        box.addView(text(
            "Анализ исторических цен мировых активов",
            13f, muted
        ))

        val keyInput = EditText(context).apply {
            hint = "API-ключ Alpha Vantage"
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(prefs.getString("alpha_key", ""))
        }

        val ticker = EditText(context).apply {
            hint = "Тикер: AAPL, MSFT, TSLA, SPY"
            inputType = InputType.TYPE_CLASS_TEXT
            setSingleLine(true)
        }

        box.addView(keyInput)
        box.addView(ticker)

        val result = text(
            "Введите API-ключ и тикер акции или ETF.",
            14f, dark
        )

        val button = Button(context).apply {
            text = "АНАЛИЗИРОВАТЬ"
            setTextColor(Color.WHITE)
            setBackgroundColor(blue)

            setOnClickListener {
                val key = keyInput.text.toString().trim()
                val symbol = ticker.text.toString()
                    .trim().uppercase(Locale.US)

                if (key.isBlank() || symbol.isBlank()) {
                    result.text = "Введите API-ключ и тикер."
                    return@setOnClickListener
                }

                if (!symbol.matches(
                    Regex("[A-Z0-9.\\-]{1,20}")
                )) {
                    result.text = "Проверьте формат тикера."
                    return@setOnClickListener
                }

                prefs.edit().putString("alpha_key", key).apply()
                result.text = "Загружаем историю цен..."

                Thread {
                    try {
                        val answer = request(symbol, key)
                        (context as? android.app.Activity)
                            ?.runOnUiThread {
                                result.text = answer
                            }
                    } catch (e: Exception) {
                        (context as? android.app.Activity)
                            ?.runOnUiThread {
                                result.text =
                                    "Не удалось выполнить анализ.\n\n" +
                                    (e.message ?: "Попробуйте позже.")
                            }
                    }
                }.start()
            }
        }

        box.addView(button)
        box.addView(View(context).apply {
            layoutParams = LinearLayout.LayoutParams(
                1, dp(12)
            )
        })
        box.addView(result)

        return box
    }
}
