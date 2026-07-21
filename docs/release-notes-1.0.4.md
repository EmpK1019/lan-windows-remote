# LAN Remote 1.0.4

- Locking a controlled Windows session now starts an explicit capture
  transition immediately, preventing the native DXGI stream from exposing a
  black surface before Windows publishes the session-lock event.
- The secure-desktop handoff keeps the last valid native D3D frame visible
  until the compatibility stream has delivered its first usable lock-screen
  frame, so temporary black captures are not shown to the controller.
- Locked sessions use a three-stage capture path: native DXGI/H.264 for the
  normal desktop, GDI for LockApp while it is still on the Default desktop, and
  the system helper for the Winlogon credential desktop.
- The compatibility stream rejects initial all-black transition frames for a
  bounded interval and only replaces the retained native image after an actual
  decoded image-load event.
- Unlock status detection now asks the secure-desktop helper when the main
  application cannot identify the input desktop. Winlogon and LogonUI can
  therefore be recognized as a ready credential screen instead of returning
  `state_unavailable` before the saved password is sent.
- Regression coverage now verifies the immediate lock marker, black-frame
  suppression, retained native frame, LockApp-to-Winlogon capture handoff, and
  helper-assisted credential-state classification.
