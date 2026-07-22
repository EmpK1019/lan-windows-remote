#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <iostream>
#include <string>

#include "../native/NativeVideoProtocol.hpp"
#include "../native/VideoQualityPolicy.hpp"

int wmain() {
    HANDLE reader = nullptr;
    HANDLE writer = nullptr;
    if (!CreatePipe(&reader, &writer, nullptr, 0)) return 2;
    lanremote::video::Message expected;
    expected.type = lanremote::video::MessageType::VideoAccessUnit;
    expected.flags = lanremote::video::Keyframe | lanremote::video::CodecConfig;
    expected.generation = 9;
    expected.sequence = 42;
    expected.timestamp_us = 1234567;
    expected.coordinate_width = 1920;
    expected.coordinate_height = 1080;
    expected.fps_limit = 120;
    expected.payload = {0, 0, 0, 1, 0x65, 1, 2, 3};
    if (!lanremote::video::WriteMessage(writer, expected)) return 3;
    CloseHandle(writer);
    lanremote::video::Message actual;
    std::string error;
    if (!lanremote::video::ReadMessage(reader, &actual, &error)) {
        std::cerr << error << std::endl;
        return 4;
    }
    CloseHandle(reader);
    if (actual.type != expected.type || actual.flags != expected.flags ||
        actual.generation != expected.generation || actual.sequence != expected.sequence ||
        actual.timestamp_us != expected.timestamp_us ||
        actual.coordinate_width != expected.coordinate_width ||
        actual.coordinate_height != expected.coordinate_height ||
        actual.fps_limit != expected.fps_limit || actual.payload != expected.payload) {
        return 5;
    }
    auto packed = lanremote::video::PackMessage(expected);
    packed[28] = 0x7f;
    HANDLE bad_reader = nullptr;
    HANDLE bad_writer = nullptr;
    if (!CreatePipe(&bad_reader, &bad_writer, nullptr, 0)) return 6;
    DWORD written = 0;
    if (!WriteFile(bad_writer, packed.data(), lanremote::video::kHeaderSize, &written, nullptr)) return 7;
    CloseHandle(bad_writer);
    if (lanremote::video::ReadMessage(bad_reader, &actual, &error) ||
        error.find("exceeds") == std::string::npos) {
        return 8;
    }
    CloseHandle(bad_reader);
    if (lanremote::video::DefaultBitrateForFps(30) != 12'000'000 ||
        lanremote::video::DefaultBitrateForFps(60) != 24'000'000 ||
        lanremote::video::DefaultBitrateForFps(120) != 48'000'000) {
        return 9;
    }
    if (lanremote::video::AdaptiveBitrateForFps(48'000'000, 120, 60) != 24'000'000 ||
        lanremote::video::AdaptiveBitrateForFps(24'000'000, 60, 30) != 12'000'000 ||
        lanremote::video::AdaptiveBitrateForFps(12'000'000, 30, 24) != 9'600'000 ||
        lanremote::video::AdaptiveBitrateForFps(24'000'000, 60, 120) != 24'000'000) {
        return 10;
    }
    if (!lanremote::video::PreferSoftwareEncoderFor120Fps(61.0, 85.0) ||
        !lanremote::video::PreferSoftwareEncoderFor120Fps(88.46, 89.57) ||
        lanremote::video::PreferSoftwareEncoderFor120Fps(100.0, 98.0) ||
        lanremote::video::PreferSoftwareEncoderFor120Fps(90.0, 79.0) ||
        lanremote::video::PreferSoftwareEncoderFor120Fps(55.0, 59.0)) {
        return 11;
    }
    std::cout << "native video protocol tests passed" << std::endl;
    return 0;
}
