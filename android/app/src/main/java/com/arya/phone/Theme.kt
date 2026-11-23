package com.arya.phone

import android.content.Context
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Shader
import android.graphics.Typeface
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.view.Gravity
import android.view.Window
import android.widget.TextView

/** ARYA's look on the phone, shared by the native screens (talk, pairing) and matching the phone page: near-black
 *  with a soft aurora, glass surfaces, an indigo→violet accent. */
object Theme {
    val BG = Color.parseColor("#07080C")
    val TEXT = Color.parseColor("#EEF0F6")
    val MUTED = Color.parseColor("#8D93A5")
    val ACCENT = Color.parseColor("#7C8CFF")
    val ACCENT2 = Color.parseColor("#B07CFF")
    val WARN = Color.parseColor("#FFC46B")
    private val GLASS = Color.parseColor("#9E161924")
    private val LINE = Color.parseColor("#1FFFFFFF")

    /** System bars in ARYA's background colour, so the screen reads as one surface. */
    @Suppress("DEPRECATION")
    fun edgeToEdge(window: Window) {
        window.statusBarColor = BG
        window.navigationBarColor = BG
        window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(BG))
    }

    /** Near-black with two faint coloured glows (top-left indigo, bottom-right violet). */
    fun backdrop(): Drawable {
        val base = GradientDrawable().apply { setColor(BG) }
        fun glow(c: Int, cx: Float, cy: Float) = GradientDrawable().apply {
            gradientType = GradientDrawable.RADIAL_GRADIENT
            colors = intArrayOf(Color.argb(70, Color.red(c), Color.green(c), Color.blue(c)), Color.TRANSPARENT)
            setGradientCenter(cx, cy)
            gradientRadius = 900f
        }
        return LayerDrawable(arrayOf(base, glow(ACCENT, 0.1f, 0.05f), glow(ACCENT2, 0.95f, 0.95f)))
    }

    fun glass(radius: Float, fill: Int = GLASS) = GradientDrawable().apply {
        cornerRadius = radius; setColor(fill); setStroke(2, LINE)
    }

    fun accentFill(radius: Float) = GradientDrawable(GradientDrawable.Orientation.TL_BR, intArrayOf(ACCENT, ACCENT2)).apply {
        cornerRadius = radius
    }

    /** "ARYA" spaced out, with an indigo→white gradient. */
    fun wordmark(ctx: Context, size: Float = 18f) = TextView(ctx).apply {
        text = "ARYA"; textSize = size; letterSpacing = 0.35f; setTextColor(TEXT)
        typeface = Typeface.create("sans-serif", Typeface.BOLD)
        addOnLayoutChangeListener { v, _, _, _, _, _, _, _, _ ->
            val tv = v as TextView
            tv.paint.shader = LinearGradient(0f, 0f, tv.width.toFloat(), 0f, TEXT, Color.parseColor("#BFC7FF"), Shader.TileMode.CLAMP)
        }
    }

    fun iconButton(ctx: Context, glyph: String, label: String, onClick: () -> Unit) = TextView(ctx).apply {
        text = glyph; textSize = 16f; gravity = Gravity.CENTER; setTextColor(TEXT)
        contentDescription = label
        background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(GLASS); setStroke(2, LINE) }
        isClickable = true
        setOnClickListener { onClick() }
    }

    /** A chat bubble: yours in the accent gradient (right), ARYA's in glass (left). */
    fun bubble(ctx: Context, mine: Boolean): TextView {
        val d = ctx.resources.displayMetrics.density
        val r = 20 * d
        val small = 6 * d
        return TextView(ctx).apply {
            textSize = if (mine) 16f else 18f
            setLineSpacing(0f, 1.15f)
            setTextColor(if (mine) Color.WHITE else TEXT)
            setPadding((16 * d).toInt(), (11 * d).toInt(), (16 * d).toInt(), (12 * d).toInt())
            maxWidth = (ctx.resources.displayMetrics.widthPixels * 0.82f).toInt()
            background = (if (mine) accentFill(r) else glass(r)).apply {
                cornerRadii = if (mine) floatArrayOf(r, r, r, r, small, small, r, r)
                              else floatArrayOf(small, small, r, r, r, r, r, r)
            }
        }
    }

    /** A big pill button, accent-filled (primary) or glass. */
    fun pill(ctx: Context, label: String, primary: Boolean, onClick: () -> Unit) = TextView(ctx).apply {
        val d = ctx.resources.displayMetrics.density
        text = label; textSize = 16f; gravity = Gravity.CENTER
        setTextColor(if (primary) Color.WHITE else TEXT)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setPadding((20 * d).toInt(), (15 * d).toInt(), (20 * d).toInt(), (15 * d).toInt())
        background = if (primary) accentFill(18 * d) else glass(18 * d)
        if (primary) elevation = 6 * d
        isClickable = true
        setOnClickListener { onClick() }
    }

}
