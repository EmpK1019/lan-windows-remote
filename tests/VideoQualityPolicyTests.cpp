#include <cstdint>
#include <iostream>

#include "../native/VideoQualityPolicy.hpp"

int main() {
    using lanremote::video::AdaptiveBitrateForFps;
    using lanremote::video::QualityProfileForFps;

    constexpr auto fps30 = QualityProfileForFps(30);
    constexpr auto fps60 = QualityProfileForFps(60);
    constexpr auto fps120 = QualityProfileForFps(120);

    static_assert(fps30.max_width == 2560 && fps30.max_height == 1440);
    static_assert(fps60.max_width == 2560 && fps60.max_height == 1440);
    static_assert(fps120.max_width == 1920 && fps120.max_height == 1080);
    static_assert(fps30.bitrate == 20'000'000);
    static_assert(fps60.bitrate == 40'000'000);
    static_assert(fps120.bitrate == 64'000'000);

    static_assert(AdaptiveBitrateForFps(fps120.bitrate, 120, 90) == 48'000'000);
    static_assert(AdaptiveBitrateForFps(fps120.bitrate, 120, 60) == 32'000'000);
    static_assert(AdaptiveBitrateForFps(fps60.bitrate, 60, 30) == 20'000'000);
    static_assert(AdaptiveBitrateForFps(fps30.bitrate, 30, 1) == 16'000'000);
    static_assert(AdaptiveBitrateForFps(fps30.bitrate, 30, 120) == fps30.bitrate);

    std::cout << "video quality policy tests passed" << std::endl;
    return 0;
}
