# Native video known limits

- Secure desktop/UAC capture uses the existing authenticated MJPEG compatibility
  service. Returning to the user desktop starts a new native generation.
- Cross-adapter multi-monitor layouts and rotated displays can require the
  measured CPU-readback compatibility path. Same-adapter, unrotated displays use
  direct D3D11 texture composition.
- Windows N/KN systems without Media Foundation components fall back to MJPEG
  and show the initialization reason.
- Actual FPS remains bounded by source display updates, encoder/decoder capacity,
  network delivery, controller display refresh and compositor availability.
- The first release uses authenticated TCP with `TCP_NODELAY`. The versioned
  framing and receiver reports leave room for a future LAN datagram transport;
  WebRTC, FFmpeg, STUN/TURN and cloud relays are intentionally not included.
- The native cursor layer currently draws a Windows-style arrow at the remote
  cursor position. Position and ownership are real-time and independent from
  video, while cursor bitmap-shape replication is future work.
- Capture-to-present timestamps are directly comparable in the automated local
  loopback gate. Unrelated machines have different monotonic clock domains, so
  cross-machine end-to-end latency is left unavailable until clock-offset
  estimation is added; per-stage FPS, queue and drop metrics remain available.
