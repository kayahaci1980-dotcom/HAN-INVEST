
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

    private val blue = Color.rgb(76, 67, 245)
    private val purple = Color.rgb(177, 66, 255)
    private val green = Color.rgb(24, 220, 130)
    private val dark = Color.rgb(242, 244, 255)
    private val muted = Color.rgb(158, 169, 197)
    private val page = Color.rgb(8, 12, 30)

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
            background = bg(Color.rgb(17, 25, 52), 19)
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
        BuySignalWorker.schedule(this)

        window.statusBarColor = page
        window.navigationBarColor = page
        window.decorView.systemUiVisibility = 0

        val scroll = ScrollView(this).apply { isFillViewport = true }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(18), dp(16), dp(24))
            setBackgroundColor(page)
        }

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val brand = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        val brandLine = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        brandLine.addView(label("◢", 27f, purple, true))
        brandLine.addView(label(" HAN", 29f, Color.WHITE, true))
        brandLine.addView(label(" INVEST", 29f, purple, true))
        brand.addView(brandLine)
        brand.addView(label("Международная инвестиционная аналитика", 12f, muted))
        header.addView(brand)
        val notification = TextView(this).apply {
            text = "♧"
            textSize = 25f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            setPadding(dp(8), dp(5), dp(8), dp(5))
        }
        val settings = TextView(this).apply {
            text = "⚙"
            textSize = 25f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            setPadding(dp(8), dp(5), dp(4), dp(5))
        }
        header.addView(notification)
        header.addView(settings)
        root.addView(header)
        root.addView(space(14))

        val banner = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(20), dp(18), dp(20))
            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(Color.rgb(54, 39, 140), Color.rgb(94, 42, 190), Color.rgb(10, 95, 160))
            ).apply { cornerRadius = dp(20).toFloat() }
        }
        banner.addView(label("Инвестируйте\nосознанно.", 27f, Color.WHITE, true))
        banner.addView(space(6))
        banner.addView(label("Анализ рынков • Сигналы • Виртуальная торговля", 13f, Color.WHITE))
        root.addView(banner)
        root.addView(space(14))

        root.addView(CryptoScanner(this).createView())
        scroll.addView(root)
        setContentView(scroll)

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2001)
        }
    }
}
