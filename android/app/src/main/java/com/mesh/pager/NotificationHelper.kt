package com.mesh.pager

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

object NotificationHelper {

    const val CHANNEL_ID = "pager_messages"
    const val CHANNEL_NAME = "Pager Messages"

    fun createChannel(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Incoming Pager messages"
                enableVibration(true)
                setShowBadge(true)
            }
            val mgr = context.getSystemService(NotificationManager::class.java)
            mgr.createNotificationChannel(channel)
        }
    }

    fun showMessageNotification(context: Context, fromSS: String, fromName: String, text: String) {
        // Open chat with this contact when tapping notification
        val intent = Intent(context, ChatActivity::class.java).apply {
            putExtra("contact_ss_id", fromSS)
            putExtra("contact_name", fromName)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pending = PendingIntent.getActivity(
            context, fromSS.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)  // Use app icon
            .setContentTitle(fromName)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setAutoCancel(true)
            .setContentIntent(pending)
            .setColor(context.getColor(R.color.accent))  // Sports-Punk orange
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .build()

        try {
            NotificationManagerCompat.from(context).notify(fromSS.hashCode(), notification)
        } catch (e: SecurityException) {
            // Notification permission not granted
        }
    }

    fun clearNotification(context: Context, fromSS: String) {
        NotificationManagerCompat.from(context).cancel(fromSS.hashCode())
    }
}
