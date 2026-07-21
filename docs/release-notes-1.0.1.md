# LAN Remote 1.0.1

- The native video surface now leaves interactive regions for the remote
  toolbar, title bar, top reveal strip and unlock action. Remote controls remain
  visible and clickable in both windowed and full-screen sessions.
- A locked computer shows an explicit unlock action. An initially locked remote
  computer still receives one automatic unlock attempt, while a lock occurring
  later in the session waits for the controller to click the action.
- Active control sessions can be ended directly from the selected device's
  desktop preview. Session cancellation is relayed to the remote control window
  and releases the device occupancy state.
- The controlled computer can open Settings while it is occupied without losing
  the visible controlled-state treatment or starting control of another device.
- 120 FPS mode keeps the full 1920-pixel encoding width and raises its default
  bitrate to 24 Mbps, preventing the high-FPS ceiling from needlessly reducing
  desktop sharpness.
- Automated coverage now includes native video exclusion regions, unlock retry
  semantics and outgoing-session cancellation.
