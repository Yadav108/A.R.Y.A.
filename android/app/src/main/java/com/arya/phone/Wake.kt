package com.arya.phone

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/** Wake-on-LAN: wakes a sleeping laptop with a "magic packet" (6 x 0xFF, then its MAC 16 times) sent as a UDP
 *  broadcast. Works only on the same Wi-Fi as the laptop (Tailscale can't carry broadcasts) and only when the
 *  laptop's network card is allowed to wake it. The laptop tells the app its MAC and broadcast address. */
object Wake {
    /** "80:38:FB:8A:6C:D6" (or with dashes) -> the 102-byte packet, null for anything else. */
    fun magicPacket(mac: String): ByteArray? {
        val parts = mac.trim().split(':', '-')
        if (parts.size != 6) return null
        val bytes = parts.map { it.toIntOrNull(16)?.takeIf { b -> it.length == 2 && b in 0..255 } ?: return null }
        val packet = ByteArray(6 + 16 * 6) { 0xFF.toByte() }
        for (i in 0 until 16) for (j in 0 until 6) packet[6 + i * 6 + j] = bytes[j].toByte()
        return packet
    }

    /** Send it (a few times, to the subnet broadcast and to 255.255.255.255, ports 9 and 7). Network: call off the
     *  UI thread. True when at least one send went out. */
    fun send(mac: String, broadcast: String): Boolean {
        val packet = magicPacket(mac) ?: return false
        var sent = false
        DatagramSocket().use { socket ->
            socket.broadcast = true
            val targets = listOf(broadcast, "255.255.255.255").filter { it.isNotBlank() }.distinct()
            repeat(3) {
                for (host in targets) for (port in intArrayOf(9, 7)) {
                    try {
                        socket.send(DatagramPacket(packet, packet.size, InetAddress.getByName(host), port))
                        sent = true
                    } catch (e: Exception) { /* e.g. not on Wi-Fi */ }
                }
            }
        }
        return sent
    }
}
