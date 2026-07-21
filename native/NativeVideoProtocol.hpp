#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace lanremote::video {

constexpr std::array<std::uint8_t, 4> kMagic = {'L', 'R', 'V', '1'};
constexpr std::uint8_t kVersion = 1;
constexpr std::size_t kHeaderSize = 40;
constexpr std::uint32_t kMaxConfigBytes = 64 * 1024;
constexpr std::uint32_t kMaxAccessUnitBytes = 16 * 1024 * 1024;

enum class MessageType : std::uint8_t {
    StreamConfig = 1,
    VideoAccessUnit = 2,
    RequestKeyframe = 3,
    Reconfigure = 4,
    ReceiverReport = 5,
    StreamEnd = 6,
    Error = 7,
    SenderReport = 8,
};

enum MessageFlags : std::uint16_t {
    Keyframe = 0x0001,
    CodecConfig = 0x0002,
};

struct Message {
    MessageType type = MessageType::Error;
    std::uint16_t flags = 0;
    std::uint32_t generation = 0;
    std::uint64_t sequence = 0;
    std::uint64_t timestamp_us = 0;
    std::uint16_t coordinate_width = 0;
    std::uint16_t coordinate_height = 0;
    std::uint16_t fps_limit = 0;
    std::vector<std::uint8_t> payload;
};

inline void Store16(std::uint8_t* target, const std::uint16_t value) {
    target[0] = static_cast<std::uint8_t>(value >> 8);
    target[1] = static_cast<std::uint8_t>(value);
}

inline void Store32(std::uint8_t* target, const std::uint32_t value) {
    target[0] = static_cast<std::uint8_t>(value >> 24);
    target[1] = static_cast<std::uint8_t>(value >> 16);
    target[2] = static_cast<std::uint8_t>(value >> 8);
    target[3] = static_cast<std::uint8_t>(value);
}

inline void Store64(std::uint8_t* target, const std::uint64_t value) {
    for (int index = 0; index < 8; ++index) {
        target[index] = static_cast<std::uint8_t>(value >> (56 - index * 8));
    }
}

inline std::uint16_t Load16(const std::uint8_t* source) {
    return static_cast<std::uint16_t>(
        (static_cast<std::uint16_t>(source[0]) << 8) |
        static_cast<std::uint16_t>(source[1]));
}

inline std::uint32_t Load32(const std::uint8_t* source) {
    return (static_cast<std::uint32_t>(source[0]) << 24) |
        (static_cast<std::uint32_t>(source[1]) << 16) |
        (static_cast<std::uint32_t>(source[2]) << 8) |
        static_cast<std::uint32_t>(source[3]);
}

inline std::uint64_t Load64(const std::uint8_t* source) {
    std::uint64_t value = 0;
    for (int index = 0; index < 8; ++index) {
        value = (value << 8) | source[index];
    }
    return value;
}

inline bool WriteAll(const HANDLE handle, const void* source, std::size_t size) {
    const auto* bytes = static_cast<const std::uint8_t*>(source);
    while (size > 0) {
        const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(size, 1024 * 1024));
        DWORD written = 0;
        if (!WriteFile(handle, bytes, chunk, &written, nullptr) || written == 0) {
            return false;
        }
        bytes += written;
        size -= written;
    }
    return true;
}

inline bool ReadAll(const HANDLE handle, void* target, std::size_t size) {
    auto* bytes = static_cast<std::uint8_t*>(target);
    while (size > 0) {
        const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(size, 1024 * 1024));
        DWORD received = 0;
        if (!ReadFile(handle, bytes, chunk, &received, nullptr) || received == 0) {
            return false;
        }
        bytes += received;
        size -= received;
    }
    return true;
}

inline std::uint32_t PayloadLimit(const MessageType type) {
    return type == MessageType::VideoAccessUnit ? kMaxAccessUnitBytes : kMaxConfigBytes;
}

inline bool IsKnownType(const std::uint8_t value) {
    return value >= static_cast<std::uint8_t>(MessageType::StreamConfig) &&
        value <= static_cast<std::uint8_t>(MessageType::SenderReport);
}

