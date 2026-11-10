
package com.haninvest.app

import android.app.Activity
import android.os.Bundle
import android.graphics.Color
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.LinearGradient
import android.graphics.Shader
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.*
import android.text.InputType
import android.graphics.drawable.GradientDrawable
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class MainActivity : Activity() {

    private val blue = Color.rgb(55, 105, 245)
    private val purple = Color.rgb(125, 75, 235)
    private val green = Color.rgb(25, 175, 115)
    private val dark = Color.rgb(30, 42, 65)
    private val muted = Color.rgb(125, 137, 158)
    private val page = Color.rgb(246, 248, 253)

    private fun dp(v: Int): Int =
        (v * resources.displayMetrics.density).toInt()

    private fun bg(color: Int, radius: Int = 18) =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = dp(radius).toFloat()
        }

    private fun label(
        value: String,
        size: Float,
        color: Int,
        bold: Boolean = false
    ) = TextView(this).apply {
        text = value
        textSize = size
        setTextColor(color)
        if (bold) setTypeface(
            null,
            android.graphics.Typeface.BOLD
        )
    }

    private fun card() = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(18), dp(16), dp(18), dp(16))
        background = bg(Color.WHITE, 20)
        elevation = dp(3).toFloat()
    }

    private fun space(height: Int = 14) =
        View(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                1,
                dp(height)
            )
        }

    private fun input(hintText: String, type: Int) =
        EditText(this).apply {
            hint = hintText
            inputType = type
            textSize = 15f
            setTextColor(dark)
            setHintTextColor(muted)
            background = bg(page, 12)
            setPadding(dp(14), dp(10), dp(14), dp(10))
        }

    // ЗАГРУЗКА РЕАЛЬНЫХ ВАЛЮТНЫХ КУРСОВ

    private fun loadCurrencyRates(target: TextView) {
        target.text = "Загружаем валютные курсы..."

        Thread {
            var connection: HttpURLConnection? = null

            try {
                val url = URL(
                    "https://api.frankfurter.dev/v1/latest?base=USD"
                )

                connection =
                    url.openConnection() as HttpURLConnection

                connection.requestMethod = "GET"
                connection.connectTimeout = 15000
                connection.readTimeout = 15000
                connection.setRequestProperty(
                    "Accept",
                    "application/json"
                )

                val responseCode = connection.responseCode

                if (responseCode !in 200..299) {
                    throw Exception("HTTP $responseCode")
                }

                val response = connection.inputStream
                    .bufferedReader()
                    .use { it.readText() }

                val json = JSONObject(response)
                val rates = json.getJSONObject("rates")
                val date = json.optString(
                    "date",
                    "не указана"
                )

                val currencies = listOf(
                    "EUR" to "Евро",
                    "GBP" to "Фунт стерлингов",
                    "CNY" to "Китайский юань",
                    "TRY" to "Турецкая лира",
                    "CHF" to "Швейцарский франк",
                    "JPY" to "Японская иена"
                )

                val lines = StringBuilder()

                for ((code, name) in currencies) {
                    if (rates.has(code)) {
                        val value = rates.getDouble(code)

                        lines.append(
                            String.format(
                                Locale.US,
                                "1 USD = %.4f %s\n%s\n\n",
                                value,
                                code,
                                name
                            )
                        )
                    }
                }

                if (lines.isEmpty()) {
                    lines.append(
                        "Валюты временно недоступны."
                    )
                }

                val result =
                    "Дата курса: $date\n\n" +
                    lines.toString() +
                    "Источник: Frankfurter API\n" +
                    "Курсы справочные, не банковские."

                runOnUiThread {
                    target.text = result
                }

            } catch (e: Exception) {
                runOnUiThread {
                    target.text =
                        "Не удалось загрузить курсы.\n" +
                        "Проверьте интернет и нажмите обновить."
                }
            } finally {
                connection?.disconnect()
            }
        }.start()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.statusBarColor = page
        window.navigationBarColor = page
        window.decorView.systemUiVisibility =
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR

        val scroll = ScrollView(this)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(
                dp(20),
                dp(22),
                dp(20),
                dp(28)
            )
            setBackgroundColor(page)
        }

        // ШАПКА

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }

        header.addView(
            label("HAN", 26f, blue, true)
        )

        header.addView(
            label(" INVEST", 26f, purple, true)
        )

        val spacer = View(this)

        header.addView(
            spacer,
            LinearLayout.LayoutParams(0, 1, 1f)
        )

        header.addView(
            label("●", 22f, green, true).apply {
                background = bg(Color.WHITE, 30)
                setPadding(
                    dp(13),
                    dp(8),
                    dp(13),
                    dp(8)
                )
            }
        )

        root.addView(header)

        root.addView(
            label(
                "Инвестиционная платформа",
                14f,
                muted
            )
        )

        root.addView(space(18))

        // ГЛАВНЫЙ БАННЕР

        val banner = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(
                dp(22),
                dp(22),
                dp(22),
                dp(22)
            )

            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(blue, purple)
            ).apply {
                cornerRadius = dp(22).toFloat()
            }
        }

        banner.addView(
            label(
                "УМНЫЕ ИНВЕСТИЦИИ",
                12f,
                Color.WHITE,
                true
            )
        )

        banner.addView(space(8))

        banner.addView(
            label(
                "Ваш капитал.\nВаши возможности.",
                27f,
                Color.WHITE,
                true
            )
        )

        banner.addView(space(8))

        banner.addView(
            label(
                "Анализируйте рынки и находите инвестиционные возможности.",
                14f,
                Color.WHITE
            )
        )

        root.addView(banner)
        root.addView(space(20))

        // ПОРТФЕЛЬ

        root.addView(
            label("Мой портфель", 21f, dark, true)
        )

        root.addView(space(10))

        val portfolio = card()

        portfolio.addView(
            label("ОБЩИЙ КАПИТАЛ", 12f, muted, true)
        )

        portfolio.addView(space(5))

        portfolio.addView(
            label("$0.00", 34f, dark, true)
        )

        portfolio.addView(space(5))

        portfolio.addView(
            label(
                "Начните с анализа своих возможностей",
                13f,
                muted
            )
        )

        root.addView(portfolio)
        root.addView(space(20))

        // РЫНКИ

        root.addView(
            label("Рынки для анализа", 21f, dark, true)
        )

        root.addView(space(10))

        val markets = listOf(
            "📈  Акции" to blue,
            "🥇  Золото" to purple,
            "💱  Валюты" to green,
            "🏠  Недвижимость" to Color.rgb(235, 145, 45),
            "₿  Криптовалюты" to Color.rgb(30, 165, 185),
            "🏭  Бизнес и производство" to Color.rgb(225, 95, 135)
        )

        for ((name, color) in markets) {
            val item = card().apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
            }

            item.addView(
                label(name, 16f, dark, true)
            )

            val gap = View(this)

            item.addView(
                gap,
                LinearLayout.LayoutParams(0, 1, 1f)
            )

            item.addView(
                label("›", 26f, color, true)
            )

            root.addView(item)
            root.addView(space(9))
        }

        // ВАЛЮТНЫЕ КУРСЫ

        root.addView(space(8))

        root.addView(
            label(
                "Реальные валютные курсы",
                21f,
                dark,
                true
            )
        )

        root.addView(space(10))

        val currencyCard = card()

        val currencyText = label(
            "Загрузка валютных данных...",
            14f,
            dark
        )

        currencyCard.addView(currencyText)
        currencyCard.addView(space(10))

        val refreshRates = Button(this).apply {
            text = "ОБНОВИТЬ ВАЛЮТНЫЕ КУРСЫ"
            setTextColor(Color.WHITE)

            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(blue, purple)
            ).apply {
                cornerRadius = dp(14).toFloat()
            }

            setOnClickListener {
                loadCurrencyRates(currencyText)
            }
        }

        currencyCard.addView(refreshRates)
        root.addView(currencyCard)

        loadCurrencyRates(currencyText)

        root.addView(space(20))

        // ГРАФИК

        root.addView(
            label(
                "Динамика инвестиций",
                21f,
                dark,
                true
            )
        )

        root.addView(space(10))

        val chartCard = card()

        chartCard.addView(
            label("Демонстрационный график", 13f, muted)
        )

        chartCard.addView(space(8))

        chartCard.addView(
            InvestmentChart(this),
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(150)
            )
        )

        chartCard.addView(space(5))

        chartCard.addView(
            label(
                "График пока демонстрационный, не реальные котировки.",
                11f,
                muted
            )
        )

        root.addView(chartCard)
        root.addView(space(20))

        // КАЛЬКУЛЯТОР ДОХОДНОСТИ

        root.addView(
            label(
                "Калькулятор доходности",
                21f,
                dark,
                true
            )
        )

        root.addView(space(10))

        val calc = card()

        val money = input(
            "Сумма инвестиций, USD",
            InputType.TYPE_CLASS_NUMBER or
            InputType.TYPE_NUMBER_FLAG_DECIMAL
        )

        val percent = input(
            "Доходность в год, %",
            InputType.TYPE_CLASS_NUMBER or
            InputType.TYPE_NUMBER_FLAG_DECIMAL or
            InputType.TYPE_NUMBER_FLAG_SIGNED
        )

        val years = input(
            "Срок инвестирования, лет",
            InputType.TYPE_CLASS_NUMBER
        )

        calc.addView(money)
        calc.addView(space(10))

        calc.addView(percent)
        calc.addView(space(10))

        calc.addView(years)
        calc.addView(space(14))

        val result = label(
            "Здесь появится результат расчёта",
            15f,
            dark,
            true
        )

        val button = Button(this).apply {
            text = "РАССЧИТАТЬ ДОХОДНОСТЬ"
            setTextColor(Color.WHITE)

            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(blue, purple)
            ).apply {
                cornerRadius = dp(14).toFloat()
            }

            setOnClickListener {
                try {
                    val capital =
                        money.text.toString().toDouble()

                    val rate =
                        percent.text.toString().toDouble()

                    val period =
                        years.text.toString().toInt()

                    require(
                        capital > 0 &&
                        rate > -100 &&
                        period > 0
                    )

                    val total = capital *
                        Math.pow(
                            1 + rate / 100,
                            period.toDouble()
                        )

                    val profit = total - capital

                    result.text = String.format(
                        Locale.US,
                        "Итоговая сумма: $%.2f\n" +
                        "Прибыль: $%.2f\n" +
                        "Начальный капитал: $%.2f",
                        total,
                        profit,
                        capital
                    )

                } catch (e: Exception) {
                    result.text =
                        "Проверьте введённые данные."
                }
            }
        }

        calc.addView(button)
        calc.addView(space(12))
        calc.addView(result)

        root.addView(calc)
        root.addView(space(20))

        // ИНФОРМАЦИЯ

        root.addView(
            label("HAN INVEST", 20f, blue, true)
        )

        root.addView(
            label(
                "Инвестиционный анализ и оценка рисков.\n" +
                "Прогнозы не гарантируют доходность.",
                12f,
                muted
            )
        )

        scroll.addView(root)
        setContentView(scroll)
    }
}

