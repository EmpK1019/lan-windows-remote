# Native video performance report

Measured 2026-07-21 on Windows 11, Intel Core i5-12500H, Intel Iris Xe
(31.0.101.4502), with a 90 Hz physical display and a 100 Hz virtual display.
Tests used the real encoder executable, authenticated loopback TCP tunnel,
decoder DLL and an actual D3D11 child surface. A GPU-generated changing desktop
region was used because the automated desktop session does not expose visible
window changes through Desktop Duplication.

| Mode | Encoded size for 4800x1800 virtual desktop | Capture/encode/send | Decoded/rendered | Present latency P50/P95 | Codec path |
|---|---:|---:|---:|---:|---|
| 30 ceiling | 1920x720 | ~30/30/30 FPS | ~30/30 FPS | 47/54 ms | Intel QSV + D3D11 |
| 60 ceiling | 1920x720 | ~60/60/60 FPS | ~60/~60 FPS | 29/38 ms | Intel QSV + D3D11 |
| 120 ceiling | 1280x480 | ~90/~90/~90 FPS | ~90/~87 FPS | 35/47 ms | adaptive software MFT + D3D11 |

The 120 result is device-limited on this machine: the receiver report caps the
sender at the controller's 90 Hz display capacity, and the flip-model renderer
keeps the latest decoded texture rather than allowing TCP or swap-chain queues
to accumulate. A 120 FPS selection remains a ceiling, not a promise of 120
physical presents. The release gate scales both encode and present expectations
to the active display refresh rate.

Representative 60 FPS stage timings were approximately 0.8-1.3 ms capture,
0.7-1.1 ms D3D11 color conversion and 10-11 ms encoder processing. Native H.264
used about 17.5 Mbit/s for the deliberately changing test pattern. The retained
MJPEG baseline produced median 81.2 KiB frames at 60.3 FPS (about 39 Mbit/s), so
the native test reduced bandwidth by roughly 55%; ordinary desktop content is
typically more compressible than the test pattern.

GPU utilization percentage is not reported because Windows GPU Engine counters
from the loopback test combine encoder, decoder, compositor and unrelated
virtual-display processes. Diagnostics instead prove the active GPU boundaries
(`dxgi_d3d11_gpu_texture`, `d3d11_video_processor`, D3D-aware decode) and expose
per-stage time, queue depth, drops, bitrate and actual FPS without inventing an
unreliable utilization number. Capture-to-present latency is calculated from
preserved monotonic timestamps in the same-machine loopback test; cross-machine
clock domains are rejected rather than reported as misleading measurements.

Run the reproducible gates with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-native-video.ps1 -RunTests
.venv-build\Scripts\python tests\native_video_pipeline_e2e.py --fps 30 --measure-seconds 3 --enforce-performance
.venv-build\Scripts\python tests\native_video_pipeline_e2e.py --fps 60 --measure-seconds 3 --enforce-performance
.venv-build\Scripts\python tests\native_video_pipeline_e2e.py --fps 120 --measure-seconds 3 --enforce-performance
```
