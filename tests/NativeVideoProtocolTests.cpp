#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <iostream>
#include <string>

#include "../native/NativeVideoProtocol.hpp"

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
    std::cout << "native video protocol tests passed" << std::endl;
    return 0;
}
