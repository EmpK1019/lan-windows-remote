# LAN Remote 1.1.1

- Refines the native glass remote-session toolbar with free dragging, top-edge
  docking and collapse, compact 30/60/120 FPS controls, and menus that remain
  anchored while the toolbar moves.
- Makes the lock button reflect the remote state: an open lock starts locking,
  while a green closed lock starts unlocking. Lock requests are now confirmed
  against the active Windows session with a WTS fallback.
- Keeps the native D3D video surface adaptive in windowed and full-screen modes,
  restores foreground activation for new controller windows, and applies
  rounded Windows 11 corners outside maximized/full-screen states.
- Improves the main device view with wallpaper previews, clearer control/view
  status text, and a full-preview glass action for ending an active control
  session.
- Preserves the stable native H.264 input and rendering path while expanding
  regression coverage for the native toolbar, window startup, lock state,
  input transport, and installer packaging.
