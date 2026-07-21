# LAN Remote 1.0.2

- The remote-session toolbar is now a native borderless glass overlay above the
  complete D3D video surface. The renderer no longer cuts holes for WebView
  controls, so the toolbar remains available without creating artificial black
  strips in windowed or full-screen sessions.
- The toolbar, its compact collapsed handle and the device-preview stop-control
  action use a lighter translucent frosted-glass treatment instead of opaque
  black or red blocks.
- Remote sessions now offer Fit and Fill display modes. Fit preserves the full
  desktop, while Fill crops the centered edges to remove aspect-ratio bars.
- Native rendering, the remote-cursor overlay and mouse coordinate conversion
  now share the selected scale mode, keeping clicks and the controlled cursor
  aligned even when Fill mode crops the source frame.
- The native toolbar exposes monitor, 30/60/120 FPS ceiling, display mode,
  keyboard, clipboard, remote files, lock, full-screen and window controls.
- High-DPI packaged-host coverage verifies that the owned native toolbar remains
  visible above the live D3D surface without reducing its render area.
