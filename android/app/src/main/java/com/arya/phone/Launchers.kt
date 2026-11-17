package com.arya.phone

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.service.quicksettings.TileService
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.speech.RecognitionService
import android.speech.SpeechRecognizer
import android.widget.RemoteViews

private fun talkIntent(ctx: Context) = Intent(ctx, TalkActivity::class.java)
    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)

/** Quick Settings tile "Talk to ARYA" (pull down the shade, tap). */
class TalkTile : TileService() {
    override fun onClick() {
        super.onClick()
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, talkIntent(this), PendingIntent.FLAG_IMMUTABLE))
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(talkIntent(this))
        }
    }
}

/** Home-screen widget: one tap to talk. */
class TalkWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, manager: AppWidgetManager, ids: IntArray) {
        val tap = PendingIntent.getActivity(ctx, 0, talkIntent(ctx), PendingIntent.FLAG_IMMUTABLE)
        for (id in ids) {
            val views = RemoteViews(ctx.packageName, R.layout.widget_talk)
            views.setOnClickPendingIntent(R.id.widget_talk, tap)
            manager.updateAppWidget(id, views)
        }
    }
}

// ── Android's assistant (Settings → Apps → Default apps → Digital assistant app → ARYA) ────────────────────────
// Long-pressing the power button (or the earbuds' assistant gesture) then opens the Talk screen.

class AssistService : VoiceInteractionService()

class AssistSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = AssistSession(this)
}

class AssistSession(ctx: Context) : VoiceInteractionSession(ctx) {
    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        startAssistantActivity(Intent(context, TalkActivity::class.java))
        hide()
    }
}

/** Android requires an assistant to name a speech recognizer; ARYA's listening happens on the laptop, so other
 *  apps asking this one to recognise speech are told it isn't available. */
class AssistRecognitionService : RecognitionService() {
    override fun onStartListening(intent: Intent?, listener: Callback?) {
        listener?.error(SpeechRecognizer.ERROR_CLIENT)
    }
    override fun onStopListening(listener: Callback?) {}
    override fun onCancel(listener: Callback?) {}
}
