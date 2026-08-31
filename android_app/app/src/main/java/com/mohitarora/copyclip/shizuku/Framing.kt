package com.mohitarora.copyclip.shizuku

import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Exact Kotlin mirror of transports/framing.py + termux/copyclip_node.py's own duplicate of
 * it. Byte-for-byte compatible with the laptop's TcpTransport (transports/tcp_transport.py):
 * same handshake (HELLO/AUTH/AUTH-OK/CHALLENGE/RESPONSE), same frame format
 * "<seq> <hmac-sha256-hex> <payload>", same strictly-increasing sequence check. This must
 * not drift from the Python side -- there is exactly one wire format, not two.
 */
object Framing {
    class AuthError(message: String) : RuntimeException(message)

    private fun hmacHex(key: ByteArray, message: ByteArray): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(message).joinToString("") { "%02x".format(it) }
    }

    fun newNonce(): String {
        val bytes = ByteArray(16)
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }

    private fun mac(key: ByteArray, seq: Long, payload: String): String =
        hmacHex(key, "$seq:$payload".toByteArray(Charsets.UTF_8))

    fun seal(key: ByteArray, seq: Long, payload: String): String =
        "$seq ${mac(key, seq, payload)} $payload"

    /** Returns (seq, payload); throws AuthError on any failure. */
    fun openFrame(key: ByteArray, raw: String, lastSeq: Long): Pair<Long, String> {
        val parts = raw.split(" ", limit = 3).toMutableList()
        if (parts.size == 2) parts.add("")
        if (parts.size < 3) throw AuthError("malformed frame")
        val seq = parts[0].toLongOrNull() ?: throw AuthError("bad sequence number")
        val receivedMac = parts[1]
        val payload = parts[2]
        if (seq <= lastSeq) throw AuthError("replay or reorder: seq $seq <= $lastSeq")
        val expected = mac(key, seq, payload)
        if (!constantTimeEquals(receivedMac, expected)) throw AuthError("bad MAC")
        return seq to payload
    }

    fun challengeResponse(key: ByteArray, nonce: String): String =
        hmacHex(key, nonce.toByteArray(Charsets.UTF_8))

    fun verifyChallenge(key: ByteArray, nonce: String, response: String): Boolean =
        constantTimeEquals(response, challengeResponse(key, nonce))

    private fun constantTimeEquals(a: String, b: String): Boolean {
        if (a.length != b.length) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].code xor b[i].code)
        return diff == 0
    }
}
