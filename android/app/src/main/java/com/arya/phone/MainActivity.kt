package com.arya.phone

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
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
        val d = resources.displayMetrics.density
        val pad = (24 * d).toInt()
        Theme.edgeToEdge(window)
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(pad, pad, pad, pad)
            fitsSystemWindows = true
        }
        val screen = android.widget.ScrollView(this).apply {
            background = Theme.backdrop()
            isFillViewport = true
            addView(box)
        }
        fun text(s: String, size: Float, color: Int = Theme.TEXT) = TextView(this).apply {
            this.text = s; textSize = size; setTextColor(color); gravity = Gravity.CENTER
            setLineSpacing(0f, 1.2f); setPadding(0, 0, 0, pad / 2)
        }
        fun gap(dp: Int) = View(this).apply { layoutParams = LinearLayout.LayoutParams(1, (dp * d).toInt()) }
        val wide = { LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT) }
        box.addView(gap(40))
        box.addView(OrbView(this), LinearLayout.LayoutParams((170 * d).toInt(), (170 * d).toInt()))
        box.addView(gap(8))
        box.addView(Theme.wordmark(this, 30f))
        box.addView(gap(6))
        box.addView(text("Your personal assistant, on your phone.", 16f, Theme.MUTED))
        box.addView(gap(18))
        box.addView(text("On the laptop, say \"connect my phone\" (or open ARYA's phone page), then scan the QR " +
                "code it shows. With Tailscale on both devices this works from anywhere; without it, only on " +
                "your home Wi-Fi.", 14.5f, Theme.MUTED))
        status = text(message ?: "", 14.5f, Theme.WARN)
        box.addView(status)
        box.addView(View(this), LinearLayout.LayoutParams(1, 0, 1f))      // push the buttons down
        box.addView(Theme.pill(this, "Scan the QR code", primary = true) { scan() }, wide())
        box.addView(gap(14))
        val link = EditText(this).apply {
            hint = "…or paste the link (http://…#k=…)"
            textSize = 15f
            setTextColor(Theme.TEXT); setHintTextColor(Theme.MUTED)
            background = Theme.glass(18 * d)
            setPadding((18 * d).toInt(), (14 * d).toInt(), (18 * d).toInt(), (14 * d).toInt())
            isSingleLine = true
        }
        box.addView(link, wide())
        box.addView(gap(10))
        box.addView(Theme.pill(this, "Use this link", primary = false) { pair(link.text.toString()) }, wide())
        setContentView(screen)
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
            setBackgroundColor(Theme.BG)
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
        Theme.edgeToEdge(window)
        // 🎙 Talk: the voice screen, floating over the page's top-right corner — until the page says it has its own
        // mic button (the new page calls AryaApp.pageHasTalk(); an older laptop page doesn't, so this stays).
        val d = resources.displayMetrics.density
        val talk = Theme.pill(this, "🎙 Talk", primary = true) {
            startActivity(Intent(this@MainActivity, TalkActivity::class.java))
        }.apply { setPadding((16 * d).toInt(), (10 * d).toInt(), (16 * d).toInt(), (10 * d).toInt()) }
        talkButton = talk
        // The page may only open the talk screen or hide this button: nothing else crosses into the app.
        w.addJavascriptInterface(object {
            @android.webkit.JavascriptInterface
            fun talk() { runOnUiThread { startActivity(Intent(this@MainActivity, TalkActivity::class.java)) } }
            @android.webkit.JavascriptInterface
            fun pageHasTalk() { runOnUiThread { talkButton?.visibility = View.GONE } }
        }, "AryaApp")
        val frame = android.widget.FrameLayout(this)
        frame.addView(w)
        frame.addView(talk, android.widget.FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP or Gravity.END
        ).apply { setMargins(0, (10 * d).toInt(), (10 * d).toInt(), 0) })
        setContentView(frame)
        shareLogin()
        // The pairing link logs the page in (it trades the key for a cookie); afterwards the plain address.
        w.loadUrl(if (firstTime && prefs.pairingUrl.isNotEmpty()) prefs.pairingUrl else prefs.urls.first())
    }

    /** The page logs in with a cookie, and a cookie belongs to one address. The app already holds the key, so it
     *  gives the page that cookie on every address of the laptop (Tailscale and home Wi-Fi): switching between them,
     *  or a cleared cookie, no longer shows "Not paired". */
    private fun shareLogin() {
        if (prefs.key.isEmpty()) return
        val cookies = android.webkit.CookieManager.getInstance()
        cookies.setAcceptCookie(true)
        for (url in prefs.urls) {
            cookies.setCookie(url, "arya_phone=${prefs.key}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict")
        }
        cookies.flush()
    }

    private var tried = 0
    private var talkButton: View? = null

    private fun tryNextAddress(view: WebView) {
        val urls = prefs.urls
        tried++
        if (tried < urls.size) view.loadUrl(urls[tried]) else {
            tried = 0
            talkButton?.visibility = View.VISIBLE                 // the error page has no mic of its own
            view.loadData("<meta name='viewport' content='width=device-width,initial-scale=1'>" +
                    "<body style='margin:0;min-height:100vh;display:flex;flex-direction:column;justify-content:center;" +
                    "padding:28px;box-sizing:border-box;background:radial-gradient(60% 40% at 15% 5%,#7c8cff33,transparent)," +
                    "radial-gradient(50% 40% at 90% 95%,#b07cff26,transparent),#07080c;color:#eef0f6;" +
                    "font:16px/1.5 system-ui,sans-serif'><div style='width:72px;height:72px;border-radius:50%;" +
                    "margin-bottom:22px;background:radial-gradient(circle at 32% 28%,#fff 0 6%,#7c8cff 30%,#b07cff 62%,#1b1440);" +
                    "opacity:.55;filter:grayscale(.4)'></div><h2 style='margin:0 0 10px;font-size:24px'>Can't reach ARYA</h2>" +
                    "<p style='color:#8d93a5;margin:0 0 10px'>Is the laptop awake with ARYA running? Is Tailscale on on both " +
                    "devices (or are you on the home Wi-Fi)?</p><p style='color:#8d93a5;margin:0'>Reminders still arrive as " +
                    "soon as the connection is back. The 🎙 Talk button still works for phone tasks.</p></body>",
                    "text/html", "utf-8")
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
