# LAN Remote 1.1.0

- Restores the complete native D3D remote-session implementation from the
  proven 1.0.2 baseline.
- Removes the later WebView-overlay and rectangular video-surface cutout
  regressions introduced after 1.0.2.
- Keeps the native borderless glass toolbar above the uninterrupted D3D video
  surface in both windowed and full-screen sessions.
- Retains native Fit/Fill scaling, 30/60/120 FPS ceilings, clipboard, remote
  files, lock, keyboard, monitor and window controls from the stable baseline.
- Revalidated the native H.264 pipeline, mouse click and wheel input, packaged
  host behavior, and the Windows installer before release.
