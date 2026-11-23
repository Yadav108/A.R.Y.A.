package com.arya.phone

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Shader
import android.graphics.SweepGradient
import android.view.View
import kotlin.math.PI
import kotlin.math.min
import kotlin.math.sin

/** ARYA's living orb on the talk screen: breathes when idle, ripples with your voice while listening ([level] from
 *  the recorder), swirls while thinking, pulses while speaking. Colours glide from one state to the next. */
class OrbView(context: Context) : View(context) {
    enum class Mode { IDLE, LISTENING, THINKING, SPEAKING }

    var mode = Mode.IDLE
        set(v) { field = v; target = palette(v) }
    /** 0..1, how loud the microphone is right now (read every frame while listening). */
    var level: () -> Float = { 0f }

    private val density = resources.displayMetrics.density
    private val body = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glow = Paint(Paint.ANTI_ALIAS_FLAG)
    private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 1.6f * density }
    private val swirl = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 3f * density; strokeCap = Paint.Cap.ROUND
    }
    private var colors = palette(Mode.IDLE)
    private var target = colors
    private var smooth = 0f
    private val start = System.nanoTime()

    private fun palette(m: Mode) = when (m) {
        Mode.IDLE -> intArrayOf(0xFF7C8CFF.toInt(), 0xFFB07CFF.toInt())
        Mode.LISTENING -> intArrayOf(0xFF4FD1C5.toInt(), 0xFF5B9DFF.toInt())
        Mode.THINKING -> intArrayOf(0xFFB07CFF.toInt(), 0xFFFF6BB5.toInt())
        Mode.SPEAKING -> intArrayOf(0xFF7C8CFF.toInt(), 0xFF3EE0A0.toInt())
    }

    private fun mix(a: Int, b: Int, t: Float): Int = Color.argb(
        255,
        (Color.red(a) + (Color.red(b) - Color.red(a)) * t).toInt(),
        (Color.green(a) + (Color.green(b) - Color.green(a)) * t).toInt(),
        (Color.blue(a) + (Color.blue(b) - Color.blue(a)) * t).toInt(),
    )

    private fun alpha(c: Int, a: Float) = Color.argb((a.coerceIn(0f, 1f) * 255).toInt(), Color.red(c), Color.green(c), Color.blue(c))

    override fun onDraw(canvas: Canvas) {
        val t = (System.nanoTime() - start) / 1e9f
        colors = intArrayOf(mix(colors[0], target[0], 0.08f), mix(colors[1], target[1], 0.08f))
        val (c1, c2) = colors[0] to colors[1]
        val cx = width / 2f
        val cy = height / 2f
        val maxR = min(width, height) / 2f
        val base = maxR * 0.52f

        val live = if (mode == Mode.LISTENING) level() else 0f
        smooth += (live - smooth) * 0.25f
        val pulse = when (mode) {
            Mode.IDLE -> 0.03f * sin(t * 2 * PI.toFloat() / 4.5f)
            Mode.LISTENING -> 0.02f * sin(t * 4f) + smooth * 0.22f
            Mode.THINKING -> 0.025f * sin(t * 6f)
            Mode.SPEAKING -> 0.05f * sin(t * 9f) * (0.6f + 0.4f * sin(t * 2.3f))
        }
        val r = base * (1f + pulse)

        // Soft outer glow
        glow.shader = RadialGradient(cx, cy, maxR, intArrayOf(alpha(c1, 0.45f), alpha(c2, 0.12f), Color.TRANSPARENT),
            floatArrayOf(0f, 0.55f, 1f), Shader.TileMode.CLAMP)
        canvas.drawCircle(cx, cy, maxR, glow)

        // Ripples: listening (driven by your voice) and speaking
        if (mode == Mode.LISTENING || mode == Mode.SPEAKING) {
            for (i in 0 until 3) {
                val p = ((t * (if (mode == Mode.SPEAKING) 0.7f else 0.5f) + i / 3f) % 1f)
                val rr = r + (maxR - r) * p * (if (mode == Mode.LISTENING) 0.5f + smooth else 1f).coerceAtMost(1f)
                ring.color = alpha(c1, (1f - p) * 0.6f)
                canvas.drawCircle(cx, cy, rr, ring)
            }
        }

        // The sphere
        body.shader = RadialGradient(cx - r * 0.35f, cy - r * 0.4f, r * 1.6f,
            intArrayOf(Color.WHITE, c1, c2, 0xFF140F33.toInt()), floatArrayOf(0f, 0.22f, 0.65f, 1f), Shader.TileMode.CLAMP)
        canvas.drawCircle(cx, cy, r, body)

        // Thinking: a bright arc circling the sphere
        if (mode == Mode.THINKING) {
            canvas.save()
            canvas.rotate((t * 220f) % 360f, cx, cy)
            swirl.shader = SweepGradient(cx, cy, intArrayOf(Color.TRANSPARENT, alpha(Color.WHITE, 0.9f)), floatArrayOf(0.55f, 1f))
            canvas.drawCircle(cx, cy, r + 10 * density, swirl)
            canvas.restore()
        }

        postInvalidateOnAnimation()
    }
}
