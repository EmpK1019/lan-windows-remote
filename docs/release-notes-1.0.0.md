# LAN Remote 1.0.0

- Ordinary Windows desktop control now defaults to native H.264 using DXGI,
  D3D11 and Media Foundation instead of browser MJPEG rendering.
- 30, 60 and 120 remain maximum FPS ceilings. Diagnostics show actual capture,
  encode, send, decode and render rates, stage timings, bandwidth, latency and
  drops. Receiver capacity and sustained backpressure automatically lower the
  real cadence/bitrate without changing the selected ceiling.
- Hardware encoding is preferred; 120 FPS mode can select the faster built-in
  Windows software MFT when a hardware benchmark cannot approach the ceiling.
- Native decoding, D3D11 rendering and a latest-frame queue prevent old frames
  from accumulating into visible control delay; the local release gate measures
  capture-to-present P50/P95 instead of relying on visual estimates.
- Native cursor ownership preserves immediate controller motion and switches to
  the controlled computer's live cursor when its local user moves the mouse.
- MJPEG remains available for codec/driver failures, old peers and secure
  desktop, with an explicit compatibility-mode status and automatic recovery.
- Existing native mouse/keyboard input, clipboard defaults, one-time lock-screen
  auto-unlock, files, credentials, device occupancy and self-control protection
  remain unchanged.
