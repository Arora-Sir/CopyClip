package com.mohitarora.copyclip;

// Hosted by Shizuku as a privileged (shell UID) process. Runs the exact, unmodified
// clipbridge.dex (bundled as an APK asset, same dex android/build_dex.ps1 produces) as a
// subprocess -- exactly what termux/copyclip_node.py already does via `rish`, just launched
// by Shizuku's Java API instead of a shell script -- and speaks the identical HMAC-framed TCP
// protocol the laptop's TcpTransport already understands (see shizuku/Framing.kt,
// shizuku/BridgeServer.kt). No clipboard-touching code was rewritten; only how ClipBridge is
// launched and reached changed. See bugs/SPIKE-ANDROID-USERSERVICE.md for the proof this
// mechanism (shell-UID appops grant surviving a UserService-hosted call) actually works.
interface IUserService {

    // Reserved transaction code Shizuku itself calls on unbind -- must self-terminate, per
    // Shizuku.java's own doc comment (a UserService process is not killed automatically).
    void destroy() = 16777114;

    // Spike-only diagnostic, kept for troubleshooting. Not the real sync path.
    String probe() = 1;

    // Extracts dexBytes to a shell-writable path, starts the HMAC-framed TCP server on
    // `port`, and spawns clipbridge.dex against it on the first client connection (mirrors
    // copyclip_node.py's one-client-at-a-time model). Returns true if the server socket
    // bound successfully. hmacKey must be the exact bytes of the laptop's copyclip.key.
    boolean startBridge(int port, in byte[] hmacKey, in byte[] dexBytes) = 2;

    void stopBridge() = 3;

    // Human-readable one-line status for the UI to poll: listening/client-connected/error.
    String getBridgeStatus() = 4;
}
