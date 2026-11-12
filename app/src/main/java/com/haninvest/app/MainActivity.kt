
package com.haninvest.app

import android.app.Activity
import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.graphics.Color
import android.view.Gravity
import android.view.View
import android.widget.*
import android.text.InputType
import android.graphics.drawable.GradientDrawable
import android.graphics.Typeface
import android.util.Xml
import org.json.JSONObject
import org.xmlpull.v1.XmlPullParser
import java.net.HttpURLConnection
import java.net.URL
import java.util.Calendar
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
        if (bold) setTypeface(null, Typeface.BOLD)
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
            layoutParams = LinearLayout.LayoutParams(1, dp(height))
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
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty(
                "User-Agent", "HAN-INVEST-Android/2.3"
            )
            if (connection.responseCode !in 200..299) {
                throw Exception("HTTP ${connection.responseCode}")
            }
            val response = connection.inputStream
                .bufferedReader().use { it.readText() }
            return JSONObject(response)
        } finally {
            connection.disconnect()
        }
    }

    private fun getText(address: String): String {
        val connection =
            URL(address).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "GET"
            connection.connectTimeout = 20000
            connection.readTimeout = 20000
            connection.setRequestProperty(
                "User-Agent", "HAN-INVEST-Android/2.3"
            )
            if (connection.responseCode !in 200..299) {
                throw Exception("HTTP ${connection.responseCode}")
            }
            return connection.inputStream.bufferedReader()
                .use { it.readText() }
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
                runOnUiThread { target.text = result }
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
        runRequest(target, "Загрузка валютных курсов...") {
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
                            rates.getDouble(code), code, name
                        )
                    )
                }
            }
            "Дата курса: $date\n\n$out" +
                "Справочные курсы, не банковские."
        }
    }

    // ЗОЛОТО, СЕРЕБРО И НЕФТЬ

    private fun loadSnapshot(
        target: TextView,
        address: String,
        instruments: List<Pair<String, String>>
    ) {
        runRequest(target, "Загрузка рыночных данных...") {
            val json = getJson(address)
            val observations = json.getJSONArray("observations")
            val out = StringBuilder()
            var found = 0

            for ((instrumentId, title) in instruments) {
                for (i in 0 until observations.length()) {
                    val item = observations.getJSONObject(i)
                    if (item.optString("instrument_id") == instrumentId) {
                        if (item.isNull("value")) {
                            out.append("$title\nНет цены за период.\n\n")
                        } else {
                            out.append(
                                String.format(
                                    Locale.US,
                                    "%s\n%.2f\nДата: %s\n\n",
                                    title,
                                    item.getDouble("value"),
                                    item.optString("date", "")
                                )
                            )
                        }
                        found++
                        break
                    }
                }
            }

            if (found == 0) {
                "Рыночные данные пока недоступны.\n"
            } else {
                out.toString()
            } + "Данные могут публиковаться с задержкой."
        }
    }

    private fun loadGold(target: TextView) {
        loadSnapshot(
            target,
            "https://snapdata.dev/api/v1/gold/world/latest.json",
            listOf("XAU.USD.OZT" to "Золото XAU — USD за унцию")
        )
    }

    private fun loadSilver(target: TextView) {
        loadSnapshot(
            target,
            "https://snapdata.dev/api/v1/silver/world/latest.json",
            listOf("XAG.USD.OZT" to "Серебро XAG — USD за унцию")
        )
    }

    private fun loadOil(target: TextView) {
        loadSnapshot(
            target,
            "https://snapdata.dev/api/v1/crude/world/latest.json",
            listOf(
                "BRENT.USD.BBL" to "BRENT — USD за баррель",
                "WTI.USD.BBL" to "WTI — USD за баррель"
            )
        )
    }

    // БИРЖЕВЫЕ ИНДЕКСЫ

    private fun loadIndices(target: TextView) {
        runRequest(target, "Загрузка биржевых индексов...") {
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
                out.append(
                    String.format(
                        Locale.US,
                        "%s\n%.2f\nДата: %s\n\n",
                        name,
                        item.getDouble("value"),
                        item.optString("date", "")
                    )
                )
                count++
            }

            if (count == 0) {
                "Опубликованных значений пока нет."
            } else {
                out.toString()
            } + "Это индексы, а не отдельные акции."
        }
    }

    // КРИПТОВАЛЮТЫ

    private fun loadCrypto(target: TextView) {
        runRequest(target, "Загрузка криптовалют...") {
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
                    "usd_24h_change", Double.NaN
                )
                if (!price.isNaN()) {
                    out.append(
                        String.format(
                            Locale.US,
                            "%s\n$%,.2f\nИзменение за 24 часа: %.2f%%\n\n",
                            title, price, change
                        )
                    )
                }
            }
            if (out.isEmpty()) {
                "Криптовалютные данные пока недоступны."
            } else {
                out.toString()
            } + "Котировки могут иметь задержку."
        }
    }

    // ГОСУДАРСТВЕННЫЕ ОБЛИГАЦИИ США

    private fun loadTreasury(target: TextView) {
        runRequest(target, "Загрузка доходности облигаций США...") {
            val year = Calendar.getInstance().get(Calendar.YEAR)
            val address =
                "https://home.treasury.gov/resource-center/" +
                "data-chart-center/interest-rates/pages/xml" +
                "?data=daily_treasury_yield_curve" +
                "&field_tdr_date_value=$year"

            val xml = getText(address)
            val parser = Xml.newPullParser()
            parser.setInput(xml.reader())

            val fields = linkedMapOf(
                "BC_1MONTH" to "1 месяц",
                "BC_3MONTH" to "3 месяца",
                "BC_6MONTH" to "6 месяцев",
                "BC_1YEAR" to "1 год",
                "BC_2YEAR" to "2 года",
                "BC_5YEAR" to "5 лет",
                "BC_10YEAR" to "10 лет",
                "BC_20YEAR" to "20 лет",
                "BC_30YEAR" to "30 лет"
            )

            val records = mutableListOf<Map<String, String>>()
            var current = mutableMapOf<String, String>()
            var event = parser.eventType

            while (event != XmlPullParser.END_DOCUMENT) {
                if (event == XmlPullParser.START_TAG) {
                    val tag = parser.name.substringAfterLast(':')
                    if (tag == "entry") {
                        current = mutableMapOf()
                    } else if (
                        tag == "NEW_DATE" || fields.containsKey(tag)
                    ) {
                        val value = parser.nextText().trim()
                        current[tag] = value
                    }
                } else if (
                    event == XmlPullParser.END_TAG &&
                    parser.name.substringAfterLast(':') == "entry"
                ) {
                    if (current.containsKey("NEW_DATE")) {
                        records.add(current.toMap())
                    }
                }
                event = parser.next()
            }

            if (records.isEmpty()) {
                throw Exception("Нет опубликованных данных")
            }

            val latest = records.last()
            val out = StringBuilder()
            out.append(
                "Дата публикации: ${latest["NEW_DATE"]}\n\n"
            )

            for ((key, title) in fields) {
                val value = latest[key]
                if (!value.isNullOrBlank() && value != "N/A") {
                    out.append("$title: $value%\n")
                }
            }

            out.append(
                "\nЭто рыночная доходность казначейских бумаг, " +
                "а не гарантированная прибыль инвестора."
            )
            out.toString()
        }
    }

    // КАРТОЧКИ РЫНКА

    private fun addMarketCard(
        root: LinearLayout,
        title: String,
        description: String,
        loader: (TextView) -> Unit
    ) {
        root.addView(label(title, 20f, dark, true))
        root.addView(space(8))

        val box = card()
        val data = label(description, 14f, dark)
        box.addView(data)
        box.addView(space(10))

        val refresh = Button(this).apply {
            text = "ОБНОВИТЬ ДАННЫЕ"
            setTextColor(Color.WHITE)
            background = bg(blue, 13)
            setOnClickListener { loader(data) }
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
            setPadding(dp(18), dp(20), dp(18), dp(28))
            setBackgroundColor(page)
        }

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        header.addView(label("HAN", 28f, blue, true))
        header.addView(label(" INVEST", 28f, purple, true))
        root.addView(header)

        root.addView(
            label(
                "Международная инвестиционная аналитика",
                13f, muted
            )
        )
        root.addView(space(18))

        val banner = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(23), dp(20), dp(23))
            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(blue, purple)
            ).apply { cornerRadius = dp(22).toFloat() }
        }
        banner.addView(label("HAN INVEST", 13f, Color.WHITE, true))
        banner.addView(space(8))
        banner.addView(
            label("Инвестируйте\nосознанно.", 29f, Color.WHITE, true)
        )
        banner.addView(space(8))
        banner.addView(
            label(
                "Сравнивайте рынки, доходность и риски.",
                14f, Color.WHITE
            )
        )
        root.addView(banner)
        root.addView(space(12))

        // PAPER TRADING: виртуальный счёт с сохранением на телефоне
        root.addView(PaperTrading(this).createView())
        root.addView(space(22))

        // ГЛАВНЫЙ РЫНОК HAN INVEST — КРИПТОВАЛЮТЫ
        root.addView(CryptoScanner(this).createView())
        root.addView(space(22))

        root.addView(
            label("Мой инвестиционный портфель", 20f, dark, true)
        )
        root.addView(space(9))

        val portfolio = card()
        portfolio.addView(label("ОБЩИЙ КАПИТАЛ", 12f, muted, true))
        portfolio.addView(space(5))
        portfolio.addView(label("$0.00", 34f, dark, true))
        portfolio.addView(
            label(
                "Портфель пока не подключён к брокерскому счёту.",
                12f, muted
            )
        )
        root.addView(portfolio)
        root.addView(space(22))

        root.addView(label("Международные рынки", 22f, dark, true))
        root.addView(space(12))

        addMarketCard(
            root, "💱 Валюты",
            "Курсы основных мировых валют к доллару.",
            ::loadCurrencies
        )
        addMarketCard(
            root, "🥇 Золото",
            "Цена золота на мировом рынке.",
            ::loadGold
        )
        addMarketCard(
            root, "🥈 Серебро",
            "Цена серебра на мировом рынке.",
            ::loadSilver
        )
        addMarketCard(
            root, "🛢 Нефть Brent и WTI",
            "Мировые цены на нефть за баррель.",
            ::loadOil
        )
        addMarketCard(
            root, "📈 Биржевые индексы",
            "Опубликованные значения доступных индексов.",
            ::loadIndices
        )
        addMarketCard(
            root, "🏦 Облигации США",
            "Доходность казначейских облигаций от 1 месяца до 30 лет.",
            ::loadTreasury
        )

        // СКАНЕР АКЦИЙ И ETF
        root.addView(StockScanner(this).createView())
        root.addView(space(18))

        // ИСТОРИЧЕСКОЕ ТЕСТИРОВАНИЕ 2.3
        root.addView(BacktestEngine(this).createView())
        root.addView(space(18))

        root.addView(
            label("🏠 Недвижимость и бизнес", 20f, dark, true)
        )
        root.addView(space(8))
        val business = card()
        business.addView(
            label(
                "Сравнение объектов и проектов по цене, расходам, " +
                "денежному потоку и ожидаемой доходности.\n\n" +
                "Для расчёта нужны данные конкретного объекта.",
                14f, dark
            )
        )
        root.addView(business)
        root.addView(space(20))

        root.addView(label("Калькулятор доходности", 22f, dark, true))
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
            15f, dark, true
        )

        val calculate = Button(this).apply {
            text = "РАССЧИТАТЬ"
            setTextColor(Color.WHITE)
            background = bg(blue, 13)
            setOnClickListener {
                try {
                    val capital = money.text.toString().toDouble()
                    val rate = percent.text.toString().toDouble()
                    val period = years.text.toString().toInt()

                    require(
                        capital > 0 && rate > -100 &&
                        period > 0 && period <= 100
                    )

                    val total = capital * Math.pow(
                        1 + rate / 100, period.toDouble()
                    )
                    val profit = total - capital

                    result.text = String.format(
                        Locale.US,
                        "Итоговая сумма: $%,.2f\n" +
                        "Расчётная прибыль: $%,.2f\n" +
                        "Начальный капитал: $%,.2f\n\n" +
                        "Это сценарий расчёта, не гарантия дохода.",
                        total, profit, capital
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

        root.addView(
            label("Анализ и управление рисками", 20f, dark, true)
        )
        root.addView(space(8))

        val risk = card()
        risk.addView(
            label(
                "Функции HAN INVEST:\n\n" +
                "• История цен и динамика рынка\n" +
                "• Волатильность и риск потерь\n" +
                "• Сравнение активов\n" +
                "• Краткосрочные и долгосрочные сценарии\n" +
                "• Поиск инвестиционных возможностей\n\n" +
                "Результаты сканера основаны на исторических " +
                "данных и не являются гарантированным прогнозом.",
                14f, dark
            )
        )
        root.addView(risk)
        root.addView(space(20))

        root.addView(label("HAN INVEST", 22f, blue, true))
        root.addView(
            label(
                "Данные могут иметь задержку. Прогнозы не гарантируют " +
                "прибыль. Инвестиции связаны с риском потери капитала.",
                12f, muted
            )
        )

        // РАСКРЫВАЮЩИЙСЯ РАЗДЕЛ ИСТОЧНИКОВ ДАННЫХ
        val sourcesToggle = Button(this).apply {
            text = "О ПРИЛОЖЕНИИ И ИСТОЧНИКАХ ДАННЫХ"
            setTextColor(blue)
            background = bg(Color.WHITE, 13)
        }

        val sourcesInfo = label(
            "Источники рыночных данных:\n" +
            "• Валюты — Frankfurter API\n" +
            "• Золото, серебро, нефть и индексы — SnapData\n" +
            "• Криптовалюты — CoinGecko\n" +
            "• Облигации США — Министерство финансов США\n" +
            "• Акции и ETF — Alpha Vantage\n\n" +
            "Методология: приложение показывает опубликованные котировки " +
            "и рассчитывает показатели на основе исторических данных. " +
            "Данные могут иметь задержку. Анализ не гарантирует доходность.",
            12f, muted
        )

        sourcesInfo.visibility = View.GONE

        sourcesToggle.setOnClickListener {
            sourcesInfo.visibility =
                if (sourcesInfo.visibility == View.VISIBLE)
                    View.GONE
                else
                    View.VISIBLE
        }

        root.addView(space(10))
        root.addView(sourcesToggle)
        root.addView(space(6))
        root.addView(sourcesInfo)

        scroll.addView(root)
        setContentView(scroll)

        // Запускаем WorkManager только после отображения основного экрана,
        // чтобы ошибка фонового планировщика не мешала запуску интерфейса.
        try {
            BuySignalWorker.schedule(applicationContext)
        } catch (error: Exception) {
            android.util.Log.e(
                "HAN_INVEST",
                "Не удалось запланировать фоновое сканирование",
                error
            )
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2001)
        }
    }
}
