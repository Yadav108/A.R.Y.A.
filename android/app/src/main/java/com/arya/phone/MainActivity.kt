package com.arya.phone

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import kotlin.concurrent.thread

/** Pair once (scan the QR code ARYA shows on the laptop after "connect my phone"), then ARYA's phone page lives
 *  here and the background service keeps the connection for notifications. */
class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private var web: WebView? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var status: TextView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        Notifier.channels(this)
        VoskModel.prepare(this)                    // offline speech model unpacked once, ready before it's needed
        if (prefs.paired) {
            AryaService.start(this)
            showWeb(firstTime = false)
        } else {
            showPairing(null)
        }
    }

    // ── Pairing ──────────────────────────────────────────────────────────────

    private fun showPairing(message: String?) {
        web = null
        val pad = (20 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 2, pad, pad)
            setBackgroundColor(Color.parseColor("#0F1115"))
        }
        fun text(s: String, size: Float, color: String = "#E8E8EA") = TextView(this).apply {
            this.text = s; textSize = size; setTextColor(Color.parseColor(color)); setPadding(0, 0, 0, pad / 2)
        }
        box.addView(text("ARYA", 30f))
        box.addView(text("On the laptop, say \"connect my phone\" (or open ARYA's phone page). Then scan the QR " +
                "code it shows. With Tailscale on both devices this works from anywhere; without it, only on " +
                "your home Wi-Fi.", 16f, "#9AA0AA"))
        status = text(message ?: "", 15f, "#FFCF6E")
        box.addView(status)
        box.addView(Button(this).apply {
            text = "Scan the QR code"
            setOnClickListener { scan() }
        })
        val link = EditText(this).apply {
            hint = "…or paste the link (http://…#k=…)"
            setTextColor(Color.WHITE); setHintTextColor(Color.GRAY)
        }
        box.addView(link)
        box.addView(Button(this).apply {
            text = "Use this link"
            setOnClickListener { pair(link.text.toString()) }
        })
        setContentView(box)
    }

    private fun scan() {
        val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { code -> pair(code.rawValue ?: "") }
            .addOnFailureListener { e -> status?.text = "Scanner unavailable (${e.message}). Paste the link instead." }
    }

    private fun pair(text: String) {
        val parsed = Prefs.parsePairing(text)
        if (parsed == null) {
            status?.text = "That isn't ARYA's pairing code. It looks like http://100.x.x.x:8001/#k=…"
            return
        }
        val (base, key) = parsed
        status?.text = "Checking…"
        thread {
            try {
                val hello = Api.hello(base, key)
                val port = Uri.parse(base).port.let { if (it > 0) ":$it" else "" }
                val others = hello.optJSONArray("addresses")
                val urls = mutableListOf(base)
                if (others != null) for (i in 0 until others.length()) urls.add("http://${others.getString(i)}$port")
                prefs.urls = urls
                prefs.key = key
                prefs.lastId = hello.optLong("last")                 // don't replay old events as notifications
                prefs.pairingUrl = text.trim()
                runOnUiThread { paired() }
            } catch (e: Api.Unpaired) {
                runOnUiThread { status?.text = "This code is old. Say \"connect my phone\" on the laptop for a new one." }
            } catch (e: Exception) {
                runOnUiThread {
                    status?.text = "Can't reach the laptop (${e.message}). Is ARYA running, phone access on, and " +
                            "Tailscale on (or the same Wi-Fi)?"
                }
            }
        }
    }

    private fun paired() {
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
        askToStayAwake()
        AryaService.start(this)
        showWeb(firstTime = true)
    }

    /** Android stops background apps to save battery; ARYA needs to stay connected to deliver reminders. */
    @SuppressLint("BatteryLife")
    private fun askToStayAwake() {
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            try {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            } catch (e: Exception) { /* some phones hide this screen */ }
        }
    }

    // ── ARYA's phone page ───────────────────────────────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWeb(firstTime: Boolean) {
        val w = WebView(this).apply {
            layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
            setBackgroundColor(Color.parseColor("#0F1115"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            webViewClient = object : WebViewClient() {
                override fun onReceivedError(view: WebView, request: android.webkit.WebResourceRequest,
                                             error: android.webkit.WebResourceError) {
                    if (request.isForMainFrame) tryNextAddress(view)
                }
            }
            webChromeClient = object : WebChromeClient() {
                // The page's 📷 / 📎 / 🎙 buttons are file inputs: open Android's picker (camera, recorder, files).
                override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>,
                                               params: FileChooserParams): Boolean {
                    fileCallback?.onReceiveValue(null)
                    fileCallback = callback
                    return try {
                        startActivityForResult(params.createIntent(), FILE_REQUEST); true
                    } catch (e: Exception) {
                        fileCallback = null; false
                    }
                }
            }
        }
        web = w
        // 🎙 Talk: the voice screen, floating over the page's top-right corner.
        val d = resources.displayMetrics.density
        val talk = Button(this).apply {
            text = "🎙 Talk"
            setTextColor(Color.WHITE)
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = 24 * d; setColor(Color.parseColor("#3B6FE0"))
            }
            setOnClickListener { startActivity(Intent(this@MainActivity, TalkActivity::class.java)) }
        }
        val frame = android.widget.FrameLayout(this)
        frame.addView(w)
        frame.addView(talk, android.widget.FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP or Gravity.END
        ).apply { setMargins(0, (10 * d).toInt(), (10 * d).toInt(), 0) })
        setContentView(frame)
        // The pairing link logs the page in (it trades the key for a cookie); afterwards the plain address.
        w.loadUrl(if (firstTime && prefs.pairingUrl.isNotEmpty()) prefs.pairingUrl else prefs.urls.first())
    }

    private var tried = 0

    private fun tryNextAddress(view: WebView) {
        val urls = prefs.urls
        tried++
        if (tried < urls.size) view.loadUrl(urls[tried]) else {
            tried = 0
            view.loadData("<body style='background:#0F1115;color:#E8E8EA;font-family:sans-serif;padding:24px'>" +
                    "<h2>Can't reach ARYA</h2><p>Is the laptop awake with ARYA running? Is Tailscale on on both " +
                    "devices (or are you on the home Wi-Fi)?</p><p>Reminders still arrive as soon as the " +
                    "connection is back.</p></body>", "text/html", "utf-8")
        }
    }

    @Deprecated("Activity API")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == FILE_REQUEST) {
            fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            fileCallback = null
            return
        }
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
    }

    override fun onResume() {
        super.onResume()
        if (!prefs.paired && web != null) showPairing("Not paired any more: scan the new QR code.")
    }

    @Deprecated("Activity API")
    override fun onBackPressed() {
        val w = web
        if (w != null && w.canGoBack()) w.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    companion object {
        private const val FILE_REQUEST = 7
    }
}
