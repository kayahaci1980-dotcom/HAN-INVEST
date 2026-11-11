
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
import kotlin.math.max

class StockScanner(private val context: Context) {

    private val prefs = context.getSharedPreferences(
        "han_invest_settings", Context.MODE_PRIVATE
    )

    private val dark = Color.rgb(30, 42, 65)
    private val blue = Color.rgb(55, 105, 245)
    private val muted = Color.rgb(125, 137, 158)

    private val symbols = listOf(
        "AAPL", "MSFT", "NVDA", "AMZN",
        "GOOGL", "SPY", "QQQ", "TSLA"
    )

    private data class Candidate(
        val symbol: String,
        val price: Double,
        val change5: Double,
        val change20: Double,
        val volatility: Double,
        val stopPercent: Double,
        val score: Int,
        val signal: String,
        val date: String
    )

    private fun dp(value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()

    private fun makeText(
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

    private fun request(
        symbol: String,
        key: String
    ): Candidate {
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
                "User-Agent", "HAN-INVEST-Android/2.2"
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
            ) ?: throw Exception("История цен недоступна")

            val dates = series.keys().asSequence()
                .sortedDescending().toList()

            if (dates.size < 60) {
                throw Exception("Недостаточно истории")
            }

            val prices = dates.map {
                series.getJSONObject(it).getDouble("4. close")
            }

            val latest = prices[0]
            val ma20 = prices.take(20).average()
            val ma50 = prices.take(50).average()

            val change5 =
                (latest / prices[5] - 1) * 100

            val change20 =
                (latest / prices[20] - 1) * 100

            val dailyReturns =
                (0 until 20).map {
                    prices[it] / prices[it + 1] - 1
                }

            val average = dailyReturns.average()
            val variance = dailyReturns.map {
                (it - average) * (it - average)
            }.average()

            val dailyVolatility = sqrt(variance)
            val annualVolatility =
                dailyVolatility * sqrt(252.0) * 100

            val stopPercent = max(
                2.0,
                dailyVolatility * 2.0 * 100
            )

            var score = 0

            if (latest > ma20) score += 25
            if (ma20 > ma50) score += 25
            if (change5 > 0) score += 20
            if (change20 > 0) score += 20
            if (annualVolatility < 50) score += 10

            val signal = when {
                score >= 80 && annualVolatility < 50 ->
                    "НАБЛЮДАТЬ: сильный сигнал"
                score >= 60 ->
                    "НАБЛЮДАТЬ"
                else ->
                    "НЕ ВХОДИТЬ"
            }

            return Candidate(
                symbol = symbol,
                price = latest,
                change5 = change5,
                change20 = change20,
                volatility = annualVolatility,
                stopPercent = stopPercent,
                score = score,
                signal = signal,
                date = dates[0]
            )
        } finally {
            connection.disconnect()
        }
    }

