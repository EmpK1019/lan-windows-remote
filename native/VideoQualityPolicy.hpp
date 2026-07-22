#pragma once

#include <algorithm>
#include <cstdint>

namespace lanremote::video {

constexpr std::uint32_t kMinimumAdaptiveFps = 24;
constexpr std::uint32_t kDefaultBitrate30 = 12'000'000;
constexpr std::uint32_t kDefaultBitrate60 = 24'000'000;
constexpr std::uint32_t kDefaultBitrate120 = 48'000'000;

constexpr std::uint32_t DefaultBitrateForFps(const std::uint32_t fps) {
    if (fps >= 120) return kDefaultBitrate120;
    if (fps >= 60) return kDefaultBitrate60;
    return kDefaultBitrate30;
}

// Preserve roughly the same number of encoded bits per delivered frame when
// congestion lowers the live frame rate. Desktop text and flat gradients look
// much worse when bitrate is reduced independently while the frame ceiling is
// left high.
constexpr std::uint32_t AdaptiveBitrateForFps(
    const std::uint32_t configured_bitrate,
    const std::uint32_t configured_fps,
    const std::uint32_t requested_fps) {
    if (configured_fps == 0) return configured_bitrate;
    const std::uint32_t target_fps = (std::clamp)(
        requested_fps, (std::min)(kMinimumAdaptiveFps, configured_fps), configured_fps);
    return static_cast<std::uint32_t>(
        static_cast<std::uint64_t>(configured_bitrate) * target_fps / configured_fps);
}

// Short Media Foundation benchmarks can overstate hardware throughput before
// the GPU/decoder pipeline reaches steady state. At the 120 FPS ceiling,
// prefer software when both candidates are in the same sub-95-FPS class and
// software is close enough to avoid the sustained hardware falloff observed
// under the full capture/transport/render load. Clearly faster hardware stays
// preferred to preserve CPU headroom.
constexpr bool PreferSoftwareEncoderFor120Fps(
    const double hardware_fps,
    const double software_fps) {
    if (software_fps < 60.0) return false;
    if (software_fps > hardware_fps * 1.05) return true;
    return hardware_fps < 95.0 &&
        software_fps >= 80.0 &&
        software_fps >= hardware_fps * 0.95;
}

}  // namespace lanremote::video
