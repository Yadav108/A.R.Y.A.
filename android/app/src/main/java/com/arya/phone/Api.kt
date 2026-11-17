package com.arya.phone

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** ARYA's phone server (arya/server/phone.py). Every request carries the pairing key as a Bearer token. */
object Api {
    // /api/wait holds the request up to 50 s until something happens: read timeout a bit longer.
    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(70, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private fun request(base: String, path: String, key: String) =
        Request.Builder().url(base.trimEnd('/') + path)
            .header("Authorization", "Bearer $key")
            .header("X-ARYA", "1")

    class Unpaired : Exception("the laptop no longer knows this phone (new phone link?)")

    private fun call(req: Request): JSONObject {
        client.newCall(req).execute().use { r ->
            if (r.code == 401) throw Unpaired()
            val body = r.body?.string() ?: "{}"
            if (!r.isSuccessful) throw java.io.IOException("HTTP ${r.code}: ${body.take(120)}")
            return JSONObject(body)
        }
    }

    fun hello(base: String, key: String): JSONObject = call(request(base, "/api/hello", key).get().build())

    fun waitFor(base: String, key: String, after: Long): JSONObject =
        call(request(base, "/api/wait?after=$after&timeout=50", key).get().build())

    fun action(base: String, key: String, id: Long, action: String, minutes: Int = 10): JSONObject {
        val body = JSONObject().put("id", id).put("action", action).put("minutes", minutes).toString()
        return call(request(base, "/api/action", key)
            .post(body.toRequestBody("application/json".toMediaType())).build())
    }
}
