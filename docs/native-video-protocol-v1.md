# LAN Remote Native Video Protocol v1

This document defines the binary protocol used by the Windows-native H.264
desktop stream. All multi-byte integers use network byte order (big endian).
The protocol is independent from the existing native input and cursor streams.

## Handshake

The controller opens a LAN-only authenticated HTTP tunnel:

```http
CONNECT /video-stream HTTP/1.1
Host: <remote-host>:<port>
X-Remote-Token: <session-token>
X-LAN-Video-Protocol: 1
```

The monitor and requested ceiling are URL query parameters:
`/video-stream?monitor=<monitor-id>&fps=30|60|120`.

On success the controlled endpoint returns status `200`, keeps the connection
open, and includes `X-LAN-Video-Protocol: 1`. A missing encoder returns `503`,
an unsupported protocol returns `426`, and authentication/LAN-boundary failures
keep their existing HTTP status.
The controller must fall back to `mjpeg_v1` when native initialization fails.

## Fixed header

Every post-handshake message starts with a 40-byte header:

| Offset | Size | Field | Description |
|---:|---:|---|---|
| 0 | 4 | magic | ASCII `LRV1` |
| 4 | 1 | version | `1` |
| 5 | 1 | type | Message type |
| 6 | 2 | flags | Bit field |
| 8 | 4 | generation | Stream generation; starts at 1 |
| 12 | 8 | sequence | Monotonic message/frame sequence |
| 20 | 8 | timestamp_us | Monotonic capture timestamp in microseconds; sender-local clock domain |
| 28 | 4 | payload_length | Bytes following the header |
| 32 | 2 | width | Original remote coordinate width |
| 34 | 2 | height | Original remote coordinate height |
| 36 | 2 | fps_limit | Requested ceiling: 30, 60 or 120 |
| 38 | 2 | reserved | Must be zero |

Dimensions describe the remote coordinate space. Encoded dimensions are
carried by `STREAM_CONFIG`, because 120 FPS may use an adaptively reduced
encoded resolution while input remains mapped to the original desktop.

## Message types

| Value | Name | Direction | Payload |
|---:|---|---|---|
| 1 | `STREAM_CONFIG` | controlled → controller | UTF-8 JSON, at most 64 KiB |
| 2 | `VIDEO_ACCESS_UNIT` | controlled → controller | Annex-B H.264 access unit, at most 16 MiB |
| 3 | `REQUEST_KEYFRAME` | controller → controlled | Optional UTF-8 JSON reason |
| 4 | `RECONFIGURE` | controller → controlled | UTF-8 JSON monitor/FPS request |
| 5 | `RECEIVER_REPORT` | controller → controlled | UTF-8 JSON metrics |
| 6 | `STREAM_END` | either direction | Optional UTF-8 JSON reason |
| 7 | `ERROR` | either direction | UTF-8 JSON error object |
| 8 | `SENDER_REPORT` | controlled → controller | UTF-8 JSON capture/encode/send metrics |

`STREAM_CONFIG` JSON contains at least:

```json
{
  "codec": "h264_annexb",
  "encoded_width": 1920,
  "encoded_height": 1080,
  "coordinate_width": 1920,
  "coordinate_height": 1080,
  "fps_limit": 60,
  "bitrate": 12000000,
  "encoder": "Intel Quick Sync Video H.264 Encoder MFT",
  "hardware": true
}
```

## Flags

- `0x0001 KEYFRAME`: access unit is independently decodable.
- `0x0002 CODEC_CONFIG`: access unit carries SPS/PPS configuration.

Unknown flags must be ignored only when their meaning does not change framing.
Unknown message types, non-zero reserved fields, invalid dimensions, invalid FPS
ceilings, oversized payloads, and length mismatches terminate the native stream.

## Generations and recovery

- A connection begins at generation 1.
- Monitor, FPS, encoded resolution, codec, or decoder configuration changes
  increment the generation.
- A new generation begins with `STREAM_CONFIG`, then an access unit marked both
  `KEYFRAME` and `CODEC_CONFIG`.
- Receivers discard messages from older generations.
- A dropped reference chain triggers `REQUEST_KEYFRAME`; stale frames are not
  queued in an attempt to provide file-like complete playback.

## Backpressure

Desktop control prioritizes the newest decodable frame. The controlled endpoint
keeps at most two pending access units. When the sender or receiver falls behind,
old non-keyframes are dropped. Input and cursor data use separate connections so
video backpressure can never delay mouse or keyboard delivery.

`RECEIVER_REPORT` carries decoder throughput and controller display capacity.
The sender treats 30/60/120 as ceilings, applies hysteresis before lowering its
capture cadence, and adjusts `CODECAPI_AVEncCommonMeanBitRate` when sustained
receiver or send-queue pressure is detected.

## Security

- The endpoint retains the existing private/loopback LAN allowlist.
- The session token is sent only in the CONNECT header and is never logged.
- Reauthentication occurs periodically for long-running streams.
- Parser limits apply before payload allocation.
- Protocol errors do not include tokens, passwords, or raw request headers.