inline bool WriteMessage(const HANDLE handle, const Message& message) {
    const std::uint32_t payload_limit = PayloadLimit(message.type);
    if (message.payload.size() > payload_limit) {
        throw std::runtime_error("native video payload exceeds protocol limit");
    }
    std::array<std::uint8_t, kHeaderSize> header{};
    std::copy(kMagic.begin(), kMagic.end(), header.begin());
    header[4] = kVersion;
    header[5] = static_cast<std::uint8_t>(message.type);
    Store16(header.data() + 6, message.flags);
    Store32(header.data() + 8, message.generation);
    Store64(header.data() + 12, message.sequence);
    Store64(header.data() + 20, message.timestamp_us);
    Store32(header.data() + 28, static_cast<std::uint32_t>(message.payload.size()));
    Store16(header.data() + 32, message.coordinate_width);
    Store16(header.data() + 34, message.coordinate_height);
    Store16(header.data() + 36, message.fps_limit);
    Store16(header.data() + 38, 0);
    return WriteAll(handle, header.data(), header.size()) &&
        (message.payload.empty() || WriteAll(handle, message.payload.data(), message.payload.size()));
}

inline std::vector<std::uint8_t> PackMessage(const Message& message) {
    const std::uint32_t payload_limit = PayloadLimit(message.type);
    if (message.payload.size() > payload_limit) {
        throw std::runtime_error("native video payload exceeds protocol limit");
    }
    std::vector<std::uint8_t> packed(kHeaderSize + message.payload.size());
    std::copy(kMagic.begin(), kMagic.end(), packed.begin());
    packed[4] = kVersion;
    packed[5] = static_cast<std::uint8_t>(message.type);
    Store16(packed.data() + 6, message.flags);
    Store32(packed.data() + 8, message.generation);
    Store64(packed.data() + 12, message.sequence);
    Store64(packed.data() + 20, message.timestamp_us);
    Store32(packed.data() + 28, static_cast<std::uint32_t>(message.payload.size()));
    Store16(packed.data() + 32, message.coordinate_width);
    Store16(packed.data() + 34, message.coordinate_height);
    Store16(packed.data() + 36, message.fps_limit);
    Store16(packed.data() + 38, 0);
    std::copy(message.payload.begin(), message.payload.end(), packed.begin() + kHeaderSize);
    return packed;
}

inline bool ReadMessage(const HANDLE handle, Message* message, std::string* error = nullptr) {
    std::array<std::uint8_t, kHeaderSize> header{};
    if (!ReadAll(handle, header.data(), header.size())) {
        if (error) *error = "native video stream ended while reading a header";
        return false;
    }
    if (!std::equal(kMagic.begin(), kMagic.end(), header.begin()) || header[4] != kVersion) {
        if (error) *error = "invalid native video protocol magic or version";
        return false;
    }
    if (!IsKnownType(header[5]) || Load16(header.data() + 38) != 0) {
        if (error) *error = "invalid native video message type or reserved field";
        return false;
    }
    Message parsed;
    parsed.type = static_cast<MessageType>(header[5]);
    parsed.flags = Load16(header.data() + 6);
    parsed.generation = Load32(header.data() + 8);
    parsed.sequence = Load64(header.data() + 12);
    parsed.timestamp_us = Load64(header.data() + 20);
    const std::uint32_t payload_size = Load32(header.data() + 28);
    parsed.coordinate_width = Load16(header.data() + 32);
    parsed.coordinate_height = Load16(header.data() + 34);
    parsed.fps_limit = Load16(header.data() + 36);
    if (payload_size > PayloadLimit(parsed.type)) {
        if (error) *error = "native video payload exceeds protocol limit";
        return false;
    }
    parsed.payload.resize(payload_size);
    if (payload_size && !ReadAll(handle, parsed.payload.data(), parsed.payload.size())) {
        if (error) *error = "native video stream ended while reading a payload";
        return false;
    }
    *message = std::move(parsed);
    return true;
}

}  // namespace lanremote::video
