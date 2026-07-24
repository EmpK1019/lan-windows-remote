#pragma once

#include <algorithm>
#include <cstdint>

namespace lanremote::video {

constexpr std::uint32_t kMinimumAdaptiveFps = 24;

struct VideoQualityProfile {
    std::uint32_t max_width;
    std::uint32_t max_height;
    std::uint32_t bitrate;
};

// LAN Remote prioritizes desktop text legibility over conserving LAN
// bandwidth. 30/60 FPS may retain a 2K source, while the 120 FPS ceiling
// remains at 1080p so supported hardware can still encode it in real time.
constexpr VideoQualityProfile QualityProfileForFps(const std::uint32_t fps) {
    if (fps >= 120) return {1920, 1080, 80'000'000};
    if (fps >= 60) return {2560, 1440, 80'000'000};
    return {2560, 1440, 56'000'000};
}

constexpr std::uint32_t DefaultBitrateForFps(const std::uint32_t fps) {
    return QualityProfileForFps(fps).bitrate;
}

// Higher values spend more encoder time preserving desktop text and fine
// edges. The 120 FPS tier keeps a moderate setting so encode latency remains
// inside the frame budget, while 30/60 FPS use the available headroom.
constexpr std::uint32_t EncoderQualityVsSpeedForFps(const std::uint32_t fps) {
    if (fps >= 120) return 33;
    if (fps >= 60) return 85;
    return 92;
}

// The selected FPS is only a ceiling. A 60 Hz receiver can legitimately ask a
// 120 FPS session to deliver fewer frames, but that is not network congestion
// and must not silently reduce the selected quality tier's bitrate.
// Keep the configured quality budget while FPS adapts; the sender queue still
// drops stale frames, so latency stays bounded on a slower LAN.
constexpr std::uint32_t AdaptiveBitrateForFps(
    const std::uint32_t configured_bitrate,
    const std::uint32_t configured_fps,
    const std::uint32_t requested_fps) {
    static_cast<void>(configured_fps);
    static_cast<void>(requested_fps);
    return configured_bitrate;
}

}  // namespace lanremote::video
