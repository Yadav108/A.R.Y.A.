package com.arya.phone

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/** Listening when the laptop can't be reached: the phone's own speech recognition, on the device where it can.
 *  ARYA is the default assistant, so the system's "default recognizer" may be ARYA's own placeholder
 *  (AssistRecognitionService, which always refuses) — so an on-device recognizer, or another installed one, is
 *  picked explicitly. [onText] gets what was heard, or null (nothing heard / no recognizer). Main thread only. */
class OfflineEar(private val ctx: Context) {
    private var rec: SpeechRecognizer? = null

    private fun create(): SpeechRecognizer? {
        if (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx)) {
            return SpeechRecognizer.createOnDeviceSpeechRecognizer(ctx)
        }
        val services = ctx.packageManager.queryIntentServices(Intent(RecognitionService.SERVICE_INTERFACE), 0)
            .filter { it.serviceInfo.packageName != ctx.packageName }
        val s = services.firstOrNull() ?: return null
        return SpeechRecognizer.createSpeechRecognizer(ctx, ComponentName(s.serviceInfo.packageName, s.serviceInfo.name))
    }

    fun listen(onText: (String?) -> Unit) {
        stop()
        val r = create() ?: return onText(null)
        rec = r
        var done = false
        fun finish(text: String?) { if (!done) { done = true; onText(text); stop() } }
        r.setRecognitionListener(object : RecognitionListener {
            override fun onResults(results: Bundle?) =
                finish(results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull())
            override fun onError(error: Int) = finish(null)
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onPartialResults(partialResults: Bundle?) {}
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        r.startListening(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1))
    }

    fun stop() {
        try { rec?.destroy() } catch (e: Exception) { }
        rec = null
    }
}