// ДЕМОНСТРАЦИОННЫЙ ГРАФИК

class InvestmentChart(
    context: android.content.Context
) : View(context) {

    private val paint =
        Paint(Paint.ANTI_ALIAS_FLAG)

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)

        val w = width.toFloat()
        val h = height.toFloat()

        paint.color = Color.rgb(235, 239, 248)
        paint.strokeWidth = 1f

        for (i in 1..3) {
            val y = h * i / 4
            canvas.drawLine(0f, y, w, y, paint)
        }

        val points = floatArrayOf(
            0f, h * 0.72f,
            w * 0.16f, h * 0.60f,
            w * 0.32f, h * 0.68f,
            w * 0.48f, h * 0.40f,
            w * 0.64f, h * 0.49f,
            w * 0.80f, h * 0.25f,
            w, h * 0.16f
        )

        val path = Path()
        path.moveTo(points[0], points[1])

        var i = 2

        while (i < points.size) {
            path.lineTo(points[i], points[i + 1])
            i += 2
        }

        paint.style = Paint.Style.STROKE
        paint.strokeWidth = 6f
        paint.strokeCap = Paint.Cap.ROUND
        paint.strokeJoin = Paint.Join.ROUND

        paint.shader = LinearGradient(
            0f,
            0f,
            w,
            0f,
            Color.rgb(55, 105, 245),
            Color.rgb(25, 175, 115),
            Shader.TileMode.CLAMP
        )

        canvas.drawPath(path, paint)

        paint.shader = null
        paint.style = Paint.Style.FILL
    }
}
