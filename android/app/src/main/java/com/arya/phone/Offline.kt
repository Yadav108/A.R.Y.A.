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
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.SpeechService
import org.vosk.android.StorageService

/** ARYA's own offline speech model (Vosk, Apache-2.0), shipped inside the app (assets/model-en, added by the build:
 *  .github/workflows/android.yml). Unpacked once into the app's storage on first use; nothing is sent anywhere. */
object VoskModel {
    @Volatile var model: Model? = null; private set
    @Volatile private var loading = false

    fun prepare(ctx: Context) {
        if (model != null || loading) return
        val hasModel = try { ctx.assets.list("model-en")?.isNotEmpty() == true } catch (e: Exception) { false }
        if (!hasModel) return                                       // a build without the model: phone recognizer
        loading = true
        StorageService.unpack(ctx.applicationContext, "model-en", "model",
            { m -> model = m; loading = false },
            { _ -> loading = false })
    }
}

/** Listening when the laptop can't be reached. ARYA's own Vosk model when it's ready; otherwise the phone's speech
 *  recognition — an on-device one, or another installed one, picked explicitly, because with ARYA as the default
 *  assistant the system's "default recognizer" may be ARYA's own placeholder that always refuses.
 *  [onText] gets what was heard, or null (nothing heard / no recognizer). Main thread only. */
class OfflineEar(private val ctx: Context) {
    private var rec: SpeechRecognizer? = null
    private var vosk: SpeechService? = null

    init { VoskModel.prepare(ctx) }

    fun listen(onText: (String?) -> Unit) {
        stop()
        val model = VoskModel.model
        if (model != null) listenVosk(model, onText) else listenAndroid(onText)
    }

    private fun listenVosk(model: Model, onText: (String?) -> Unit) {
        val service = try { SpeechService(Recognizer(model, 16000.0f), 16000.0f) }
                      catch (e: Exception) { return listenAndroid(onText) }
        vosk = service
        var done = false
        fun finish(text: String?) { if (!done) { done = true; stop(); onText(text?.takeIf { it.isNotBlank() }) } }
        fun text(json: String?) = try { JSONObject(json ?: "{}").optString("text") } catch (e: Exception) { "" }
        service.startListening(object : org.vosk.android.RecognitionListener {
            override fun onPartialResult(hypothesis: String?) {}
            override fun onResult(hypothesis: String?) {                // the end of a sentence (a pause)
                val t = text(hypothesis)
                if (t.isNotBlank()) finish(t)
            }
            override fun onFinalResult(hypothesis: String?) = finish(text(hypothesis))
            override fun onError(exception: Exception?) = finish(null)
            override fun onTimeout() = finish(null)
        }, 10_000)                                                    // nothing said within 10 s: stop
    }

    private fun createAndroid(): SpeechRecognizer? {
        if (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx)) {
            return SpeechRecognizer.createOnDeviceSpeechRecognizer(ctx)
        }
        val services = ctx.packageManager.queryIntentServices(Intent(RecognitionService.SERVICE_INTERFACE), 0)
            .filter { it.serviceInfo.packageName != ctx.packageName }
        val s = services.firstOrNull() ?: return null
        return SpeechRecognizer.createSpeechRecognizer(ctx, ComponentName(s.serviceInfo.packageName, s.serviceInfo.name))
    }

    private fun listenAndroid(onText: (String?) -> Unit) {
        val r = createAndroid() ?: return onText(null)
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
        try { vosk?.stop(); vosk?.shutdown() } catch (e: Exception) { }
        vosk = null
        try { rec?.destroy() } catch (e: Exception) { }
        rec = null
    }
}
