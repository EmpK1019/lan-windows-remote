# LAN Remote 1.0.3

- Windows session lock/unlock events are now recorded by the system service
  without restarting the secure-desktop helpers, avoiding the long black-screen
  gap that could occur when entering or leaving the lock screen.
- Native video startup keeps a current-screen preview visible until a decoded
  frame has actually rendered, and requests additional keyframes while the
  first frame is pending instead of reporting a black surface as ready.
- Native H.264 capture now enters the compatibility path for the complete
  locked-session transition and retries only after both the session and secure
  input desktop have returned to the normal desktop.
- Automatic unlock is now an observable state machine rather than a fixed
  Enter/password/Enter macro. It distinguishes LockApp, LogonUI, transition,
  unlocked and unknown states; retries an unresponsive wake action; skips the
  initial Enter when credentials are already visible; and never types a saved
  password while the UI state is unconfirmed.
- Unlock result polling now reports successful unlock, remaining credential UI,
  return to the lock screen, pending transition and unknown result separately,
  keeping the manual unlock action available when human intervention is needed.
- Regression coverage includes service session-state freshness, lock-screen
  input routing, first-render gating, adaptive unlock branches and native-video
  secure-desktop fallback/recovery.
