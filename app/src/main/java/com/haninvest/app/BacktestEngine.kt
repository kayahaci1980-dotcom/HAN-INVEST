
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
import kotlin.math.min

class BacktestEngine(private val context: Context) {

    private val prefs = context.getSharedPreferences(
        "han_invest_settings", Context.MODE_PRIVATE
    )

    private val symbols = listOf("AAPL", "MSFT", "NVDA", "SPY")

    private data class Bar(
        val date: String,
        val open: Double,
        val high: Double,
        val low: Double,
        val close: Double
    )

    private data class Trade(
        val symbol: String,
        val entryDate: String,
        val exitDate: String,
        val entry: Double,
        val exit: Double,
        val stopPercent: Double,
        val score: Int,
        val reason: String
    )

    private fun dp(value: Int) =
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

    private fun loadBars(
        symbol: String,
        key: String
    ): List<Bar> {
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
                "User-Agent", "HAN-INVEST-Android/2.3"
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

            val bars = dates.map { date ->
                val item = series.getJSONObject(date)
                Bar(
                    date = date,
                    open = item.getDouble("1. open"),
                    high = item.getDouble("2. high"),
                    low = item.getDouble("3. low"),
                    close = item.getDouble("4. close")
                )
            }

            if (bars.size < 55) {
                throw Exception("Недостаточно исторических данных")
            }

            return bars
        } finally {
            connection.disconnect()
        }
    }

    private fun testSymbol(
        symbol: String,
        bars: List<Bar>,
        horizon: Int
    ): List<Trade> {
        val trades = mutableListOf<Trade>()

        // Индексы идут от новых дат к старым.
        // Сигнал использует только данные, известные на дату входа.
        for (i in bars.size - 51 downTo horizon) {
            val current = bars[i]
            val price = current.close

            val ma20 = (i until i + 20)
                .map { bars[it].close }.average()

            val ma50 = (i until i + 50)
                .map { bars[it].close }.average()

            val change5 =
                (price / bars[i + 5].close - 1.0) * 100.0

            val change20 =
                (price / bars[i + 20].close - 1.0) * 100.0

            val dailyReturns = (0 until 20).map {
                bars[i + it].close /
                    bars[i + it + 1].close - 1.0
            }

            val average = dailyReturns.average()
            val variance = dailyReturns.map {
                (it - average) * (it - average)
            }.average()

            val dailyVolatility = sqrt(variance)
            val annualVolatility =
                dailyVolatility * sqrt(252.0) * 100.0

            var score = 0
            if (price > ma20) score += 25
            if (ma20 > ma50) score += 25
            if (change5 > 0) score += 20
            if (change20 > 0) score += 20
            if (annualVolatility < 50) score += 10

            // Тестируем только сильные сигналы сканера.
            if (score < 80 || annualVolatility >= 50) {
                continue
            }

            val stopPercent = max(
                2.0,
                dailyVolatility * 2.0 * 100.0
            )

            val stopPrice =
                price * (1.0 - stopPercent / 100.0)

            val targetPrice =
                price * (1.0 + stopPercent * 2.0 / 100.0)

            var exitPrice = bars[i - horizon].close
            var exitDate = bars[i - horizon].date
            var reason = "Закрытие по окончании срока"

            for (day in 1..horizon) {
                val future = bars[i - day]

                val stopHit = future.low <= stopPrice
                val targetHit = future.high >= targetPrice

                // Консервативное правило: стоп раньше цели.
                if (stopHit) {
                    exitPrice = min(future.open, stopPrice)
                    exitDate = future.date
                    reason = "Стоп"
                    break
                }

                if (targetHit) {
                    exitPrice = targetPrice
                    exitDate = future.date
                    reason = "Цель 2:1"
                    break
                }
            }

            trades.add(
                Trade(
                    symbol = symbol,
                    entryDate = current.date,
                    exitDate = exitDate,
                    entry = price,
                    exit = exitPrice,
                    stopPercent = stopPercent,
                    score = score,
                    reason = reason
                )
            )
        }

        return trades
    }

    private fun runBacktest(
        key: String,
        capital: Double,
        horizon: Int
    ): String {
        val candidates = mutableListOf<Trade>()
        val errors = mutableListOf<String>()

        for ((index, symbol) in symbols.withIndex()) {
            try {
                // Пауза между запросами бесплатного API.
                if (index > 0) Thread.sleep(12000)

                val bars = loadBars(symbol, key)
                candidates.addAll(
                    testSymbol(symbol, bars, horizon)
                )
            } catch (e: Exception) {
                errors.add("$symbol: ${e.message}")
            }
        }

        if (candidates.isEmpty()) {
            return "Не найдено подходящих исторических сигналов.\n\n" +
                errors.joinToString("\n")
        }

        // Сортировка по времени входа.
        // Одновременно учитывается только одна позиция.
        val ordered = candidates.sortedWith(
            compareBy<Trade> { it.entryDate }
                .thenByDescending { it.score }
        )

        val selected = mutableListOf<Trade>()
        var lastExitDate = ""

        for (trade in ordered) {
            if (lastExitDate.isEmpty() ||
                trade.entryDate > lastExitDate
            ) {
                selected.add(trade)
                lastExitDate = trade.exitDate
            }
        }

        if (selected.isEmpty()) {
            return "Недостаточно сделок для тестирования."
        }

        var equity = capital
        var peak = capital
        var maxDrawdown = 0.0

        var wins = 0
        var losses = 0
        var winSum = 0.0
        var lossSum = 0.0

        val details = StringBuilder()

        for ((index, trade) in selected.withIndex()) {
            val riskMoney = equity * 0.02
            val stopFraction = trade.stopPercent / 100.0

            val position = min(
                equity,
                riskMoney / stopFraction
            )

            val returnFraction =
                trade.exit / trade.entry - 1.0

            val profit = position * returnFraction
            equity += profit

            if (profit > 0) {
                wins++
                winSum += profit
            } else if (profit < 0) {
                losses++
                lossSum += profit
            }

            peak = max(peak, equity)

            val drawdown =
                if (peak > 0) (peak - equity) / peak * 100.0
                else 0.0

            maxDrawdown = max(maxDrawdown, drawdown)

            if (index < 10) {
                details.append(
                    String.format(
                        Locale.US,
                        "%s | %s → %s | %s | P/L: $%.2f\n",
                        trade.symbol,
                        trade.entryDate,
                        trade.exitDate,
                        trade.reason,
                        profit
                    )
                )
            }
        }

        val count = selected.size
        val winRate = wins * 100.0 / count
        val netProfit = equity - capital
        val netPercent = netProfit / capital * 100.0

        val out = StringBuilder()

        out.append("HAN INVEST — ИСТОРИЧЕСКИЙ ТЕСТ\n\n")
        out.append("Инструменты: ${symbols.joinToString()}\n")
        out.append("Срок сделки: $horizon торговых дней\n")
        out.append("Начальный капитал: ")
        out.append(String.format(Locale.US, "$%.2f\n", capital))
        out.append("Риск на сделку: до 2%\n")
        out.append("Одновременно: одна позиция\n\n")

        out.append("РЕЗУЛЬТАТЫ\n")
        out.append("Количество сделок: $count\n")
        out.append("Прибыльных: $wins\n")
        out.append("Убыточных: $losses\n")
        out.append(
            String.format(Locale.US, "Успешных: %.1f%%\n", winRate)
        )
        out.append(
            String.format(Locale.US, "Итоговый капитал: $%.2f\n", equity)
        )
        out.append(
            String.format(Locale.US, "Результат: $%.2f (%.2f%%)\n",
                netProfit, netPercent)
        )
        out.append(
            String.format(Locale.US, "Максимальная просадка: %.2f%%\n",
                maxDrawdown)
        )

        if (wins > 0) {
            out.append(
                String.format(Locale.US, "Средняя прибыль: $%.2f\n",
                    winSum / wins)
            )
        }

        if (losses > 0) {
            out.append(
                String.format(Locale.US, "Средний убыток: $%.2f\n",
                    lossSum / losses)
            )
        }

        out.append("\nПРИМЕРЫ СДЕЛОК\n")
        out.append(details)

        if (count < 10) {
            out.append(
                "\nВНИМАНИЕ: выборка меньше 10 сделок. " +
                "Результат недостаточен для оценки стратегии.\n"
            )
        }

        if (errors.isNotEmpty()) {
            out.append("\nНе удалось проверить:\n")
            out.append(errors.joinToString("\n"))
        }

        out.append(
            "\n\nОГРАНИЧЕНИЯ ТЕСТА\n" +
            "Использованы дневные OHLC-котировки. " +
            "Стоп при гэпе может исполниться хуже расчётной цены. " +
            "Комиссии, налоги и проскальзывание не учтены. " +
            "Исторический результат не гарантирует будущую прибыль. " +
            "Это исследование, а не рекомендация покупать."
        )

        return out.toString()
    }

    fun createView(): View {
        val box = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(17), dp(15), dp(17), dp(15))
            setBackgroundColor(Color.WHITE)
        }

        box.addView(
            text("Историческое тестирование", 20f,
                Color.rgb(30, 42, 65), true)
        )

        box.addView(
            text(
                "Проверка стратегии на прошлых котировках",
                13f, Color.GRAY
            )
        )

        val capitalInput = EditText(context).apply {
            hint = "Начальный капитал в USD"
            inputType = InputType.TYPE_CLASS_NUMBER or
                InputType.TYPE_NUMBER_FLAG_DECIMAL
            setText("1000")
        }

        val horizonSpinner = Spinner(context).apply {
            adapter = ArrayAdapter(
                context,
                android.R.layout.simple_spinner_item,
                listOf("1 день", "2 дня", "3 дня")
            ).also {
                it.setDropDownViewResource(
                    android.R.layout.simple_spinner_dropdown_item
                )
            }
            setSelection(2)
        }

        box.addView(capitalInput)
        box.addView(horizonSpinner)

        val result = text(
            "Тест использует сохранённый ключ доступа.",
            14f, Color.rgb(30, 42, 65)
        )

        val button = Button(context)
        button.apply {
            text = "ЗАПУСТИТЬ ИСТОРИЧЕСКИЙ ТЕСТ"
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.rgb(55, 105, 245))

            setOnClickListener {
                val key = prefs.getString("alpha_key", "") ?: ""
                val capital = capitalInput.text.toString()
                    .replace(",", ".").toDoubleOrNull()

                if (key.isBlank()) {
                    result.text =
                        "Сначала введи ключ доступа в сканере."
                    return@setOnClickListener
                }

                if (capital == null || capital <= 0) {
                    result.text =
                        "Введи правильный размер капитала."
                    return@setOnClickListener
                }

                val horizon = horizonSpinner.selectedItemPosition + 1

                isEnabled = false
                result.text =
                    "Загружаем историю и проверяем стратегию. " +
                    "Это может занять около минуты."

                Thread {
                    val answer = try {
                        runBacktest(key, capital, horizon)
                    } catch (e: Exception) {
                        "Ошибка тестирования: ${e.message}"
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
