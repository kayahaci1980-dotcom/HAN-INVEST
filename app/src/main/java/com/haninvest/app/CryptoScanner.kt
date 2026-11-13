package com.haninvest.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

class CryptoScanner(private val context: Context) {
    private val blue = Color.rgb(55,105,245)
    private val dark = Color.rgb(30,42,65)
    private val muted = Color.rgb(125,137,158)
    private fun dp(v:Int) = (v*context.resources.displayMetrics.density).toInt()
    private fun label(v:String,s:Float,c:Int,b:Boolean=false)=TextView(context).apply { text=v;textSize=s;setTextColor(c);if(b)setTypeface(null,Typeface.BOLD) }
    private fun fetch(url:String):String {
        val c=URL(url).openConnection() as HttpURLConnection
        return try { c.connectTimeout=15000;c.readTimeout=25000;c.setRequestProperty("User-Agent","HAN-INVEST-Android/2.8");if(c.responseCode !in 200..299)throw Exception("HTTP "+c.responseCode);c.inputStream.bufferedReader().use{it.readText()} } finally { c.disconnect() }
    }
    fun createView():View {
        val root=LinearLayout(context).apply{orientation=LinearLayout.VERTICAL}
        root.addView(label("₿ Криптосканер HAN INVEST",21f,dark,true))
        root.addView(label("Топ-3 отдельно по каждой бирже. Coinbase и OKX ранжируются по 24-часовому обороту; это не технический сигнал BUY.",13f,muted))
        val panel=LinearLayout(context).apply{orientation=LinearLayout.VERTICAL;setPadding(dp(14),dp(14),dp(14),dp(14));setBackgroundColor(Color.WHITE);elevation=dp(2).toFloat()}
        val tabs=LinearLayout(context).apply{orientation=LinearLayout.HORIZONTAL}
        val status=label("Загрузка Binance...",14f,dark)
        var selected="binance"
        fun render(json:JSONObject,venue:String) {
            val arr=json.optJSONArray("assets")?:JSONArray()
            if(arr.length()==0){status.text="Нет доступных торговых пар.";return}
            val out=StringBuilder()
            out.append(venue.uppercase(Locale.US)).append(" — ТОП-3\n")
            if(venue=="coinbase"||venue=="okx") out.append("Рейтинг по 24ч обороту; не сигнал покупки.\n\n")
            for(i in 0 until minOf(3,arr.length())) {
                val a=arr.optJSONObject(i)?:continue
                out.append(i+1).append(". ").append(a.optString("symbol")).append("\n")
                out.append(String.format(Locale.US,"Цена: $%.8f\n",a.optDouble("price")))
                out.append(String.format(Locale.US,"Изменение 24ч: %.2f%%\n",a.optDouble("change_24h_pct")))
                if(a.has("score"))out.append("Оценка: ").append(a.optInt("score")).append("/100 | ").append(a.optString("signal","WATCH")).append("\n")
                else out.append("Оборот 24ч: ").append(a.optString("volume_24h","—")).append("\n")
                out.append("\n")
            }
            out.append("Обновлено: ").append(json.optString("updated_at","сейчас"))
            status.text=out.toString()
        }
        fun load(venue:String) {
            selected=venue;status.text="Загрузка "+venue+"..."
            Thread {
                try {
                    val json=when(venue) {
                        "binance"->JSONObject(fetch("https://han-invest-api-test.onrender.com/api/v1/crypto/scan?limit=30&universe_limit=100&min_volume=1000000&risk_pct=1"))
                        "bybit"->JSONObject(fetch("https://han-invest-api-test.onrender.com/api/v1/crypto/bybit/scan?limit=30&min_volume=1000000"))
                        "okx"->{
                            val list=JSONObject(fetch("https://www.okx.com/api/v5/market/tickers?instType=SPOT")).optJSONArray("data")?:JSONArray()
                            val rows=mutableListOf<JSONObject>()
                            for(i in 0 until list.length()){
                                val x=list.optJSONObject(i)?:continue;val pair=x.optString("instId");if(!pair.endsWith("-USDT"))continue
                                val price=x.optString("last").toDoubleOrNull()?:continue;val vol=x.optString("volCcy24h").toDoubleOrNull()?:0.0;val open=x.optString("open24h").toDoubleOrNull()?:0.0
                                rows.add(JSONObject().put("symbol",pair.replace("-","")).put("price",price).put("change_24h_pct",if(open>0)(price-open)/open*100 else 0.0).put("volume_24h",String.format(Locale.US,"$%.0f",vol)).put("_vol",vol))
                            }
                            rows.sortByDescending{it.optDouble("_vol")}
                            JSONObject().put("assets",JSONArray().apply{rows.forEach{put(it)}}).put("updated_at","текущие котировки OKX")
                        }
                        else->{
                            val products=JSONArray(fetch("https://api.exchange.coinbase.com/products"))
                            val rows=mutableListOf<JSONObject>()
                            for(i in 0 until products.length()){
                                val product=products.optJSONObject(i)?:continue;val base=product.optString("base_currency");val quote=product.optString("quote_currency")
                                if(base.isBlank()||(quote!="USD"&&quote!="USDT")||product.optBoolean("trading_disabled"))continue
                                try{
                                    val id=product.optString("id");val ticker=JSONObject(fetch("https://api.exchange.coinbase.com/products/"+id+"/ticker"));val stats=JSONObject(fetch("https://api.exchange.coinbase.com/products/"+id+"/stats"))
                                    val price=ticker.optString("price").toDoubleOrNull()?:continue;val vol=stats.optString("volume").toDoubleOrNull()?:0.0;val open=stats.optString("open").toDoubleOrNull()?:0.0
                                    rows.add(JSONObject().put("symbol",base+quote).put("price",price).put("change_24h_pct",if(open>0)(price-open)/open*100 else 0.0).put("volume_24h",String.format(Locale.US,"%.2f %s",vol,base)).put("_vol",vol))
                                }catch(_:Exception){}
                            }
                            rows.sortByDescending{it.optDouble("_vol")}
                            JSONObject().put("assets",JSONArray().apply{rows.forEach{put(it)}}).put("updated_at","текущие котировки Coinbase")
                        }
                    }
                    (context as? android.app.Activity)?.runOnUiThread{if(selected==venue)render(json,venue)}
                }catch(e:Exception){(context as? android.app.Activity)?.runOnUiThread{if(selected==venue)status.text=venue+" временно недоступна: "+(e.message?: "ошибка")+". Чужие цены не подставляются."}}
            }.start()
        }
        val venues=listOf("BINANCE" to "binance","BYBIT" to "bybit","COINBASE" to "coinbase","OKX" to "okx")
        for((title,key) in venues)tabs.addView(Button(context).apply{text=title;textSize=10f;isAllCaps=false;setTextColor(Color.WHITE);setBackgroundColor(blue);setOnClickListener{load(key)}},LinearLayout.LayoutParams(0,dp(44),1f))
        panel.addView(tabs);status.setPadding(0,dp(14),0,dp(10));panel.addView(status)
        panel.addView(Button(context).apply{text="ОБНОВИТЬ ВЫБРАННУЮ БИРЖУ";setOnClickListener{load(selected)}})
        root.addView(panel);load("binance");return root
    }
}