package com.arya.phone

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
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
                prefs.rememberWake(hello)
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

    // The page lives under a native cover (orb + "Connecting…"). The cover goes away only once the page has really
    // loaded; when the laptop can't be reached it turns into an offline home screen with Talk / Voice notes / Try
    // again — never a black WebView with a lone Talk button (what an asleep laptop on Tailscale used to give: the
    // load just hung, and no error ever came).
    private val ui = Handler(Looper.getMainLooper())
    private var cover: LinearLayout? = null
    private var coverTitle: TextView? = null
    private var coverText: TextView? = null
    private var coverButtons: LinearLayout? = null
    private var loadedOk = false
    private var offline = false
    private var connecting = false
    private var loadToken = 0
    private var firstLoad = false
    private val autoRetry = Runnable { if (offline && !isFinishing) connect() }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWeb(firstTime: Boolean) {
        firstLoad = firstTime
        loadedOk = false
        val w = WebView(this).apply {
            layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
            setBackgroundColor(Theme.BG)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            webViewClient = object : WebViewClient() {
                override fun onReceivedError(view: WebView, request: android.webkit.WebResourceRequest,
                                             error: android.webkit.WebResourceError) {
                    if (request.isForMainFrame) tryNextAddress()
                }

                // A laptop that answers with a server error (ARYA starting up, crashed handler) is not "loaded".
                override fun onReceivedHttpError(view: WebView, request: android.webkit.WebResourceRequest,
                                                 response: android.webkit.WebResourceResponse) {
                    if (request.isForMainFrame && response.statusCode >= 500) tryNextAddress()
                }

                override fun onPageFinished(view: WebView, url: String) {
                    if (offline || url.startsWith("about:") || url.startsWith("data:")) return
                    pageLoaded()
                }

                // The WebView's renderer can be killed when the phone is short of memory: without this the app
                // either crashes or is left with a dead, black view. Build a fresh one instead.
                override fun onRenderProcessGone(view: WebView, detail: android.webkit.RenderProcessGoneDetail): Boolean {
                    if (view === web) { web = null; view.destroy(); ui.post { showWeb(firstTime = false) } }
                    return true
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
            visibility = View.INVISIBLE
        }
        web = w
        Theme.edgeToEdge(window)
        // 🎙 Talk: the voice screen, floating over the page's top-right corner — until the page says it has its own
        // mic button (the new page calls AryaApp.pageHasTalk(); an older laptop page doesn't, so this stays).
        val d = resources.displayMetrics.density
        val talk = Theme.pill(this, "🎙 Talk", primary = true) {
            startActivity(Intent(this@MainActivity, TalkActivity::class.java))
        }.apply {
            setPadding((16 * d).toInt(), (10 * d).toInt(), (16 * d).toInt(), (10 * d).toInt())
            visibility = View.GONE                                   // the cover has its own Talk button
        }
        talkButton = talk
        // The page may only open the talk or voice-notes screen, or hide this button: nothing else crosses over.
        w.addJavascriptInterface(object {
            @android.webkit.JavascriptInterface
            fun talk() { runOnUiThread { startActivity(Intent(this@MainActivity, TalkActivity::class.java)) } }
            @android.webkit.JavascriptInterface
            fun voiceNotes(record: Boolean) { runOnUiThread { VoiceNotesActivity.open(this@MainActivity, record) } }
            @android.webkit.JavascriptInterface
            fun pageHasTalk() { runOnUiThread { pageHasTalk = true; talkButton?.visibility = View.GONE } }
        }, "AryaApp")
        val frame = android.widget.FrameLayout(this).apply { setBackgroundColor(Theme.BG) }
        frame.addView(w)
        frame.addView(buildCover(), android.widget.FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        frame.addView(talk, android.widget.FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP or Gravity.END
        ).apply { setMargins(0, (10 * d).toInt(), (10 * d).toInt(), 0) })
        setContentView(frame)
        shareLogin()
        connect()
    }

    private var pageHasTalk = false

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

    private fun buildCover(): View {
        val d = resources.displayMetrics.density
        val pad = (28 * d).toInt()
        fun text(size: Float, color: Int) = TextView(this).apply {
            textSize = size; setTextColor(color); gravity = Gravity.CENTER; setLineSpacing(0f, 1.2f)
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(pad, pad, pad, pad)
            background = Theme.backdrop()
            fitsSystemWindows = true
            isClickable = true                                       // taps don't fall through to the hidden page
        }
        box.addView(OrbView(this), LinearLayout.LayoutParams((130 * d).toInt(), (130 * d).toInt()))
        box.addView(View(this), LinearLayout.LayoutParams(1, (18 * d).toInt()))
        coverTitle = text(22f, Theme.TEXT).also { box.addView(it) }
        box.addView(View(this), LinearLayout.LayoutParams(1, (8 * d).toInt()))
        coverText = text(15f, Theme.MUTED).also { box.addView(it) }
        box.addView(View(this), LinearLayout.LayoutParams(1, (24 * d).toInt()))
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; visibility = View.GONE }
        val wide = { LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            .apply { bottomMargin = (12 * d).toInt() } }
        buttons.addView(Theme.pill(this, "🎙 Talk to ARYA", primary = true) {
            startActivity(Intent(this, TalkActivity::class.java))
        }, wide())
        buttons.addView(Theme.pill(this, "🗒 Voice notes", primary = false) {
            VoiceNotesActivity.open(this, false)
        }, wide())
        wakeButton = Theme.pill(this, "⏻ Wake the laptop", primary = false) { wakeLaptop() }
        buttons.addView(wakeButton, wide())
        buttons.addView(Theme.pill(this, "↻ Try again", primary = false) { connect() }, wide())
        coverButtons = buttons
        box.addView(buttons, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        cover = box
        showConnecting()
        return box
    }

    private fun showConnecting() {
        coverTitle?.text = "Connecting to ARYA…"
        coverText?.text = "Looking for your laptop."
        coverButtons?.visibility = View.GONE
        cover?.visibility = View.VISIBLE
    }

    /** Ask each laptop address with short timeouts (about a second each), then load the first one that answers. */
    private fun connect() {
        if (connecting) return
        val w = web ?: return
        connecting = true
        offline = false
        loadedOk = false
        ui.removeCallbacks(autoRetry)
        showConnecting()
        val urls = prefs.urls
        val key = prefs.key
        thread {
            val index = urls.indexOfFirst { Api.reachable(it, key) }
            runOnUiThread {
                connecting = false
                if (isFinishing || web !== w) return@runOnUiThread
                if (index < 0) showOffline() else load(index)
            }
        }
    }

    private fun load(index: Int) {
        val w = web ?: return
        tried = index
        val url = prefs.urls[index]
        val token = ++loadToken
        // The pairing link logs the page in (it trades the key for a cookie); afterwards the plain address.
        w.loadUrl(if (firstLoad && index == 0 && prefs.pairingUrl.isNotEmpty()) prefs.pairingUrl else url)
        firstLoad = false
        // Watchdog: a laptop that accepts the connection but never answers must not leave the screen hanging.
        ui.postDelayed({ if (token == loadToken && !loadedOk && !offline) tryNextAddress() }, LOAD_TIMEOUT_MS)
    }

    private fun pageLoaded() {
        if (loadedOk) return
        loadedOk = true
        loadToken++
        web?.visibility = View.VISIBLE
        cover?.animate()?.alpha(0f)?.setDuration(220)?.withEndAction {
            cover?.visibility = View.GONE; cover?.alpha = 1f
        }?.start()
        if (!pageHasTalk) talkButton?.visibility = View.VISIBLE      // an older laptop page without its own mic
        val url = prefs.urls.getOrNull(tried) ?: return
        val key = prefs.key
        thread {                                                    // keep the Wake-on-LAN address up to date
            try { prefs.rememberWake(Api.hello(url, key)) } catch (e: Exception) { /* next time */ }
        }
    }

    private var wakeButton: View? = null

    /** Magic packet to the laptop (home Wi-Fi only), then look for it again once it has had time to wake. */
    private fun wakeLaptop() {
        val mac = prefs.wakeMac
        val broadcast = prefs.wakeBroadcast
        coverText?.text = "Waking the laptop…"
        thread {
            val sent = try { Wake.send(mac, broadcast) } catch (e: Exception) { false }
            runOnUiThread {
                if (!sent) {
                    coverText?.text = "Couldn't send the wake signal. It only works on the same Wi-Fi as the laptop."
                    return@runOnUiThread
                }
                coverText?.text = "Wake signal sent. If the laptop is asleep on this Wi-Fi, it wakes in a few seconds…"
                ui.removeCallbacks(autoRetry)
                ui.postDelayed({ if (offline && !isFinishing) connect() }, WAKE_WAIT_MS)
            }
        }
    }

    private var tried = 0
    private var talkButton: View? = null

    private fun tryNextAddress() {
        if (offline || connecting) return
        val urls = prefs.urls
        loadToken++
        if (tried + 1 < urls.size) { load(tried + 1); return }
        showOffline()
    }

    private fun showOffline() {
        offline = true
        loadedOk = false
        loadToken++
        web?.apply { stopLoading(); visibility = View.INVISIBLE }
        talkButton?.visibility = View.GONE
        coverTitle?.text = "Can't reach ARYA"
        coverText?.text = "Is the laptop awake with ARYA running, and Tailscale on on both devices (or are you on " +
                "the home Wi-Fi)?\n\nPhone tasks — alarms, timers, calls, messages, maps — still work by voice, and " +
                "voice notes are kept until the laptop is back. I'll keep trying."
        coverButtons?.visibility = View.VISIBLE
        wakeButton?.visibility = if (prefs.wakeMac.isNotEmpty()) View.VISIBLE else View.GONE
        cover?.alpha = 1f
        cover?.visibility = View.VISIBLE
        ui.removeCallbacks(autoRetry)
        ui.postDelayed(autoRetry, RETRY_MS)
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
        if (!prefs.paired && web != null) { showPairing("Not paired any more: scan the new QR code."); return }
        if (web != null && offline) connect()                    // back in the app: try the laptop again right away
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(autoRetry)                             // no retries while the app is in the background
    }

    override fun onDestroy() {
        ui.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    @Deprecated("Activity API")
    override fun onBackPressed() {
        val w = web
        if (w != null && w.canGoBack()) w.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    companion object {
        private const val FILE_REQUEST = 7
        private const val LOAD_TIMEOUT_MS = 15_000L
        private const val RETRY_MS = 15_000L
        private const val WAKE_WAIT_MS = 12_000L
    }
}