    private fun analyze(
        key: String,
        capital: Double
    ): String {
        val results = mutableListOf<Candidate>()
        val errors = mutableListOf<String>()

        for (symbol in symbols) {
            try {
                results.add(request(symbol, key))
            } catch (e: Exception) {
                errors.add("$symbol: ${e.message}")
            }
        }

        if (results.isEmpty()) {
            return "Не удалось получить данные.\n\n" +
                errors.joinToString("\n")
        }

        val sorted = results.sortedWith(
            compareByDescending<Candidate> { it.score }
                .thenBy { it.volatility }
        )

        val out = StringBuilder()

        out.append("HAN INVEST — РЕЗУЛЬТАТЫ СКАНИРОВАНИЯ\n")
        out.append("Горизонт анализа: 1–3 дня\n")
        out.append("Капитал: $")
        out.append(String.format(Locale.US, "%.2f", capital))
        out.append("\nПлановый риск: 2% на сделку\n")
        out.append("Дата котировок: ${sorted.first().date}\n\n")

        for ((index, item) in sorted.withIndex()) {
            val riskMoney = capital * 0.02
            val position = riskMoney /
                (item.stopPercent / 100.0)

            val safePosition = position.coerceAtMost(capital)
            val stop = item.price *
                (1 - item.stopPercent / 100.0)
            val target = item.price *
                (1 + item.stopPercent * 2 / 100.0)

            out.append("${index + 1}. ${item.symbol}\n")
            out.append("Оценка сигнала: ${item.score}/100\n")
            out.append("${item.signal}\n")
            out.append(
                String.format(
                    Locale.US,
                    "Цена закрытия: $%.2f\n" +
                    "Изменение за 5 дней: %.2f%%\n" +
                    "Изменение за 20 дней: %.2f%%\n" +
                    "Годовая волатильность: %.2f%%\n" +
                    "Расчётная сумма позиции: $%.2f\n" +
                    "Условный стоп: $%.2f\n" +
                    "Цель 2:1: $%.2f\n\n",
                    item.price,
                    item.change5,
                    item.change20,
                    item.volatility,
                    safePosition,
                    stop,
                    target
                )
            )
        }

        val best = sorted.firstOrNull {
            it.score >= 80 && it.volatility < 50
        }

        out.append(
            if (best == null) {
                "ИТОГ: подходящего сигнала нет. " +
                    "Сегодня лучше не входить."
            } else {
                "ЛУЧШИЙ КАНДИДАТ: ${best.symbol}\n" +
                    "Это кандидат для дальнейшей проверки, " +
                    "а не команда покупать."
            }
        )

        if (errors.isNotEmpty()) {
            out.append("\n\nНе удалось проверить:\n")
            out.append(errors.joinToString("\n"))
        }

        out.append(
            "\n\nВажно: используются дневные цены закрытия. " +
            "Сигналы не являются прогнозом. " +
            "Стоп не гарантирует ограничение фактического убытка. " +
            "Комиссии и проскальзывание не учтены."
        )

        return out.toString()
    }

    fun createView(): View {
        val box = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(17), dp(15), dp(17), dp(15))
            setBackgroundColor(Color.WHITE)
        }

        box.addView(makeText(
            "Автоматический сканер", 20f, dark, true
        ))

        box.addView(makeText(
            "Поиск возможностей на срок до 3 дней",
            13f, muted
        ))

        val keyInput = EditText(context).apply {
            hint = "Ключ доступа"
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(prefs.getString("alpha_key", ""))
        }

        val capitalInput = EditText(context).apply {
            hint = "Размер капитала в USD"
            inputType = InputType.TYPE_CLASS_NUMBER or
                InputType.TYPE_NUMBER_FLAG_DECIMAL
            setText("1000")
        }

        box.addView(keyInput)
        box.addView(capitalInput)

        val result = makeText(
            "Введите ключ доступа и капитал, затем запустите сканирование.",
            14f, dark
        )

        // Кнопка создаётся отдельно, чтобы избежать ошибки инициализации
        val button = Button(context)

        button.apply {
            text = "НАЙТИ ЛУЧШИЕ ВОЗМОЖНОСТИ"
            setTextColor(Color.WHITE)
            setBackgroundColor(blue)

            setOnClickListener {
                val key = keyInput.text.toString().trim()
                val capital = capitalInput.text.toString()
                    .replace(",", ".").toDoubleOrNull()

                if (key.isBlank() || capital == null ||
                    capital <= 0
                ) {
                    result.text =
                        "Введите ключ доступа и правильный размер капитала."
                    return@setOnClickListener
                }

                prefs.edit()
                    .putString("alpha_key", key)
                    .apply()

                isEnabled = false
                result.text =
                    "Проверяем 8 активов. Это может занять время..."

                Thread {
                    val answer = try {
                        analyze(key, capital)
                    } catch (e: Exception) {
                        "Ошибка анализа: ${e.message}"
                    }

                    (context as? android.app.Activity)
                        ?.runOnUiThread {
                            result.text = answer
                            button.isEnabled = true
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
