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
    if (fps >= 120) return {1920, 1080, 64'000'000};
    if (fps >= 60) return {2560, 1440, 40'000'000};
    return {2560, 1440, 20'000'000};
}

constexpr std::uint32_t DefaultBitrateForFps(const std::uint32_t fps) {
    return QualityProfileForFps(fps).bitrate;
}

// When the receiver cannot sustain the selected ceiling, lower total bitrate
// in direct proportion to FPS. This preserves encoded bits per delivered frame
// instead of making every frame progressively blurrier.
constexpr std::uint32_t AdaptiveBitrateForFps(
    const std::uint32_t configured_bitrate,
    const std::uint32_t configured_fps,
    const std::uint32_t requested_fps) {
    if (configured_fps == 0) return configured_bitrate;
    const std::uint32_t minimum_fps = (std::min)(kMinimumAdaptiveFps, configured_fps);
    const std::uint32_t target_fps = (std::clamp)(
        requested_fps, minimum_fps, configured_fps);
    return static_cast<std::uint32_t>(
        static_cast<std::uint64_t>(configured_bitrate) * target_fps /
        configured_fps);
}

}  // namespace lanremote::video
