# Native video architecture decision

## Decision

LAN Remote 1.0 uses a Windows-native video path for ordinary desktop control:

```text
DXGI Desktop Duplication
  -> D3D11 BGRA composition
  -> D3D11 VideoProcessor NV12 conversion/scaling
  -> Media Foundation H.264 MFT
  -> authenticated versioned binary TCP tunnel
  -> Media Foundation H.264 decoder with DXVA/D3D11 manager
  -> bounded latest-frame render queue
  -> D3D11 VideoProcessor and flip-model swap chain
```

WebView2 continues to host the product UI and toolbars. The video rectangle is
a native child surface owned by `ControlWindowHost`; it is moved and hidden by
the WebView bridge. Mouse, keyboard, cursor, clipboard and file transfer retain
their independent connections and existing authorization boundaries.

## Why this boundary

- Media Foundation, DXGI and D3D11 ship with Windows and add no codec royalty or
  third-party runtime dependency to this build.
- Encoded video never returns to a browser image element on the normal path.
- The source coordinate space remains separate from the adaptively encoded
  size, so multi-monitor and high-DPI input mapping do not change.
- A bounded capture slot, two-access-unit sender queue and one-frame renderer
  slot favor the newest desktop state instead of accumulating playback delay.
- Receiver reports advertise decode throughput and display capacity. Wide
  hysteresis lowers the capture ceiling only under sustained pressure, while
  real-time `ICodecAPI` bitrate changes react to receiver/send-queue pressure.
- A frame-latency waitable flip-model swap chain prevents `Present` from
  blocking the decoder/network reader behind the controller display refresh.
- `native_h264_v1` is negotiated independently from `mjpeg_v1`, allowing old,
  unsupported and secure-desktop sessions to use the verified compatibility
  path without changing the rest of the application.

## Encoder selection

Hardware H.264 MFT is preferred. In 120 FPS mode the selected hardware encoder
is measured before the stream starts. If it cannot approach the requested
ceiling and the Windows software MFT is materially faster, the software MFT is
selected while capture and color conversion remain on D3D11. The selected MFT,
selection reason and benchmark rates are exposed in native diagnostics.

## Failure behavior

Initialization, codec, protocol, session and secure-desktop failures are
reported to the controller. The UI labels and starts the MJPEG compatibility
path; it retries a fresh H.264 generation after the ordinary desktop returns.
No failure silently claims to remain in native mode.
