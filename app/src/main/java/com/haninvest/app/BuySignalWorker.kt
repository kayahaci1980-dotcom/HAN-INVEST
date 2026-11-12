package com.haninvest.app

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequest
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

class BuySignalWorker(
    appContext: Context,
    workerParams: WorkerParameters
) : Worker(appContext, workerParams) {

    override fun doWork(): Result {
        return try {
            val connection = URL(
                "https://han-invest-api-test.onrender.com/api/v1/crypto/scan" +
                    "?limit=30&universe_limit=100&min_volume=1000000" +
                    "&risk_pct=1&capital_usdt=100"
            ).openConnection() as HttpURLConnection

            val responseText = try {
                connection.requestMethod = "GET"
                connection.connectTimeout = 20000
                connection.readTimeout = 30000
                connection.setRequestProperty("Accept", "application/json")
                connection.setRequestProperty("User-Agent", "HAN-INVEST-Android/2.5")
                if (connection.responseCode !in 200..299) {
                    throw IllegalStateException("HTTP ${connection.responseCode}")
                }
                connection.inputStream.bufferedReader().use { it.readText() }
            } finally {
                connection.disconnect()
            }

            val json = JSONObject(responseText)
            val btc = json.optJSONObject("btc_market_filter")
            val btcAllowsBuys = btc?.optBoolean("buy_allowed", false) == true
            val assets = json.optJSONArray("assets")
                ?: return Result.retry()

            val currentBuySymbols = linkedSetOf<String>()
            if (btcAllowsBuys) {
                for (i in 0 until assets.length()) {
                    val coin = assets.optJSONObject(i) ?: continue
                    val symbol = coin.optString("symbol")
                    val isConfirmedBuy =
                        coin.optString("signal") == "BUY" &&
                        coin.optBoolean("trade_ready", false) &&
                        coin.optBoolean("volume_confirmed", false)
                    if (symbol.isNotBlank() && isConfirmedBuy) {
                        currentBuySymbols.add(symbol)
                    }
                }
            }

            val prefs = applicationContext.getSharedPreferences(
                PREFS_NAME, Context.MODE_PRIVATE
            )
            val previous = prefs.getStringSet(KEY_ACTIVE_BUYS, emptySet())
                ?.toSet() ?: emptySet()

            val newSymbols = currentBuySymbols - previous
            for (symbol in newSymbols) {
                val coin = (0 until assets.length())
                    .mapNotNull { assets.optJSONObject(it) }
                    .firstOrNull { it.optString("symbol") == symbol }
                    ?: continue
                sendBuyNotification(
                    symbol = symbol,
                    entry = coin.optString("entry_price", "—"),
                    stop = coin.optString("stop_loss", "—"),
                    target = coin.optString("take_profit", "—"),
                    score = coin.optInt("score", 0)
                )
            }

            prefs.edit()
                .putStringSet(KEY_ACTIVE_BUYS, currentBuySymbols.toSet())
                .putLong(KEY_LAST_SCAN, System.currentTimeMillis())
                .apply()

            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    private fun sendBuyNotification(
        symbol: String,
        entry: String,
        stop: String,
        target: String,
        score: Int
    ) {
        if (Build.VERSION.SDK_INT >= 33 &&
            applicationContext.checkSelfPermission(
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }

        val manager = applicationContext.getSystemService(
            Context.NOTIFICATION_SERVICE
        ) as NotificationManager

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Сигналы HAN INVEST",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Уведомления о новых подтверждённых сигналах BUY"
            }
            manager.createNotificationChannel(channel)
        }

        val openApp = Intent(applicationContext, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            applicationContext,
            symbol.hashCode(),
            openApp,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val details = "Вход: $entry USDT · Stop Loss: $stop · Take Profit: $target"
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(applicationContext, CHANNEL_ID)
        } else {
            Notification.Builder(applicationContext)
        }

        val notification = builder
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("HAN INVEST: BUY — $symbol")
            .setContentText(details)
            .setStyle(Notification.BigTextStyle().bigText(
                "$details\nОценка: $score/100. Проверьте цену и риск перед сделкой."
            ))
            .setPriority(Notification.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()

        manager.notify(symbol, symbol.hashCode(), notification)
    }

    companion object {
        private const val PREFS_NAME = "han_invest_buy_alerts"
        private const val KEY_ACTIVE_BUYS = "active_buy_symbols"
        private const val KEY_LAST_SCAN = "last_scan_at"
        private const val CHANNEL_ID = "han_invest_buy_signals"
        private const val WORK_NAME = "han_invest_confirmed_buy_scan"

        fun schedule(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()

            val request = PeriodicWorkRequest.Builder(
                BuySignalWorker::class.java,
                15,
                TimeUnit.MINUTES
            )
                .setConstraints(constraints)
                .setBackoffCriteria(
                    androidx.work.BackoffPolicy.EXPONENTIAL,
                    1,
                    TimeUnit.MINUTES
                )
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME,
                ExistingPeriodicWorkPolicy.UPDATE,
                request
            )
        }
    }
}
