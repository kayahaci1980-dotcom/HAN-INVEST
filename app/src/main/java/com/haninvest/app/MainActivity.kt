
package com.haninvest.app

import android.app.Activity
import android.os.Bundle
import android.graphics.Color
import android.view.Gravity
import android.widget.*

class MainActivity : Activity() {

    private val dark = Color.rgb(15, 23, 42)
    private val green = Color.rgb(22, 163, 74)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scroll = ScrollView(this)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(24, 30, 24, 30)
            setBackgroundColor(dark)
        }

        fun text(value: String, size: Float, color: Int) =
            TextView(this).apply {
                text = value
                textSize = size
                setTextColor(color)
                setPadding(0, 12, 0, 12)
            }

        layout.addView(text("HAN INVEST", 30f, Color.WHITE))
        layout.addView(text(
            "Умная система инвестиционного анализа",
            16f, Color.LTGRAY
        ))

        layout.addView(text("ГЛАВНАЯ ПАНЕЛЬ", 18f, green))
        layout.addView(text(
            "Рынки: Все рынки\n" +
            "Режим: Самостоятельный анализ\n" +
            "Капитал: Любая сумма",
            16f, Color.WHITE
        ))

        layout.addView(text("ИНВЕСТИЦИОННЫЙ КАЛЬКУЛЯТОР",
            18f, green))

        val capital = EditText(this).apply {
            hint = "Сумма вложений ($)"
            inputType = 8194
            setTextColor(Color.WHITE)
            setHintTextColor(Color.LTGRAY)
        }

        val rate = EditText(this).apply {
            hint = "Ожидаемая доходность в год (%)"
            inputType = 8194
            setTextColor(Color.WHITE)
            setHintTextColor(Color.LTGRAY)
        }

        val years = EditText(this).apply {
            hint = "Срок инвестирования (лет)"
            inputType = 2
            setTextColor(Color.WHITE)
            setHintTextColor(Color.LTGRAY)
        }

        layout.addView(capital)
        layout.addView(rate)
        layout.addView(years)

        val result = text("Здесь появится расчёт", 17f, Color.WHITE)

        val calculate = Button(this).apply {
            text = "РАССЧИТАТЬ ДОХОДНОСТЬ"
            setOnClickListener {
                try {
                    val money = capital.text.toString().toDouble()
                    val percent = rate.text.toString().toDouble()
                    val period = years.text.toString().toInt()

                    require(money > 0 && percent > -100 && period > 0)

                    val future = money *
                        Math.pow(1 + percent / 100, period.toDouble())
                    val profit = future - money

                    result.text = String.format(
                        java.util.Locale.US,
                        "Итоговая сумма: $%.2f\n" +
                        "Прибыль: $%.2f\n" +
                        "Первоначальный капитал: $%.2f\n\n" +
                        "Расчёт теоретический, без учёта налогов и комиссий.",
                        future, profit, money
                    )
                } catch (e: Exception) {
                    result.text = "Проверьте введённые данные."
                }
            }
        }

        layout.addView(calculate)
        layout.addView(result)

        layout.addView(text("НАПРАВЛЕНИЯ АНАЛИЗА", 18f, green))
        layout.addView(text(
            "• Акции и фондовый рынок\n" +
            "• Валюты и золото\n" +
            "• Недвижимость\n" +
            "• Бизнес и производство\n" +
            "• Криптовалюты\n" +
            "• Международные инвестиции",
            16f, Color.WHITE
        ))

        layout.addView(text(
            "HAN INVEST v1.0\nИнвестиции требуют оценки рисков.",
            13f, Color.LTGRAY
        ))

        scroll.addView(layout)
        setContentView(scroll)
    }
}
