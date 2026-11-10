
package com.haninvest.app

import android.app.Activity
import android.os.Bundle
import android.graphics.Color
import android.view.Gravity
import android.view.View
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

    private fun dp(v: Int) =
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
        if (bold) {
            setTypeface(
                null,
                android.graphics.Typeface.BOLD
            )
        }
    }

    private fun card() =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(17), dp(15), dp(17), dp(15))
            background = bg(Color.WHITE, 19)
            elevation = dp(2).toFloat()
        }

    private fun space(height: Int = 12) =
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
            setPadding(dp(13), dp(11), dp(13), dp(11))
        }

    private fun getJson(address: String): JSONObject {
        val connection =
            URL(address).openConnection() as HttpURLConnection

        try {
            connection.requestMethod = "GET"
            connection.connectTimeout = 15000
            connection.readTimeout = 15000
            connection.setRequestProperty(
                "Accept",
                "application/json"
            )
            connection.setRequestProperty(
                "User-Agent",
                "HAN-INVEST-Android/1.0"
            )

            val code = connection.responseCode

            if (code !in 200..299) {
                throw Exception("HTTP $code")
            }

            val response = connection.inputStream
                .bufferedReader()
                .use { it.readText() }

            return JSONObject(response)

        } finally {
            connection.disconnect()
        }
    }

    private fun runRequest(
        target: TextView,
        loading: String,
        request: () -> String
    ) {
        target.text = loading

        Thread {
            try {
                val result = request()

                runOnUiThread {
                    target.text = result
                }

            } catch (e: Exception) {
                runOnUiThread {
                    target.text =
                        "Данные временно недоступны.\n" +
                        "Проверьте интернет и попробуйте обновить."
                }
            }
        }.start()
    }

    // ВАЛЮТЫ

    private fun loadCurrencies(target: TextView) {
        runRequest(
            target,
            "Загрузка валютных курсов..."
        ) {
            val json = getJson(
                "https://api.frankfurter.dev/v1/latest?base=USD"
            )

            val rates = json.getJSONObject("rates")
            val date = json.optString("date", "не указана")

            val currencies = listOf(
                "EUR" to "Евро",
                "GBP" to "Фунт стерлингов",
                "CNY" to "Китайский юань",
                "TRY" to "Турецкая лира",
                "CHF" to "Швейцарский франк",
                "JPY" to "Японская иена"
            )

            val out = StringBuilder()

            for ((code, name) in currencies) {
                if (rates.has(code)) {
                    out.append(
                        String.format(
                            Locale.US,
                            "1 USD = %.4f %s\n%s\n\n",
                            rates.getDouble(code),
                            code,
                            name
                        )
                    )
                }
            }

            "Дата курса: $date\n\n$out" +
            "Источник: Frankfurter\n" +
            "Справочные курсы, не банковские."
        }
    }

    // ЗОЛОТО, СЕРЕБРО И НЕФТЬ

    private fun loadSnapshot(
        target: TextView,
        address: String,
        instruments: List<Pair<String, String>>,
        sourceName: String
    ) {
        runRequest(
            target,
            "Загрузка рыночных данных..."
        ) {
            val json = getJson(address)
            val observations = json.getJSONArray("observations")
            val out = StringBuilder()
            var found = 0

            for ((instrumentId, title) in instruments) {
                for (i in 0 until observations.length()) {
                    val item = observations.getJSONObject(i)

                    if (
                        item.optString("instrument_id") ==
                        instrumentId
                    ) {
                        if (item.isNull("value")) {
                            out.append(
                                "$title\nНет цены за последний период.\n\n"
                            )
                        } else {
                            val value = item.getDouble("value")
                            val date = item.optString("date", "")
                            val status = item.optString("status", "")

                            out.append(
                                String.format(
                                    Locale.US,
                                    "%s\n%.2f\nДата: %s\nСтатус: %s\n\n",
                                    title,
                                    value,
                                    date,
                                    status
                                )
                            )
                        }

                        found++
                        break
                    }
                }
            }

            if (found == 0) {
                "В ответе источника не найдены нужные инструменты.\n"
            } else {
                out.toString()
            } +
            "Источник: $sourceName\n" +
            "Данные дневные, возможна задержка публикации."
        }
    }

    private fun loadGold(target: TextView) {
        loadSnapshot(
            target,
            "https://snapdata.dev/api/v1/gold/world/latest.json",
            listOf(
                "XAU.USD.OZT" to
                    "Золото XAU — USD за тройскую унцию"
            ),
            "Snapdata / публичные источники"
        )
    }

    private fun loadSilver(target: TextView) {
        loadSnapshot(
            target,
            "https://snapdata.dev/api/v1/silver/world/latest.json",
            listOf(
                "XAG.USD.OZT" to
                    "Серебро XAG — USD за тройскую унцию"
            ),
            "Snapdata"
        )
    }

    private fun loadOil(target: TextView) {
        loadSnapshot(
            target,
            "https://snapdata.dev/api/v1/crude/world/latest.json",
            listOf(
                "BRENT.USD.BBL" to
                    "BRENT — USD за баррель",
                "WTI.USD.BBL" to
                    "WTI — USD за баррель"
            ),
            "Snapdata / EIA"
        )
    }

    // БИРЖЕВЫЕ ИНДЕКСЫ

    private fun loadIndices(target: TextView) {
        runRequest(
            target,
            "Загрузка биржевых индексов..."
        ) {
            val json = getJson(
                "https://snapdata.dev/api/v1/equity-indices/in/latest.json"
            )

            val observations = json.getJSONArray("observations")
            val out = StringBuilder()
            var count = 0

            for (i in 0 until observations.length()) {
                val item = observations.getJSONObject(i)

                if (item.isNull("value")) continue

                val name = item.optString(
                    "instrument",
                    item.optString("instrument_id")
                )

                val value = item.getDouble("value")
                val date = item.optString("date", "")

                out.append(
                    String.format(
                        Locale.US,
                        "%s\n%.2f\nДата: %s\n\n",
                        name,
                        value,
                        date
                    )
                )

                count++
            }

            if (count == 0) {
                "На данный момент нет опубликованных значений."
            } else {
                out.toString()
            } +
            "Источник: Snapdata\n" +
            "Это индексы, а не котировки отдельных акций."
        }
    }

    // КРИПТОВАЛЮТЫ

    private fun loadCrypto(target: TextView) {
        runRequest(
            target,
            "Загрузка криптовалют..."
        ) {
            val json = getJson(
                "https://api.coingecko.com/api/v3/simple/price" +
                "?ids=bitcoin,ethereum,solana,binancecoin" +
                "&vs_currencies=usd&include_24hr_change=true"
            )

            val coins = listOf(
                "bitcoin" to "Bitcoin (BTC)",
                "ethereum" to "Ethereum (ETH)",
                "solana" to "Solana (SOL)",
                "binancecoin" to "BNB"
            )

            val out = StringBuilder()

            for ((id, title) in coins) {
                if (!json.has(id)) continue

                val coin = json.getJSONObject(id)
                val price = coin.optDouble("usd", Double.NaN)
                val change = coin.optDouble(
                    "usd_24h_change",
                    Double.NaN
                )

                if (!price.isNaN()) {
                    out.append(
                        String.format(
                            Locale.US,
                            "%s\n$%,.2f\nИзменение за 24 часа: %.2f%%\n\n",
                            title,
                            price,
                            change
                        )
                    )
                }
            }

            if (out.isEmpty()) {
                "Криптовалютные данные пока недоступны."
            } else {
                out.toString()
            } +
            "Источник: CoinGecko\n" +
            "Котировки могут иметь задержку."
        }
    }

    // КАРТОЧКА РЫНКА

    private fun addMarketCard(
        root: LinearLayout,
        title: String,
        description: String,
        loader: (TextView) -> Unit
    ) {
        root.addView(
            label(title, 20f, dark, true)
        )
        root.addView(space(8))

        val box = card()
        val data = label(description, 14f, dark)

        box.addView(data)
        box.addView(space(10))

        val refresh = Button(this).apply {
            text = "ОБНОВИТЬ ДАННЫЕ"
            setTextColor(Color.WHITE)
            background = bg(blue, 13)

            setOnClickListener {
                loader(data)
            }
        }

        box.addView(refresh)
        root.addView(box)
        root.addView(space(17))

        loader(data)
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
                dp(18),
                dp(20),
                dp(18),
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
            label("HAN", 28f, blue, true)
        )

        header.addView(
            label(" INVEST", 28f, purple, true)
        )

        root.addView(header)

        root.addView(
            label(
                "Международная инвестиционная аналитика",
                13f,
                muted
            )
        )

        root.addView(space(18))

        // БАННЕР

        val banner = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(23), dp(20), dp(23))

            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(blue, purple)
            ).apply {
                cornerRadius = dp(22).toFloat()
            }
        }

        banner.addView(
            label(
                "HAN INVEST",
                13f,
                Color.WHITE,
                true
            )
        )

        banner.addView(space(8))

        banner.addView(
            label(
                "Инвестируйте\nосознанно.",
                29f,
                Color.WHITE,
                true
            )
        )

        banner.addView(space(8))

        banner.addView(
            label(
                "Сравнивайте рынки, доходность и риски.",
                14f,
                Color.WHITE
            )
        )

        root.addView(banner)
        root.addView(space(22))

        // ПОРТФЕЛЬ

        root.addView(
            label("Мой инвестиционный портфель", 20f, dark, true)
        )

        root.addView(space(9))

        val portfolio = card()

        portfolio.addView(
            label("ОБЩИЙ КАПИТАЛ", 12f, muted, true)
        )

        portfolio.addView(space(5))

        portfolio.addView(
            label("$0.00", 34f, dark, true)
        )

        portfolio.addView(
            label(
                "Портфель пока не подключён к брокерскому счёту.",
                12f,
                muted
            )
        )

        root.addView(portfolio)
        root.addView(space(22))

        // РЫНОЧНЫЕ ДАННЫЕ

        root.addView(
            label("Международные рынки", 22f, dark, true)
        )

        root.addView(space(12))

        addMarketCard(
            root,
            "💱 Валюты",
            "Курсы основных мировых валют к доллару.",
            ::loadCurrencies
        )

        addMarketCard(
            root,
            "🥇 Золото",
            "Цена золота на мировом рынке.",
            ::loadGold
        )

        addMarketCard(
            root,
            "🥈 Серебро",
            "Цена серебра на мировом рынке.",
            ::loadSilver
        )

        addMarketCard(
            root,
            "🛢 Нефть Brent и WTI",
            "Мировые цены на нефть за баррель.",
            ::loadOil
        )

        addMarketCard(
            root,
            "₿ Криптовалюты",
            "BTC, ETH, SOL и BNB. Изменение за 24 часа.",
            ::loadCrypto
        )

        addMarketCard(
            root,
            "📈 Биржевые индексы",
            "Опубликованные значения доступных индексов.",
            ::loadIndices
        )

        // АКЦИИ

        root.addView(
            label("📊 Отдельные акции", 20f, dark, true)
        )

        root.addView(space(8))

        val stocks = card()

        stocks.addView(
            label(
                "Следующий этап подключения:\n" +
                "США, Европа, Азия и другие биржи.\n\n" +
                "Индивидуальные котировки акций пока не подключены. " +
                "Мы не показываем вместо них вымышленные цены.",
                14f,
                dark
            )
        )

        root.addView(stocks)
        root.addView(space(18))

        // НЕДВИЖИМОСТЬ И БИЗНЕС

        root.addView(
            label("🏠 Недвижимость и бизнес", 20f, dark, true)
        )

        root.addView(space(8))

        val business = card()

        business.addView(
            label(
                "Сравнение объектов и проектов по цене, " +
                "расходам, денежному потоку и ожидаемой доходности.\n\n" +
                "Для расчёта потребуются данные конкретного объекта " +
                "или бизнеса.",
                14f,
                dark
            )
        )

        root.addView(business)
        root.addView(space(20))

        // КАЛЬКУЛЯТОР

        root.addView(
            label("Калькулятор доходности", 22f, dark, true)
        )

        root.addView(space(10))

        val calc = card()

        val money = input(
            "Сумма инвестиций, USD",
            InputType.TYPE_CLASS_NUMBER or
            InputType.TYPE_NUMBER_FLAG_DECIMAL
        )

        val percent = input(
            "Предполагаемая годовая доходность, %",
            InputType.TYPE_CLASS_NUMBER or
            InputType.TYPE_NUMBER_FLAG_DECIMAL or
            InputType.TYPE_NUMBER_FLAG_SIGNED
        )

        val years = input(
            "Срок инвестирования, лет",
            InputType.TYPE_CLASS_NUMBER
        )

        calc.addView(money)
        calc.addView(space(9))
        calc.addView(percent)
        calc.addView(space(9))
        calc.addView(years)
        calc.addView(space(12))

        val result = label(
            "Введите сумму, доходность и срок.",
            15f,
            dark,
            true
        )

        val calculate = Button(this).apply {
            text = "РАССЧИТАТЬ"
            setTextColor(Color.WHITE)
            background = bg(blue, 13)

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
                        period > 0 &&
                        period <= 100
                    )

                    val total = capital *
                        Math.pow(
                            1 + rate / 100,
                            period.toDouble()
                        )

                    val profit = total - capital

                    result.text = String.format(
                        Locale.US,
                        "Итоговая сумма: $%,.2f\n" +
                        "Расчётная прибыль: $%,.2f\n" +
                        "Начальный капитал: $%,.2f\n\n" +
                        "Это математический сценарий, не гарантия дохода.",
                        total,
                        profit,
                        capital
                    )

                } catch (e: Exception) {
                    result.text =
                        "Проверьте сумму, процент и срок."
                }
            }
        }

        calc.addView(calculate)
        calc.addView(space(12))
        calc.addView(result)

        root.addView(calc)
        root.addView(space(20))

        // АНАЛИТИКА

        root.addView(
            label("Анализ и управление рисками", 20f, dark, true)
        )

        root.addView(space(8))

        val risk = card()

        risk.addView(
            label(
                "HAN INVEST будет развивать систему оценки:\n\n" +
                "• История цен и динамика рынка\n" +
                "• Волатильность и риск потерь\n" +
                "• Сравнение активов\n" +
                "• Краткосрочные и долгосрочные сценарии\n" +
                "• Возможные точки входа и выхода\n\n" +
                "Автоматические рекомендации и прогнозы " +
                "будут добавлены после подключения исторических " +
                "данных и проверки алгоритмов.",
                14f,
                dark
            )
        )

        root.addView(risk)
        root.addView(space(20))

        root.addView(
            label("HAN INVEST", 22f, blue, true)
        )

        root.addView(
            label(
                "Данные могут иметь задержку. " +
                "Прогнозы не гарантируют прибыль. " +
                "Инвестиции связаны с риском потери капитала.",
                12f,
                muted
            )
        )

        scroll.addView(root)
        setContentView(scroll)
    }
}
