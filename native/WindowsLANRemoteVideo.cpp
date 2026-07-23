#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <codecapi.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dxgi1_2.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mftransform.h>
#include <icodecapi.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "NativeVideoProtocol.hpp"

using Microsoft::WRL::ComPtr;
using lanremote::video::Message;
using lanremote::video::MessageType;

namespace {

constexpr wchar_t kWindowClass[] = L"LANRemoteNativeVideoSurfaceV1";
constexpr wchar_t kCursorWindowClass[] = L"LANRemoteNativeCursorV1";
std::mutex api_error_lock;
std::string api_error;

void SetApiError(const std::string& value) {
    std::lock_guard<std::mutex> guard(api_error_lock);
    api_error = value;
}

void ThrowIfFailed(const HRESULT result, const char* operation) {
    if (FAILED(result)) {
        std::ostringstream message;
        message << operation << " failed (0x" << std::hex << std::uppercase
                << static_cast<unsigned long>(result) << ')';
        throw std::runtime_error(message.str());
    }
}

std::string WideToUtf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        nullptr, 0, nullptr, nullptr);
    if (size <= 0) throw std::runtime_error("UTF-8 conversion failed");
    std::string result(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        result.data(), size, nullptr, nullptr);
    return result;
}

std::wstring Utf8ToWide(const std::string& value) {
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (size <= 0) return L"";
    std::wstring result(static_cast<std::size_t>(size), L'\0');
    MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        result.data(), size);
    return result;
}

std::string JsonEscape(const std::string& value) {
    std::ostringstream output;
    for (const unsigned char character : value) {
        switch (character) {
            case '\\': output << "\\\\"; break;
            case '"': output << "\\\""; break;
            case '\n': output << "\\n"; break;
            case '\r': output << "\\r"; break;
            case '\t': output << "\\t"; break;
            default:
                if (character < 0x20) {
                    output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                           << static_cast<int>(character) << std::dec;
                } else {
                    output << character;
                }
        }
    }
    return output.str();
}

std::string UrlEncode(const std::string& value) {
    static constexpr char digits[] = "0123456789ABCDEF";
    std::string output;
    for (const unsigned char character : value) {
        if ((character >= 'a' && character <= 'z') ||
            (character >= 'A' && character <= 'Z') ||
            (character >= '0' && character <= '9') ||
            character == '-' || character == '_' || character == '.' || character == '~') {
            output.push_back(static_cast<char>(character));
        } else {
            output.push_back('%');
            output.push_back(digits[character >> 4]);
            output.push_back(digits[character & 15]);
        }
    }
    return output;
}

std::uint64_t MonotonicMicroseconds() {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
}

UINT DisplayRefreshHertz(const HWND window) {
    MONITORINFOEXW monitor{};
    monitor.cbSize = sizeof(monitor);
    const HMONITOR handle = MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST);
    if (!handle || !GetMonitorInfoW(handle, &monitor)) return 0;
    DEVMODEW mode{};
    mode.dmSize = sizeof(mode);
    if (!EnumDisplaySettingsW(monitor.szDevice, ENUM_CURRENT_SETTINGS, &mode)) return 0;
    return mode.dmDisplayFrequency >= 24 && mode.dmDisplayFrequency <= 500
        ? mode.dmDisplayFrequency
        : 0;
}

bool SendAll(const SOCKET socket, const void* source, std::size_t size) {
    const auto* bytes = static_cast<const char*>(source);
    while (size) {
        const int chunk = static_cast<int>((std::min)(size, static_cast<std::size_t>(1024 * 1024)));
        const int sent = send(socket, bytes, chunk, 0);
        if (sent <= 0) return false;
        bytes += sent;
        size -= static_cast<std::size_t>(sent);
    }
    return true;
}

bool ReceiveAll(const SOCKET socket, void* target, std::size_t size) {
    auto* bytes = static_cast<char*>(target);
    while (size) {
        const int chunk = static_cast<int>((std::min)(size, static_cast<std::size_t>(1024 * 1024)));
        const int received = recv(socket, bytes, chunk, 0);
        if (received <= 0) return false;
        bytes += received;
        size -= static_cast<std::size_t>(received);
    }
    return true;
}

bool ReceiveMessage(const SOCKET socket, Message* message) {
    std::array<std::uint8_t, lanremote::video::kHeaderSize> header{};
    if (!ReceiveAll(socket, header.data(), header.size())) return false;
    if (!std::equal(lanremote::video::kMagic.begin(), lanremote::video::kMagic.end(), header.begin()) ||
        header[4] != lanremote::video::kVersion ||
        !lanremote::video::IsKnownType(header[5]) ||
        lanremote::video::Load16(header.data() + 38) != 0) {
        throw std::runtime_error("the native video server sent an invalid protocol header");
    }
    Message result;
    result.type = static_cast<MessageType>(header[5]);
    result.flags = lanremote::video::Load16(header.data() + 6);
    result.generation = lanremote::video::Load32(header.data() + 8);
    result.sequence = lanremote::video::Load64(header.data() + 12);
    result.timestamp_us = lanremote::video::Load64(header.data() + 20);
    const std::uint32_t payload_size = lanremote::video::Load32(header.data() + 28);
    result.coordinate_width = lanremote::video::Load16(header.data() + 32);
    result.coordinate_height = lanremote::video::Load16(header.data() + 34);
    result.fps_limit = lanremote::video::Load16(header.data() + 36);
    if (payload_size > lanremote::video::PayloadLimit(result.type)) {
        throw std::runtime_error("the native video server payload exceeds the protocol limit");
    }
    result.payload.resize(payload_size);
    if (payload_size && !ReceiveAll(socket, result.payload.data(), payload_size)) return false;
    *message = std::move(result);
    return true;
}

int JsonInteger(const std::string& json, const std::string& name, const int fallback = 0) {
    const std::string marker = "\"" + name + "\":";
    std::size_t offset = json.find(marker);
    if (offset == std::string::npos) return fallback;
    offset += marker.size();
    bool negative = false;
    if (offset < json.size() && json[offset] == '-') {
        negative = true;
        ++offset;
    }
    std::uint64_t result = 0;
    bool found = false;
    while (offset < json.size() && json[offset] >= '0' && json[offset] <= '9') {
        found = true;
        result = result * 10 + static_cast<unsigned>(json[offset++] - '0');
        if (result > static_cast<std::uint64_t>(INT_MAX)) return fallback;
    }
    if (!found) return fallback;
    return negative ? -static_cast<int>(result) : static_cast<int>(result);
}

double JsonNumber(const std::string& json, const std::string& name, const double fallback = 0.0) {
    const std::string marker = "\"" + name + "\":";
    std::size_t offset = json.find(marker);
    if (offset == std::string::npos) return fallback;
    offset += marker.size();
    char* end = nullptr;
    const double result = std::strtod(json.c_str() + offset, &end);
    return end == json.c_str() + offset ? fallback : result;
}

std::string JsonString(const std::string& json, const std::string& name) {
    const std::string marker = "\"" + name + "\":\"";
    std::size_t offset = json.find(marker);
    if (offset == std::string::npos) return {};
    offset += marker.size();
    std::string result;
    while (offset < json.size()) {
        const char character = json[offset++];
        if (character == '"') return result;
        if (character == '\\' && offset < json.size()) {
            const char escaped = json[offset++];
            if (escaped == 'n') result.push_back('\n');
            else if (escaped == 'r') result.push_back('\r');
            else if (escaped == 't') result.push_back('\t');
            else result.push_back(escaped);
        } else {
            result.push_back(character);
        }
    }
    return {};
}

struct StreamConfiguration {
    UINT encoded_width = 0;
    UINT encoded_height = 0;
    UINT coordinate_width = 0;
    UINT coordinate_height = 0;
    UINT fps_limit = 0;
    UINT bitrate = 0;
    std::string encoder;
    bool encoder_hardware = false;
    std::string capture;
    std::string color_conversion;
    std::string encoder_selection;
    double hardware_benchmark_fps = 0.0;
    double software_benchmark_fps = 0.0;
};

StreamConfiguration ParseConfiguration(const Message& message) {
    const std::string json(message.payload.begin(), message.payload.end());
    StreamConfiguration result;
    result.encoded_width = static_cast<UINT>(JsonInteger(json, "encoded_width"));
    result.encoded_height = static_cast<UINT>(JsonInteger(json, "encoded_height"));
    result.coordinate_width = static_cast<UINT>(JsonInteger(json, "coordinate_width", message.coordinate_width));
    result.coordinate_height = static_cast<UINT>(JsonInteger(json, "coordinate_height", message.coordinate_height));
    result.fps_limit = static_cast<UINT>(JsonInteger(json, "fps_limit", message.fps_limit));
    result.bitrate = static_cast<UINT>(JsonInteger(json, "bitrate"));
    result.encoder = JsonString(json, "encoder");
    result.encoder_hardware = json.find("\"hardware\":true") != std::string::npos;
    result.capture = JsonString(json, "capture");
    result.color_conversion = JsonString(json, "color_conversion");
    result.encoder_selection = JsonString(json, "encoder_selection");
    result.hardware_benchmark_fps = JsonNumber(json, "hardware_benchmark_fps");
    result.software_benchmark_fps = JsonNumber(json, "software_benchmark_fps");
    if (result.encoded_width < 2 || result.encoded_width > 8192 ||
        result.encoded_height < 2 || result.encoded_height > 8192 ||
        result.coordinate_width < 1 || result.coordinate_width > 65535 ||
        result.coordinate_height < 1 || result.coordinate_height > 65535 ||
        (result.fps_limit != 30 && result.fps_limit != 60 && result.fps_limit != 120)) {
        throw std::runtime_error("the native video stream configuration is invalid");
    }
    return result;
}

std::vector<std::uint8_t> ExtractH264ParameterSets(const std::vector<std::uint8_t>& access_unit) {
    struct Nal { std::size_t start_code; std::size_t payload; };
    std::vector<Nal> units;
    for (std::size_t index = 0; index + 3 < access_unit.size();) {
        if (access_unit[index] == 0 && access_unit[index + 1] == 0 && access_unit[index + 2] == 1) {
            units.push_back({index, index + 3});
            index += 3;
        } else if (index + 4 < access_unit.size() && access_unit[index] == 0 &&
            access_unit[index + 1] == 0 && access_unit[index + 2] == 0 && access_unit[index + 3] == 1) {
            units.push_back({index, index + 4});
            index += 4;
        } else {
            ++index;
        }
    }
    std::vector<std::uint8_t> result;
    for (std::size_t index = 0; index < units.size(); ++index) {
        const std::size_t end = index + 1 < units.size() ? units[index + 1].start_code : access_unit.size();
        if (units[index].payload >= end) continue;
        const std::uint8_t type = access_unit[units[index].payload] & 0x1f;
        if (type != 7 && type != 8) continue;
        result.insert(result.end(), {0, 0, 0, 1});
        result.insert(
            result.end(),
            access_unit.begin() + units[index].payload,
            access_unit.begin() + end);
    }
    return result;
}

struct LatencySnapshot {
    double p50_ms = 0.0;
    double p95_ms = 0.0;
    std::size_t samples = 0;
};

class PresentationLatencyTracker {
public:
    void Reset() {
        std::lock_guard<std::mutex> guard(lock_);
        samples_ms_.clear();
    }

    void Record(const std::uint64_t capture_timestamp_us) {
        const std::uint64_t now_us = MonotonicMicroseconds();
        if (capture_timestamp_us == 0 || now_us < capture_timestamp_us) return;
        const std::uint64_t latency_us = now_us - capture_timestamp_us;
        // steady_clock timestamps are directly comparable for the local loopback
        // performance test. Different machines have unrelated clock domains, so
        // reject implausible deltas instead of exposing misleading diagnostics.
        if (latency_us > 5'000'000) return;
        std::lock_guard<std::mutex> guard(lock_);
        if (samples_ms_.size() >= kMaximumSamples) samples_ms_.erase(samples_ms_.begin());
        samples_ms_.push_back(latency_us / 1000.0);
    }

    LatencySnapshot Snapshot() const {
        std::lock_guard<std::mutex> guard(lock_);
        LatencySnapshot result;
        result.samples = samples_ms_.size();
        if (samples_ms_.empty()) return result;
        auto sorted = samples_ms_;
        std::sort(sorted.begin(), sorted.end());
        result.p50_ms = Percentile(sorted, 50);
        result.p95_ms = Percentile(sorted, 95);
        return result;
    }

private:
    static double Percentile(const std::vector<double>& sorted, const std::size_t percentile) {
        const std::size_t rank = (sorted.size() * percentile + 99) / 100;
        return sorted[(std::max)(std::size_t{1}, rank) - 1];
    }

    static constexpr std::size_t kMaximumSamples = 2048;
    mutable std::mutex lock_;
    std::vector<double> samples_ms_;
};

class D3DRenderer {
public:
    D3DRenderer(
        HWND window, std::atomic<std::uint64_t>* rendered_frames,
        std::atomic<std::uint64_t>* dropped_frames,
        std::atomic<std::uint64_t>* render_microseconds,
        std::atomic<std::uint64_t>* render_attempts,
        PresentationLatencyTracker* latency_tracker)
        : window_(window), rendered_frames_(rendered_frames),
          dropped_frames_(dropped_frames),
          external_render_microseconds_(render_microseconds),
          external_render_attempts_(render_attempts),
          latency_tracker_(latency_tracker) {
        UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT;
        D3D_FEATURE_LEVEL feature_level{};
        HRESULT status = D3D11CreateDevice(
            nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags, nullptr, 0,
            D3D11_SDK_VERSION, &device_, &feature_level, &context_);
        if (FAILED(status)) {
            ThrowIfFailed(
                D3D11CreateDevice(
                    nullptr, D3D_DRIVER_TYPE_WARP, nullptr, flags, nullptr, 0,
                    D3D11_SDK_VERSION, &device_, &feature_level, &context_),
                "create D3D11 video device");
            hardware_ = false;
        }
        ComPtr<IDXGIDevice> dxgi_device;
        ComPtr<ID3D11Multithread> multithread;
        if (SUCCEEDED(context_.As(&multithread))) {
            multithread->SetMultithreadProtected(TRUE);
        }
        ThrowIfFailed(device_.As(&dxgi_device), "query DXGI device");
        ComPtr<IDXGIDevice1> dxgi_device1;
        if (SUCCEEDED(dxgi_device.As(&dxgi_device1))) {
            dxgi_device1->SetMaximumFrameLatency(1);
        }
        ComPtr<IDXGIAdapter> adapter;
        ThrowIfFailed(dxgi_device->GetAdapter(&adapter), "get DXGI adapter");
        ComPtr<IDXGIFactory2> factory;
        ThrowIfFailed(adapter->GetParent(IID_PPV_ARGS(&factory)), "get DXGI factory");
        DXGI_SWAP_CHAIN_DESC1 description{};
        description.Width = 1;
        description.Height = 1;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        description.BufferCount = 2;
        description.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
        description.Scaling = DXGI_SCALING_STRETCH;
        description.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
        description.Flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
        ThrowIfFailed(
            factory->CreateSwapChainForHwnd(device_.Get(), window_, &description, nullptr, nullptr, &swap_chain_),
            "create native video swap chain");
        ComPtr<IDXGISwapChain2> swap_chain2;
        if (SUCCEEDED(swap_chain_.As(&swap_chain2))) {
            swap_chain2->SetMaximumFrameLatency(1);
            frame_latency_waitable_object_ = swap_chain2->GetFrameLatencyWaitableObject();
        }
        factory->MakeWindowAssociation(window_, DXGI_MWA_NO_ALT_ENTER);
        ThrowIfFailed(device_.As(&video_device_), "query D3D11 video device");
        ThrowIfFailed(context_.As(&video_context_), "query D3D11 video context");
        UINT token = 0;
        ThrowIfFailed(MFCreateDXGIDeviceManager(&token, &device_manager_), "create MF DXGI device manager");
        ThrowIfFailed(device_manager_->ResetDevice(device_.Get(), token), "attach D3D11 device to Media Foundation");
        render_worker_ = std::thread(&D3DRenderer::RenderLoop, this);
    }

    ~D3DRenderer() {
        {
            std::lock_guard<std::mutex> guard(queue_lock_);
            stopping_ = true;
            pending_sample_.Reset();
        }
        queue_signal_.notify_all();
        if (render_worker_.joinable()) render_worker_.join();
        if (frame_latency_waitable_object_) CloseHandle(frame_latency_waitable_object_);
    }

    ID3D11Device* device() const { return device_.Get(); }
    IMFDXGIDeviceManager* device_manager() const { return device_manager_.Get(); }
    bool hardware() const { return hardware_; }
    void SetVisible(const bool visible) { visible_ = visible; }
    void SetScaleMode(const bool fill) {
        std::lock_guard<std::mutex> guard(lock_);
        fill_mode_ = fill;
    }
    double AverageRenderMilliseconds() const {
        const std::uint64_t attempts = render_attempts_.load();
        return attempts ? render_microseconds_.load() / attempts / 1000.0 : 0.0;
    }

    void Submit(IMFSample* sample, const UINT input_width, const UINT input_height) {
        if (!sample) return;
        LONGLONG sample_time = 0;
        const std::uint64_t timestamp_us =
            SUCCEEDED(sample->GetSampleTime(&sample_time)) && sample_time > 0
            ? static_cast<std::uint64_t>(sample_time / 10)
            : 0;
        {
            std::lock_guard<std::mutex> guard(queue_lock_);
            if (stopping_) return;
            if (pending_sample_ && dropped_frames_) ++(*dropped_frames_);
            pending_sample_ = sample;
            pending_width_ = input_width;
            pending_height_ = input_height;
            pending_timestamp_us_ = timestamp_us;
        }
        queue_signal_.notify_one();
    }

    void Resize(const UINT width, const UINT height) {
        std::lock_guard<std::mutex> guard(lock_);
        const UINT safe_width = (std::max)(1U, width);
        const UINT safe_height = (std::max)(1U, height);
        if (safe_width == output_width_ && safe_height == output_height_) return;
        back_buffer_.Reset();
        processor_enumerator_.Reset();
        processor_.Reset();
        ThrowIfFailed(
            swap_chain_->ResizeBuffers(
                0, safe_width, safe_height, DXGI_FORMAT_UNKNOWN,
                DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT),
            "resize native video swap chain");
        output_width_ = safe_width;
        output_height_ = safe_height;
    }

private:
    void RenderLoop() {
        while (true) {
            ComPtr<IMFSample> sample;
            UINT width = 0;
            UINT height = 0;
            std::uint64_t capture_timestamp_us = 0;
            {
                std::unique_lock<std::mutex> guard(queue_lock_);
                queue_signal_.wait(guard, [&] { return stopping_ || pending_sample_; });
                if (stopping_) return;
                sample = std::move(pending_sample_);
                width = pending_width_;
                height = pending_height_;
                capture_timestamp_us = pending_timestamp_us_;
            }
            const auto started = std::chrono::steady_clock::now();
            const bool rendered = RenderNow(sample.Get(), width, height);
            render_microseconds_ += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - started).count());
            ++render_attempts_;
            if (external_render_microseconds_) {
                *external_render_microseconds_ += static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - started).count());
            }
            if (external_render_attempts_) ++(*external_render_attempts_);
            if (rendered) {
                if (rendered_frames_) ++(*rendered_frames_);
                if (latency_tracker_) latency_tracker_->Record(capture_timestamp_us);
            } else if (dropped_frames_) {
                ++(*dropped_frames_);
            }
        }
    }

    bool RenderNow(IMFSample* sample, const UINT input_width, const UINT input_height) {
        if (!sample || !visible_) return false;
        if (frame_latency_waitable_object_ &&
            WaitForSingleObject(frame_latency_waitable_object_, 12) != WAIT_OBJECT_0) return false;
        std::lock_guard<std::mutex> guard(lock_);
        if (output_width_ == 0 || output_height_ == 0 || !visible_) return false;
        ComPtr<ID3D11Texture2D> texture;
        UINT subresource = 0;
        ComPtr<IMFMediaBuffer> buffer;
        ThrowIfFailed(sample->GetBufferByIndex(0, &buffer), "get decoded video buffer");
        ComPtr<IMFDXGIBuffer> dxgi_buffer;
        if (SUCCEEDED(buffer.As(&dxgi_buffer))) {
            ThrowIfFailed(dxgi_buffer->GetResource(IID_PPV_ARGS(&texture)), "get decoded D3D11 texture");
            dxgi_buffer->GetSubresourceIndex(&subresource);
        } else {
            BYTE* bytes = nullptr;
            DWORD length = 0;
            ThrowIfFailed(buffer->Lock(&bytes, nullptr, &length), "lock decoded NV12 buffer");
            const DWORD required = input_width * input_height * 3 / 2;
            if (length < required) {
                buffer->Unlock();
                throw std::runtime_error("decoded NV12 buffer is truncated");
            }
            EnsureUploadTexture(input_width, input_height);
            context_->UpdateSubresource(upload_texture_.Get(), 0, nullptr, bytes, input_width, required);
            buffer->Unlock();
            texture = upload_texture_;
        }
        EnsureProcessor(input_width, input_height);
        if (!back_buffer_) ThrowIfFailed(swap_chain_->GetBuffer(0, IID_PPV_ARGS(&back_buffer_)), "get video back buffer");

        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
        input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        input_description.Texture2D.MipSlice = 0;
        input_description.Texture2D.ArraySlice = subresource;
        ComPtr<ID3D11VideoProcessorInputView> input_view;
        ThrowIfFailed(
            video_device_->CreateVideoProcessorInputView(
                texture.Get(), processor_enumerator_.Get(), &input_description, &input_view),
            "create video processor input view");
        D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_description{};
        output_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
        output_description.Texture2D.MipSlice = 0;
        ComPtr<ID3D11VideoProcessorOutputView> output_view;
        ThrowIfFailed(
            video_device_->CreateVideoProcessorOutputView(
                back_buffer_.Get(), processor_enumerator_.Get(), &output_description, &output_view),
            "create video processor output view");

        RECT source{0, 0, static_cast<LONG>(input_width), static_cast<LONG>(input_height)};
        RECT destination{0, 0, static_cast<LONG>(output_width_), static_cast<LONG>(output_height_)};
        if (!fill_mode_) {
            const double scale = (std::min)(
                static_cast<double>(output_width_) / input_width,
                static_cast<double>(output_height_) / input_height);
            const LONG rendered_width = static_cast<LONG>(input_width * scale);
            const LONG rendered_height = static_cast<LONG>(input_height * scale);
            destination = RECT{
                (static_cast<LONG>(output_width_) - rendered_width) / 2,
                (static_cast<LONG>(output_height_) - rendered_height) / 2,
                (static_cast<LONG>(output_width_) + rendered_width) / 2,
                (static_cast<LONG>(output_height_) + rendered_height) / 2};
        }
        D3D11_VIDEO_COLOR black{};
        black.RGBA.A = 1.0f;
        video_context_->VideoProcessorSetOutputBackgroundColor(processor_.Get(), TRUE, &black);
        video_context_->VideoProcessorSetStreamSourceRect(processor_.Get(), 0, TRUE, &source);
        video_context_->VideoProcessorSetStreamDestRect(processor_.Get(), 0, TRUE, &destination);
        video_context_->VideoProcessorSetStreamFrameFormat(
            processor_.Get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.pInputSurface = input_view.Get();
        ThrowIfFailed(
            video_context_->VideoProcessorBlt(processor_.Get(), output_view.Get(), 0, 1, &stream),
            "render decoded video texture");
        const HRESULT present = swap_chain_->Present(0, DXGI_PRESENT_DO_NOT_WAIT);
        if (present == DXGI_ERROR_WAS_STILL_DRAWING) return false;
        ThrowIfFailed(present, "present native video frame");
        return true;
    }

    void EnsureUploadTexture(const UINT width, const UINT height) {
        if (upload_texture_) {
            D3D11_TEXTURE2D_DESC current{};
            upload_texture_->GetDesc(&current);
            if (current.Width == width && current.Height == height) return;
            upload_texture_.Reset();
        }
        D3D11_TEXTURE2D_DESC description{};
        description.Width = width;
        description.Height = height;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_NV12;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_DEFAULT;
        description.BindFlags = D3D11_BIND_DECODER | D3D11_BIND_SHADER_RESOURCE;
        ThrowIfFailed(device_->CreateTexture2D(&description, nullptr, &upload_texture_), "create NV12 upload texture");
    }

    void EnsureProcessor(const UINT width, const UINT height) {
        if (processor_ && processor_input_width_ == width && processor_input_height_ == height) return;
        processor_enumerator_.Reset();
        processor_.Reset();
        D3D11_VIDEO_PROCESSOR_CONTENT_DESC description{};
        description.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        description.InputFrameRate.Numerator = 60;
        description.InputFrameRate.Denominator = 1;
        description.InputWidth = width;
        description.InputHeight = height;
        description.OutputFrameRate.Numerator = 60;
        description.OutputFrameRate.Denominator = 1;
        description.OutputWidth = output_width_;
        description.OutputHeight = output_height_;
        description.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
        ThrowIfFailed(
            video_device_->CreateVideoProcessorEnumerator(&description, &processor_enumerator_),
            "create video processor enumerator");
        ThrowIfFailed(video_device_->CreateVideoProcessor(processor_enumerator_.Get(), 0, &processor_),
            "create video processor");
        processor_input_width_ = width;
        processor_input_height_ = height;
    }

    HWND window_;
    bool hardware_ = true;
    UINT output_width_ = 1;
    UINT output_height_ = 1;
    UINT processor_input_width_ = 0;
    UINT processor_input_height_ = 0;
    std::mutex lock_;
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    ComPtr<ID3D11VideoDevice> video_device_;
    ComPtr<ID3D11VideoContext> video_context_;
    ComPtr<IMFDXGIDeviceManager> device_manager_;
    ComPtr<IDXGISwapChain1> swap_chain_;
    HANDLE frame_latency_waitable_object_ = nullptr;
    ComPtr<ID3D11Texture2D> back_buffer_;
    ComPtr<ID3D11Texture2D> upload_texture_;
    ComPtr<ID3D11VideoProcessorEnumerator> processor_enumerator_;
    ComPtr<ID3D11VideoProcessor> processor_;
    std::atomic<std::uint64_t>* rendered_frames_ = nullptr;
    std::atomic<std::uint64_t>* dropped_frames_ = nullptr;
    std::atomic<std::uint64_t>* external_render_microseconds_ = nullptr;
    std::atomic<std::uint64_t>* external_render_attempts_ = nullptr;
    PresentationLatencyTracker* latency_tracker_ = nullptr;
    std::mutex queue_lock_;
    std::condition_variable queue_signal_;
    ComPtr<IMFSample> pending_sample_;
    UINT pending_width_ = 0;
    UINT pending_height_ = 0;
    std::uint64_t pending_timestamp_us_ = 0;
    bool stopping_ = false;
    std::thread render_worker_;
    std::atomic<std::uint64_t> render_microseconds_{0};
    std::atomic<std::uint64_t> render_attempts_{0};
    std::atomic<bool> visible_{true};
    bool fill_mode_ = false;
};

class MfH264Decoder {
public:
    MfH264Decoder(
        D3DRenderer* renderer, const StreamConfiguration& configuration,
        std::vector<std::uint8_t> sequence_header)
        : renderer_(renderer), configuration_(configuration), sequence_header_(std::move(sequence_header)) {
        MFT_REGISTER_TYPE_INFO input{MFMediaType_Video, MFVideoFormat_H264_ES};
        MFT_REGISTER_TYPE_INFO output{MFMediaType_Video, MFVideoFormat_NV12};
        IMFActivate** activations = nullptr;
        UINT32 count = 0;
        HRESULT enumeration = MFTEnumEx(
            MFT_CATEGORY_VIDEO_DECODER,
            MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
            &input, &output, &activations, &count);
        if (SUCCEEDED(enumeration) && count > 0) {
            hardware_mft_ = ActivateFirst(activations, count);
        }
        ReleaseActivations(activations, count);
        activations = nullptr;
        count = 0;
        if (!transform_) {
            ThrowIfFailed(
                MFTEnumEx(
                    MFT_CATEGORY_VIDEO_DECODER,
                    MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER,
                    &input, &output, &activations, &count),
                "enumerate Media Foundation H.264 decoders");
            ActivateFirst(activations, count);
            ReleaseActivations(activations, count);
        }
        if (!transform_) throw std::runtime_error("no Media Foundation H.264 decoder is available");
        ComPtr<IMFAttributes> attributes;
        if (SUCCEEDED(transform_->GetAttributes(&attributes))) {
            attributes->SetUINT32(MF_LOW_LATENCY, TRUE);
            UINT32 async_value = 0;
            if (SUCCEEDED(attributes->GetUINT32(MF_TRANSFORM_ASYNC, &async_value)) && async_value) {
                asynchronous_ = true;
                ThrowIfFailed(attributes->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE), "unlock async decoder");
                ThrowIfFailed(transform_.As(&event_generator_), "query async decoder events");
            }
        }
        transform_->ProcessMessage(
            MFT_MESSAGE_SET_D3D_MANAGER,
            reinterpret_cast<ULONG_PTR>(renderer_->device_manager()));
        ComPtr<ICodecAPI> codec;
        if (SUCCEEDED(transform_.As(&codec))) {
            VARIANT low_latency;
            VariantInit(&low_latency);
            // The inbox Media Foundation H.264 decoder is the documented
            // exception: CODECAPI_AVLowLatencyMode uses VT_UI4, not VT_BOOL.
            low_latency.vt = VT_UI4;
            low_latency.ulVal = 1;
            codec->SetValue(&CODECAPI_AVLowLatencyMode, &low_latency);
        }
        ConfigureTypes();
        ThrowIfFailed(transform_->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0), "decoder flush");
        ThrowIfFailed(transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0), "decoder begin streaming");
        ThrowIfFailed(transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0), "decoder start stream");
        if (asynchronous_) WaitForNeedInput(2000);
    }

    ~MfH264Decoder() {
        if (transform_) {
            transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
            transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
        }
    }

    const std::string& name() const { return name_; }
    bool hardware() const { return hardware_mft_ || d3d_aware_; }

    UINT Decode(const Message& message) {
        ComPtr<IMFMediaBuffer> buffer;
        ThrowIfFailed(MFCreateMemoryBuffer(static_cast<DWORD>(message.payload.size()), &buffer), "create decoder input buffer");
        BYTE* bytes = nullptr;
        ThrowIfFailed(buffer->Lock(&bytes, nullptr, nullptr), "lock decoder input buffer");
        std::memcpy(bytes, message.payload.data(), message.payload.size());
        buffer->Unlock();
        ThrowIfFailed(buffer->SetCurrentLength(static_cast<DWORD>(message.payload.size())), "set decoder input length");
        ComPtr<IMFSample> sample;
        ThrowIfFailed(MFCreateSample(&sample), "create decoder input sample");
        ThrowIfFailed(sample->AddBuffer(buffer.Get()), "attach decoder input buffer");
        sample->SetSampleTime(static_cast<LONGLONG>(message.timestamp_us) * 10);
        sample->SetSampleDuration(10'000'000LL / configuration_.fps_limit);
        if (message.flags & lanremote::video::Keyframe) {
            sample->SetUINT32(MFSampleExtension_CleanPoint, TRUE);
        }
        if (first_sample_) {
            sample->SetUINT32(MFSampleExtension_Discontinuity, TRUE);
            first_sample_ = false;
        }
        if (asynchronous_ && need_input_requests_ == 0) WaitForNeedInput(1000);
        HRESULT input_status = transform_->ProcessInput(input_stream_, sample.Get(), 0);
        if (input_status == MF_E_NOTACCEPTING) {
            Drain();
            input_status = transform_->ProcessInput(input_stream_, sample.Get(), 0);
        }
        ThrowIfFailed(input_status, "decoder ProcessInput");
        if (asynchronous_) --need_input_requests_;
        return asynchronous_ ? DrainAsync(1000) : Drain();
    }

private:
    static void ReleaseActivations(IMFActivate** activations, const UINT32 count) {
        if (!activations) return;
        for (UINT32 index = 0; index < count; ++index) activations[index]->Release();
        CoTaskMemFree(activations);
    }

    bool ActivateFirst(IMFActivate** activations, const UINT32 count) {
        for (UINT32 index = 0; index < count; ++index) {
            ComPtr<IMFTransform> candidate;
            if (FAILED(activations[index]->ActivateObject(IID_PPV_ARGS(&candidate)))) continue;
            transform_ = candidate;
            WCHAR* value = nullptr;
            UINT32 length = 0;
            if (SUCCEEDED(activations[index]->GetAllocatedString(
                    MFT_FRIENDLY_NAME_Attribute, &value, &length))) {
                name_ = WideToUtf8(std::wstring(value, length));
                CoTaskMemFree(value);
            }
            DWORD input_count = 0;
            DWORD output_count = 0;
            transform_->GetStreamCount(&input_count, &output_count);
            if (input_count == 1 && output_count == 1) {
                transform_->GetStreamIDs(1, &input_stream_, 1, &output_stream_);
            }
            return true;
        }
        return false;
    }

    void ConfigureTypes() {
        ComPtr<IMFMediaType> input_type;
        ThrowIfFailed(MFCreateMediaType(&input_type), "create decoder input type");
        input_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        input_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264_ES);
        if (!sequence_header_.empty()) {
            input_type->SetBlob(
                MF_MT_MPEG_SEQUENCE_HEADER,
                sequence_header_.data(),
                static_cast<UINT32>(sequence_header_.size()));
        }
        ThrowIfFailed(transform_->SetInputType(input_stream_, input_type.Get(), 0), "set decoder H.264 input type");

        ConfigureOutputType();
    }

    void ConfigureOutputType() {
        bool selected = false;
        for (DWORD index = 0;; ++index) {
            ComPtr<IMFMediaType> output_type;
            const HRESULT status = transform_->GetOutputAvailableType(output_stream_, index, &output_type);
            if (status == MF_E_NO_MORE_TYPES) break;
            ThrowIfFailed(status, "enumerate decoder output types");
            GUID subtype{};
            if (FAILED(output_type->GetGUID(MF_MT_SUBTYPE, &subtype)) || subtype != MFVideoFormat_NV12) continue;
            if (SUCCEEDED(transform_->SetOutputType(output_stream_, output_type.Get(), 0))) {
                ConfigureOutputAllocator(output_type.Get());
                selected = true;
                break;
            }
        }
        if (!selected) throw std::runtime_error("the H.264 decoder does not expose NV12 output");
        if (name_.empty()) name_ = "Media Foundation H.264 decoder";
    }

    void ConfigureOutputAllocator(IMFMediaType* output_type) {
        output_allocator_.Reset();
        d3d_aware_ = false;
        ComPtr<IMFAttributes> transform_attributes;
        UINT32 aware = 0;
        if (SUCCEEDED(transform_->GetAttributes(&transform_attributes))) {
            transform_attributes->GetUINT32(MF_SA_D3D11_AWARE, &aware);
        }
        ComPtr<IMFVideoSampleAllocatorEx> allocator;
        if (FAILED(MFCreateVideoSampleAllocatorEx(IID_PPV_ARGS(&allocator))) ||
            FAILED(allocator->SetDirectXManager(renderer_->device_manager()))) return;
        ComPtr<IMFAttributes> attributes;
        if (FAILED(MFCreateAttributes(&attributes, 3))) return;
        attributes->SetUINT32(
            MF_SA_D3D11_BINDFLAGS,
            D3D11_BIND_DECODER | D3D11_BIND_SHADER_RESOURCE);
        attributes->SetUINT32(MF_SA_D3D11_USAGE, D3D11_USAGE_DEFAULT);
        attributes->SetUINT32(MF_SA_BUFFERS_PER_SAMPLE, 1);
        if (FAILED(allocator->InitializeSampleAllocatorEx(
                4, 8, attributes.Get(), output_type))) return;
        output_allocator_ = std::move(allocator);
        d3d_aware_ = aware != 0;
    }

    UINT Drain() {
        UINT rendered = 0;
        while (true) {
            MFT_OUTPUT_STREAM_INFO info{};
            ThrowIfFailed(transform_->GetOutputStreamInfo(output_stream_, &info), "decoder output stream info");
            ComPtr<IMFSample> provided;
            if (!(info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES)) {
                if (output_allocator_ &&
                    SUCCEEDED(output_allocator_->AllocateSample(&provided))) {
                    // Keep the decoded frame on the D3D11 device.
                } else {
                    ComPtr<IMFMediaBuffer> output_buffer;
                    const DWORD capacity = (std::max)(
                        info.cbSize,
                        static_cast<DWORD>(configuration_.encoded_width *
                            configuration_.encoded_height * 3 / 2));
                    ThrowIfFailed(MFCreateMemoryBuffer(
                        capacity, &output_buffer), "create decoder output buffer");
                    ThrowIfFailed(MFCreateSample(
                        &provided), "create decoder output sample");
                    ThrowIfFailed(provided->AddBuffer(
                        output_buffer.Get()), "attach decoder output buffer");
                }
            }
            MFT_OUTPUT_DATA_BUFFER output{};
            output.dwStreamID = output_stream_;
            output.pSample = provided.Get();
            DWORD status_flags = 0;
            const HRESULT status = transform_->ProcessOutput(0, 1, &output, &status_flags);
            ComPtr<IMFSample> returned;
            if (!provided && output.pSample) returned.Attach(output.pSample);
            if (output.pEvents) output.pEvents->Release();
            if (status == MF_E_TRANSFORM_NEED_MORE_INPUT) break;
            if (asynchronous_ && status == E_UNEXPECTED) break;
            if (status == MF_E_TRANSFORM_STREAM_CHANGE) {
                ConfigureOutputType();
                continue;
            }
            ThrowIfFailed(status, "decoder ProcessOutput");
            IMFSample* sample = returned ? returned.Get() : provided.Get();
            if (sample) {
                renderer_->Submit(sample, configuration_.encoded_width, configuration_.encoded_height);
                ++rendered;
            }
        }
        return rendered;
    }

    bool PollEvent(MediaEventType* type) {
        ComPtr<IMFMediaEvent> event;
        const HRESULT status = event_generator_->GetEvent(MF_EVENT_FLAG_NO_WAIT, &event);
        if (status == MF_E_NO_EVENTS_AVAILABLE) return false;
        ThrowIfFailed(status, "read async decoder event");
        ThrowIfFailed(event->GetType(type), "read decoder event type");
        HRESULT event_status = S_OK;
        event->GetStatus(&event_status);
        ThrowIfFailed(event_status, "async decoder event");
        return true;
    }

    void WaitForNeedInput(const DWORD timeout_ms) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            MediaEventType type = MEUnknown;
            while (PollEvent(&type)) {
                if (type == METransformNeedInput) ++need_input_requests_;
                if (type == METransformHaveOutput) have_output_ = true;
            }
            if (need_input_requests_ > 0) return;
            Sleep(1);
        }
        throw std::runtime_error("timed out waiting for the H.264 decoder to accept input");
    }

    UINT DrainAsync(const DWORD timeout_ms) {
        UINT rendered = 0;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            MediaEventType type = MEUnknown;
            bool saw_event = false;
            while (PollEvent(&type)) {
                saw_event = true;
                if (type == METransformNeedInput) ++need_input_requests_;
                if (type == METransformHaveOutput) have_output_ = true;
            }
            if (have_output_) {
                have_output_ = false;
                rendered += Drain();
            }
            if (rendered || need_input_requests_ > 0) return rendered;
            if (!saw_event) Sleep(1);
        }
        return rendered;
    }

    D3DRenderer* renderer_;
    StreamConfiguration configuration_;
    std::vector<std::uint8_t> sequence_header_;
    DWORD input_stream_ = 0;
    DWORD output_stream_ = 0;
    bool hardware_mft_ = false;
    bool d3d_aware_ = false;
    bool first_sample_ = true;
    bool asynchronous_ = false;
    UINT need_input_requests_ = 0;
    bool have_output_ = false;
    std::string name_;
    ComPtr<IMFTransform> transform_;
    ComPtr<IMFMediaEventGenerator> event_generator_;
    ComPtr<IMFVideoSampleAllocatorEx> output_allocator_;
};

class VideoClient {
public:
    explicit VideoClient(HWND parent) : parent_(parent) {
        RegisterWindowClass();
        window_ = CreateWindowExW(
            0, kWindowClass, L"", WS_CHILD | WS_CLIPSIBLINGS,
            0, 0, 1, 1, parent_, nullptr, GetModuleHandleW(nullptr), this);
        if (!window_) {
            const DWORD error = GetLastError();
            throw std::runtime_error(
                "native video child window could not be created (Win32 " +
                std::to_string(error) + ", parent=" +
                std::to_string(reinterpret_cast<std::uintptr_t>(parent_)) +
                ", valid=" + std::to_string(IsWindow(parent_) ? 1 : 0) + ")");
        }
        cursor_window_ = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            kCursorWindowClass, L"", WS_CHILD | WS_CLIPSIBLINGS,
            0, 0, 28, 34, parent_, nullptr, GetModuleHandleW(nullptr), nullptr);
        bool layered_cursor = cursor_window_ != nullptr;
        if (!cursor_window_) {
            // Some WinForms/WebView2 host manifests reject layered child
            // windows. A shaped child window preserves the transparent cursor
            // overlay without making native video availability depend on it.
            cursor_window_ = CreateWindowExW(
                WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
                kCursorWindowClass, L"", WS_CHILD | WS_CLIPSIBLINGS,
                0, 0, 28, 34, parent_, nullptr, GetModuleHandleW(nullptr), nullptr);
        }
        if (!cursor_window_) {
            throw std::runtime_error(
                "native cursor child window could not be created (Win32 " +
                std::to_string(GetLastError()) + ")");
        }
        if (layered_cursor) {
            SetLayeredWindowAttributes(cursor_window_, RGB(1, 2, 3), 0, LWA_COLORKEY);
        } else {
            const POINT arrow[] = {
                {0, 0}, {0, 30}, {7, 24}, {12, 34}, {22, 30}, {17, 21}, {28, 21}
            };
            HRGN region = CreatePolygonRgn(arrow, 7, WINDING);
            if (region && !SetWindowRgn(cursor_window_, region, FALSE)) DeleteObject(region);
        }
    }

    ~VideoClient() {
        Stop();
        if (cursor_window_) DestroyWindow(cursor_window_);
        if (window_) DestroyWindow(window_);
    }

    bool Configure(
        const std::wstring& host, const UINT port, const std::wstring& token,
        const std::wstring& monitor, const UINT fps,
        const int left, const int top, const int width, const int height) {
        if (host.empty() || port == 0 || port > 65535 || token.empty() || token.size() > 512 ||
            token.find_first_of(L"\r\n") != std::wstring::npos ||
            (fps != 30 && fps != 60 && fps != 120) || width < 1 || height < 1) return false;
        Stop();
        host_ = host;
        token_ = token;
        monitor_ = monitor.empty() ? L"all" : monitor;
        port_ = port;
        fps_ = fps;
        stop_ = false;
        generation_ = 0;
        decoded_frames_ = 0;
        rendered_frames_ = 0;
        dropped_frames_ = 0;
        render_microseconds_ = 0;
        render_attempts_ = 0;
        transport_latency_.Reset();
        presentation_latency_.Reset();
        {
            std::lock_guard<std::mutex> guard(status_lock_);
            encoder_name_.clear();
            encoder_selection_.clear();
            capture_backend_.clear();
            color_conversion_.clear();
            encoder_hardware_ = false;
            sender_capture_fps_ = 0.0;
            sender_encode_fps_ = 0.0;
            sender_send_fps_ = 0.0;
            sender_send_mbps_ = 0.0;
            sender_capture_ms_ = 0.0;
            sender_conversion_ms_ = 0.0;
            sender_encode_ms_ = 0.0;
            sender_output_age_ms_ = 0.0;
            sender_adaptive_fps_ = 0;
            sender_queue_depth_ = 0;
            sender_dropped_frames_ = 0;
            actual_decode_fps_ = 0.0;
            actual_render_fps_ = 0.0;
            last_rate_timestamp_us_ = 0;
            last_rate_decoded_frames_ = 0;
            last_rate_rendered_frames_ = 0;
            coordinate_width_ = 0;
            coordinate_height_ = 0;
        }
        SetLayout(left, top, width, height, false);
        SetStatus("connecting", "", "", false, 0, 0);
        worker_ = std::thread(&VideoClient::Run, this);
        return true;
    }

    void Stop() {
        stop_ = true;
        const SOCKET socket = socket_.exchange(INVALID_SOCKET);
        if (socket != INVALID_SOCKET) {
            shutdown(socket, SD_BOTH);
            closesocket(socket);
        }
        if (worker_.joinable()) worker_.join();
        decoder_.reset();
        renderer_.reset();
        if (window_) ShowWindow(window_, SW_HIDE);
        remote_cursor_active_ = false;
        if (cursor_window_) ShowWindow(cursor_window_, SW_HIDE);
    }

    void SetLayout(const int left, const int top, const int width, const int height, const bool visible) {
        if (!window_) return;
        layout_left_ = left;
        layout_top_ = top;
        layout_width_ = (std::max)(1, width);
        layout_height_ = (std::max)(1, height);
        layout_visible_ = visible;
        SetWindowPos(
            window_, HWND_TOP, left, top, layout_width_, layout_height_,
            SWP_NOACTIVATE | (visible ? SWP_SHOWWINDOW : SWP_HIDEWINDOW));
        ApplyExclusionRegion();
        if (renderer_) {
            renderer_->SetVisible(visible);
            renderer_->Resize(layout_width_, layout_height_);
        }
        UpdateCursorPosition();
    }

    void SetExclusions(const int* rectangles, const int count) {
        exclusion_rectangles_.clear();
        if (rectangles && count > 0) {
            const int safe_count = (std::min)(count, 16);
            exclusion_rectangles_.reserve(static_cast<std::size_t>(safe_count));
            for (int index = 0; index < safe_count; ++index) {
                const int* value = rectangles + index * 4;
                if (value[2] <= 0 || value[3] <= 0) continue;
                exclusion_rectangles_.push_back(RECT{
                    value[0], value[1], value[0] + value[2], value[1] + value[3]});
            }
        }
        ApplyExclusionRegion();
    }

    void SetScaleMode(const bool fill) {
        fill_mode_ = fill;
        if (renderer_) renderer_->SetScaleMode(fill);
        UpdateCursorPosition();
    }

    void SetRemoteCursor(
        const int x, const int y, const bool visible,
        const int remote_width, const int remote_height, const bool remote_owner) {
        cursor_x_ = x;
        cursor_y_ = y;
        cursor_remote_width_ = remote_width;
        cursor_remote_height_ = remote_height;
        remote_cursor_active_ = visible && remote_owner && remote_width > 0 && remote_height > 0;
        UpdateCursorPosition();
    }

    std::wstring StatusJson() const {
        std::lock_guard<std::mutex> guard(status_lock_);
        const LatencySnapshot transport_latency = transport_latency_.Snapshot();
        const LatencySnapshot latency = presentation_latency_.Snapshot();
        const std::uint64_t now_us = MonotonicMicroseconds();
        if (last_rate_timestamp_us_ == 0) {
            last_rate_timestamp_us_ = now_us;
            last_rate_decoded_frames_ = decoded_frames_.load();
            last_rate_rendered_frames_ = rendered_frames_.load();
        } else if (now_us > last_rate_timestamp_us_ + 250'000) {
            const double seconds = (now_us - last_rate_timestamp_us_) / 1'000'000.0;
            const std::uint64_t decoded = decoded_frames_.load();
            const std::uint64_t rendered = rendered_frames_.load();
            actual_decode_fps_ = (decoded - last_rate_decoded_frames_) / seconds;
            actual_render_fps_ = (rendered - last_rate_rendered_frames_) / seconds;
            last_rate_timestamp_us_ = now_us;
            last_rate_decoded_frames_ = decoded;
            last_rate_rendered_frames_ = rendered;
        }
        std::ostringstream output;
        output << "{\"state\":\"" << JsonEscape(state_) << "\""
               << ",\"error\":\"" << JsonEscape(error_) << "\""
               << ",\"transport\":\"native_h264_v1\""
               << ",\"decoder\":\"" << JsonEscape(decoder_name_) << "\""
               << ",\"hardware_decode\":" << (decoder_hardware_ ? "true" : "false")
               << ",\"encoder\":\"" << JsonEscape(encoder_name_) << "\""
               << ",\"hardware_encode\":" << (encoder_hardware_ ? "true" : "false")
               << ",\"encoder_selection\":\"" << JsonEscape(encoder_selection_) << "\""
               << ",\"capture_backend\":\"" << JsonEscape(capture_backend_) << "\""
               << ",\"color_conversion\":\"" << JsonEscape(color_conversion_) << "\""
               << ",\"hardware_benchmark_fps\":" << hardware_benchmark_fps_
               << ",\"software_benchmark_fps\":" << software_benchmark_fps_
               << ",\"generation\":" << generation_
               << ",\"requested_fps\":" << fps_
               << ",\"decoded_frames\":" << decoded_frames_.load()
               << ",\"rendered_frames\":" << rendered_frames_.load()
               << ",\"dropped_frames\":" << dropped_frames_.load()
               << ",\"actual_capture_fps\":" << sender_capture_fps_
               << ",\"actual_encode_fps\":" << sender_encode_fps_
               << ",\"actual_send_fps\":" << sender_send_fps_
               << ",\"send_mbps\":" << sender_send_mbps_
               << ",\"actual_decode_fps\":" << actual_decode_fps_
               << ",\"actual_render_fps\":" << actual_render_fps_
               << ",\"capture_ms\":" << sender_capture_ms_
               << ",\"conversion_ms\":" << sender_conversion_ms_
               << ",\"encode_ms\":" << sender_encode_ms_
               << ",\"encoder_output_age_ms\":" << sender_output_age_ms_
               << ",\"adaptive_fps\":" << sender_adaptive_fps_
               << ",\"send_queue_depth\":" << sender_queue_depth_
               << ",\"sender_dropped_frames\":" << sender_dropped_frames_
               << ",\"render_ms\":" << (render_attempts_.load()
                     ? render_microseconds_.load() / render_attempts_.load() / 1000.0 : 0.0)
               << ",\"transport_latency_p50_ms\":" << transport_latency.p50_ms
               << ",\"transport_latency_p95_ms\":" << transport_latency.p95_ms
               << ",\"transport_latency_samples\":" << transport_latency.samples
               << ",\"presentation_latency_p50_ms\":" << latency.p50_ms
               << ",\"presentation_latency_p95_ms\":" << latency.p95_ms
               << ",\"presentation_latency_samples\":" << latency.samples
               << ",\"scale_mode\":\"" << (fill_mode_ ? "fill" : "fit") << "\""
               << ",\"encoded_width\":" << encoded_width_
               << ",\"encoded_height\":" << encoded_height_
               << ",\"coordinate_width\":" << coordinate_width_
               << ",\"coordinate_height\":" << coordinate_height_
               << ",\"bitrate\":" << bitrate_ << '}';
        return Utf8ToWide(output.str());
    }

    LRESULT WindowMessage(const UINT message, const WPARAM w_param, const LPARAM l_param) {
        if (message == WM_ERASEBKGND) return 1;
        if (message == WM_PAINT) {
            PAINTSTRUCT paint{};
            HDC dc = BeginPaint(window_, &paint);
            RECT rect{};
            GetClientRect(window_, &rect);
            FillRect(dc, &rect, static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
            EndPaint(window_, &paint);
            return 0;
        }
        if (message == WM_SETCURSOR) {
            SetCursor(remote_cursor_active_ ? nullptr : LoadCursor(nullptr, IDC_ARROW));
            return TRUE;
        }
        return DefWindowProcW(window_, message, w_param, l_param);
    }

private:
    void ApplyExclusionRegion() {
        if (!window_ || layout_width_ < 1 || layout_height_ < 1) return;
        if (exclusion_rectangles_.empty()) {
            SetWindowRgn(window_, nullptr, TRUE);
            return;
        }
        HRGN visible_region = CreateRectRgn(0, 0, layout_width_, layout_height_);
        if (!visible_region) return;
        for (const RECT& parent_rectangle : exclusion_rectangles_) {
            const int left = (std::max)(0, static_cast<int>(parent_rectangle.left) - layout_left_);
            const int top = (std::max)(0, static_cast<int>(parent_rectangle.top) - layout_top_);
            const int right = (std::min)(layout_width_, static_cast<int>(parent_rectangle.right) - layout_left_);
            const int bottom = (std::min)(layout_height_, static_cast<int>(parent_rectangle.bottom) - layout_top_);
            if (right <= left || bottom <= top) continue;
            HRGN excluded = CreateRectRgn(left, top, right, bottom);
            if (!excluded) continue;
            CombineRgn(visible_region, visible_region, excluded, RGN_DIFF);
            DeleteObject(excluded);
        }
        if (!SetWindowRgn(window_, visible_region, TRUE)) DeleteObject(visible_region);
    }

    static void RegisterWindowClass() {
        static std::once_flag once;
        std::call_once(once, [] {
            WNDCLASSEXW value{};
            value.cbSize = sizeof(value);
            value.lpfnWndProc = WindowProcedure;
            value.hInstance = GetModuleHandleW(nullptr);
            value.hCursor = LoadCursor(nullptr, IDC_ARROW);
            value.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
            value.lpszClassName = kWindowClass;
            if (!RegisterClassExW(&value) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
                throw std::runtime_error("native video window class registration failed");
            }
            WNDCLASSEXW cursor{};
            cursor.cbSize = sizeof(cursor);
            cursor.lpfnWndProc = CursorWindowProcedure;
            cursor.hInstance = GetModuleHandleW(nullptr);
            cursor.hbrBackground = nullptr;
            cursor.lpszClassName = kCursorWindowClass;
            if (!RegisterClassExW(&cursor) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
                throw std::runtime_error("native cursor window class registration failed");
            }
        });
    }

    static LRESULT CALLBACK CursorWindowProcedure(
        HWND window, const UINT message, const WPARAM w_param, const LPARAM l_param) {
        if (message == WM_NCHITTEST) return HTTRANSPARENT;
        if (message == WM_ERASEBKGND) return 1;
        if (message == WM_PAINT) {
            PAINTSTRUCT paint{};
            HDC dc = BeginPaint(window, &paint);
            RECT rect{};
            GetClientRect(window, &rect);
            HBRUSH transparent = CreateSolidBrush(RGB(1, 2, 3));
            FillRect(dc, &rect, transparent);
            DeleteObject(transparent);
            const POINT arrow[] = {
                {2, 1}, {2, 27}, {8, 21}, {13, 33}, {19, 30},
                {14, 19}, {25, 19}
            };
            HPEN outline = CreatePen(PS_SOLID, 2, RGB(18, 20, 24));
            HBRUSH fill = CreateSolidBrush(RGB(248, 249, 252));
            HGDIOBJ previous_pen = SelectObject(dc, outline);
            HGDIOBJ previous_brush = SelectObject(dc, fill);
            Polygon(dc, arrow, 7);
            SelectObject(dc, previous_brush);
            SelectObject(dc, previous_pen);
            DeleteObject(fill);
            DeleteObject(outline);
            EndPaint(window, &paint);
            return 0;
        }
        return DefWindowProcW(window, message, w_param, l_param);
    }

    static LRESULT CALLBACK WindowProcedure(HWND window, UINT message, WPARAM w_param, LPARAM l_param) {
        VideoClient* client = reinterpret_cast<VideoClient*>(GetWindowLongPtrW(window, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            auto* create = reinterpret_cast<CREATESTRUCTW*>(l_param);
            client = static_cast<VideoClient*>(create->lpCreateParams);
            client->window_ = window;
            SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(client));
        }
        return client ? client->WindowMessage(message, w_param, l_param) : DefWindowProcW(window, message, w_param, l_param);
    }

    void UpdateCursorPosition() {
        if (!cursor_window_ || !window_ || !remote_cursor_active_) {
            if (cursor_window_) ShowWindow(cursor_window_, SW_HIDE);
            return;
        }
        RECT video{};
        GetWindowRect(window_, &video);
        MapWindowPoints(HWND_DESKTOP, parent_, reinterpret_cast<POINT*>(&video), 2);
        const int video_width = video.right - video.left;
        const int video_height = video.bottom - video.top;
        const int remote_width = cursor_remote_width_.load();
        const int remote_height = cursor_remote_height_.load();
        if (video_width < 1 || video_height < 1 || remote_width < 1 || remote_height < 1) {
            ShowWindow(cursor_window_, SW_HIDE);
            return;
        }
        if (fill_mode_) {
            const double scale_x = static_cast<double>(video_width) / remote_width;
            const double scale_y = static_cast<double>(video_height) / remote_height;
            const int left = video.left + static_cast<int>(cursor_x_.load() * scale_x) - 2;
            const int top = video.top + static_cast<int>(cursor_y_.load() * scale_y) - 1;
            SetWindowPos(
                cursor_window_, HWND_TOP, left, top, 28, 34,
                SWP_NOACTIVATE | SWP_SHOWWINDOW);
            return;
        }
        const double scale = (std::min)(
                static_cast<double>(video_width) / remote_width,
                static_cast<double>(video_height) / remote_height);
        const int content_width = static_cast<int>(remote_width * scale);
        const int content_height = static_cast<int>(remote_height * scale);
        const int left = video.left + (video_width - content_width) / 2 +
            static_cast<int>(cursor_x_.load() * scale) - 2;
        const int top = video.top + (video_height - content_height) / 2 +
            static_cast<int>(cursor_y_.load() * scale) - 1;
        SetWindowPos(
            cursor_window_, HWND_TOP, left, top, 28, 34,
            SWP_NOACTIVATE | SWP_SHOWWINDOW);
    }

    void SetStatus(
        std::string state, std::string error, std::string decoder,
        const bool hardware, const UINT encoded_width, const UINT encoded_height) {
        std::lock_guard<std::mutex> guard(status_lock_);
        state_ = std::move(state);
        error_ = std::move(error);
        decoder_name_ = std::move(decoder);
        decoder_hardware_ = hardware;
        encoded_width_ = encoded_width;
        encoded_height_ = encoded_height;
    }

    void SetStreamConfigurationStatus(const StreamConfiguration& configuration) {
        std::lock_guard<std::mutex> guard(status_lock_);
        encoder_name_ = configuration.encoder;
        encoder_hardware_ = configuration.encoder_hardware;
        encoder_selection_ = configuration.encoder_selection;
        capture_backend_ = configuration.capture;
        color_conversion_ = configuration.color_conversion;
        hardware_benchmark_fps_ = configuration.hardware_benchmark_fps;
        software_benchmark_fps_ = configuration.software_benchmark_fps;
        coordinate_width_ = configuration.coordinate_width;
        coordinate_height_ = configuration.coordinate_height;
    }

    void SetSenderReport(const Message& message) {
        const std::string json(message.payload.begin(), message.payload.end());
        std::lock_guard<std::mutex> guard(status_lock_);
        sender_capture_fps_ = JsonNumber(json, "capture_fps");
        sender_encode_fps_ = JsonNumber(json, "encode_fps");
        sender_send_fps_ = JsonNumber(json, "send_fps");
        sender_send_mbps_ = JsonNumber(json, "send_mbps");
        sender_capture_ms_ = JsonNumber(json, "capture_ms");
        sender_conversion_ms_ = JsonNumber(json, "conversion_ms");
        sender_encode_ms_ = JsonNumber(json, "encode_ms");
        sender_output_age_ms_ = JsonNumber(json, "output_age_ms");
        sender_adaptive_fps_ = JsonInteger(json, "adaptive_fps");
        bitrate_ = static_cast<UINT>(JsonInteger(json, "bitrate", static_cast<int>(bitrate_)));
        sender_queue_depth_ = JsonInteger(json, "send_queue_depth");
        sender_dropped_frames_ = static_cast<std::uint64_t>(
            (std::max)(0, JsonInteger(json, "capture_dropped")) +
            (std::max)(0, JsonInteger(json, "send_dropped")));
    }

    SOCKET Connect() {
        WSADATA data{};
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0) throw std::runtime_error("Winsock initialization failed");
        addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;
        addrinfo* addresses = nullptr;
        const std::string host = WideToUtf8(host_);
        const std::string service = std::to_string(port_);
        if (getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses) != 0) {
            throw std::runtime_error("native video host resolution failed");
        }
        SOCKET connected = INVALID_SOCKET;
        for (addrinfo* address = addresses; address; address = address->ai_next) {
            SOCKET candidate = socket(address->ai_family, address->ai_socktype, address->ai_protocol);
            if (candidate == INVALID_SOCKET) continue;
            BOOL no_delay = TRUE;
            setsockopt(candidate, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&no_delay), sizeof(no_delay));
            DWORD timeout = 30000;
            setsockopt(candidate, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
            setsockopt(candidate, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
            if (connect(candidate, address->ai_addr, static_cast<int>(address->ai_addrlen)) == 0) {
                connected = candidate;
                break;
            }
            closesocket(candidate);
        }
        freeaddrinfo(addresses);
        if (connected == INVALID_SOCKET) throw std::runtime_error("native video connection failed");
        return connected;
    }

    void UpgradeConnection(const SOCKET socket) {
        const std::string host = WideToUtf8(host_);
        const std::string token = WideToUtf8(token_);
        const std::string monitor = UrlEncode(WideToUtf8(monitor_));
        std::ostringstream request;
        request << "CONNECT /video-stream?monitor=" << monitor << "&fps=" << fps_ << " HTTP/1.1\r\n"
                << "Host: " << host << ':' << port_ << "\r\n"
                << "X-Remote-Token: " << token << "\r\n"
                << "X-LAN-Video-Protocol: 1\r\n"
                << "Connection: keep-alive\r\n\r\n";
        const std::string bytes = request.str();
        if (!SendAll(socket, bytes.data(), bytes.size())) throw std::runtime_error("native video CONNECT request failed");
        std::string response;
        while (response.size() < 16 * 1024 && response.find("\r\n\r\n") == std::string::npos) {
            char character = 0;
            if (!ReceiveAll(socket, &character, 1)) throw std::runtime_error("native video CONNECT response ended early");
            response.push_back(character);
        }
        if (response.size() >= 16 * 1024) throw std::runtime_error("native video CONNECT response is too large");
        const std::size_t first_line = response.find("\r\n");
        if (first_line == std::string::npos || response.substr(0, first_line).find(" 200 ") == std::string::npos) {
            throw std::runtime_error("native H.264 was rejected; use the MJPEG compatibility path");
        }
        std::string lowered = response;
        std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](const unsigned char value) {
            return static_cast<char>(std::tolower(value));
        });
        if (lowered.find("x-lan-video-protocol: 1\r\n") == std::string::npos) {
            throw std::runtime_error("the native video protocol was not negotiated");
        }
    }

    void RequestKeyframe(const SOCKET socket, const std::uint32_t generation) {
        Message request;
        request.type = MessageType::RequestKeyframe;
        request.generation = generation;
        request.timestamp_us = MonotonicMicroseconds();
        const auto bytes = lanremote::video::PackMessage(request);
        SendAll(socket, bytes.data(), bytes.size());
    }

    void SendReceiverReport(
        const SOCKET socket, const std::uint32_t generation,
        const double decode_fps, const double render_fps,
        const std::uint64_t dropped_frames) {
        std::ostringstream json;
        json << "{\"decode_fps\":" << decode_fps
             << ",\"render_fps\":" << render_fps
             << ",\"dropped_frames\":" << dropped_frames
             << ",\"render_capacity_fps\":" << DisplayRefreshHertz(parent_) << '}';
        const std::string text = json.str();
        Message report;
        report.type = MessageType::ReceiverReport;
        report.generation = generation;
        report.timestamp_us = MonotonicMicroseconds();
        report.fps_limit = static_cast<std::uint16_t>(fps_);
        report.payload.assign(text.begin(), text.end());
        const auto bytes = lanremote::video::PackMessage(report);
        SendAll(socket, bytes.data(), bytes.size());
    }

    void Run() {
        HRESULT com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        bool com_initialized = SUCCEEDED(com);
        bool mf_initialized = false;
        try {
            ThrowIfFailed(com, "initialize native video COM");
            ThrowIfFailed(MFStartup(MF_VERSION, MFSTARTUP_FULL), "start Media Foundation");
            mf_initialized = true;
            renderer_ = std::make_unique<D3DRenderer>(
                window_, &rendered_frames_, &dropped_frames_,
                &render_microseconds_, &render_attempts_, &presentation_latency_);
            renderer_->SetScaleMode(fill_mode_);
            renderer_->SetVisible(layout_visible_);
            RECT client{};
            GetClientRect(window_, &client);
            renderer_->Resize(client.right - client.left, client.bottom - client.top);
            const SOCKET connected = Connect();
            socket_ = connected;
            UpgradeConnection(connected);

            std::uint32_t current_generation = 0;
            bool waiting_for_keyframe = true;
            bool first_frame_rendered = false;
            std::uint64_t first_frame_rendered_count = rendered_frames_.load();
            std::uint64_t next_first_frame_keyframe = 0;
            StreamConfiguration current_configuration;
            bool have_configuration = false;
            std::uint64_t receiver_report_started = MonotonicMicroseconds();
            std::uint64_t receiver_report_decoded = 0;
            std::uint64_t receiver_report_rendered = 0;
            std::uint64_t receiver_report_dropped = 0;
            while (!stop_) {
                Message message;
                if (!ReceiveMessage(connected, &message)) {
                    if (!stop_) throw std::runtime_error("native video connection closed");
                    break;
                }
                if (message.type == MessageType::StreamConfig) {
                    const StreamConfiguration configuration = ParseConfiguration(message);
                    current_generation = message.generation;
                    generation_ = current_generation;
                    bitrate_ = configuration.bitrate;
                    current_configuration = configuration;
                    have_configuration = true;
                    SetStreamConfigurationStatus(configuration);
                    decoder_.reset();
                    waiting_for_keyframe = true;
                    first_frame_rendered = false;
                    first_frame_rendered_count = rendered_frames_.load();
                    next_first_frame_keyframe = MonotonicMicroseconds() + 250'000;
                    SetStatus(
                        "connecting", "", "", false,
                        configuration.encoded_width, configuration.encoded_height);
                    RequestKeyframe(connected, current_generation);
                    SendReceiverReport(connected, current_generation, 0.0, 0.0, 0);
                    continue;
                }
                if (message.type == MessageType::VideoAccessUnit) {
                    if (!have_configuration || message.generation != current_generation) {
                        ++dropped_frames_;
                        continue;
                    }
                    if (waiting_for_keyframe && !(message.flags & lanremote::video::Keyframe)) {
                        ++dropped_frames_;
                        continue;
                    }
                    if (message.flags & lanremote::video::Keyframe) {
                        waiting_for_keyframe = false;
                        if (!decoder_) {
                            auto sequence_header = ExtractH264ParameterSets(message.payload);
                            if (sequence_header.empty()) {
                                waiting_for_keyframe = true;
                                RequestKeyframe(connected, current_generation);
                                ++dropped_frames_;
                                continue;
                            }
                            decoder_ = std::make_unique<MfH264Decoder>(
                                renderer_.get(), current_configuration, std::move(sequence_header));
                            SetStatus(
                                "decoding", "", decoder_->name(), decoder_->hardware(),
                                current_configuration.encoded_width,
                                current_configuration.encoded_height);
                        }
                    }
                    if (!decoder_) {
                        ++dropped_frames_;
                        continue;
                    }
                    transport_latency_.Record(message.timestamp_us);
                    ++decoded_frames_;
                    decoder_->Decode(message);
                    const std::uint64_t now_us = MonotonicMicroseconds();
                    if (!first_frame_rendered && rendered_frames_.load() > first_frame_rendered_count) {
                        first_frame_rendered = true;
                        SetStatus(
                            "streaming", "", decoder_->name(), decoder_->hardware(),
                            current_configuration.encoded_width,
                            current_configuration.encoded_height);
                    } else if (!first_frame_rendered && now_us >= next_first_frame_keyframe) {
                        RequestKeyframe(connected, current_generation);
                        next_first_frame_keyframe = now_us + 250'000;
                    }
                    if (now_us >= receiver_report_started + 1'000'000) {
                        const double seconds =
                            (now_us - receiver_report_started) / 1'000'000.0;
                        const std::uint64_t decoded = decoded_frames_.load();
                        const std::uint64_t rendered = rendered_frames_.load();
                        const std::uint64_t dropped = dropped_frames_.load();
                        SendReceiverReport(
                            connected, current_generation,
                            (decoded - receiver_report_decoded) / seconds,
                            (rendered - receiver_report_rendered) / seconds,
                            dropped - receiver_report_dropped);
                        receiver_report_started = now_us;
                        receiver_report_decoded = decoded;
                        receiver_report_rendered = rendered;
                        receiver_report_dropped = dropped;
                    }
                    continue;
                }
                if (message.type == MessageType::SenderReport) {
                    if (message.generation == current_generation) {
                        SetSenderReport(message);
                        const std::uint64_t now_us = MonotonicMicroseconds();
                        if (!first_frame_rendered && now_us >= next_first_frame_keyframe) {
                            RequestKeyframe(connected, current_generation);
                            next_first_frame_keyframe = now_us + 250'000;
                        }
                    }
                    continue;
                }
                if (message.type == MessageType::StreamEnd) {
                    const std::string reason(message.payload.begin(), message.payload.end());
                    throw std::runtime_error(reason.find("secure_desktop") != std::string::npos
                        ? "secure desktop requires MJPEG fallback"
                        : "native video stream ended");
                }
                if (message.type == MessageType::Error) {
                    throw std::runtime_error(std::string(message.payload.begin(), message.payload.end()));
                }
            }
        } catch (const std::exception& error) {
            if (!stop_) SetStatus("failed", error.what(), "", false, 0, 0);
        }
        const SOCKET socket = socket_.exchange(INVALID_SOCKET);
        if (socket != INVALID_SOCKET) closesocket(socket);
        decoder_.reset();
        renderer_.reset();
        if (mf_initialized) MFShutdown();
        if (com_initialized) CoUninitialize();
        WSACleanup();
    }

    HWND parent_ = nullptr;
    HWND window_ = nullptr;
    HWND cursor_window_ = nullptr;
    std::wstring host_;
    std::wstring token_;
    std::wstring monitor_;
    UINT port_ = 0;
    UINT fps_ = 60;
    std::atomic<bool> stop_{false};
    std::atomic<bool> remote_cursor_active_{false};
    std::atomic<bool> layout_visible_{false};
    std::atomic<bool> fill_mode_{false};
    int layout_left_ = 0;
    int layout_top_ = 0;
    int layout_width_ = 1;
    int layout_height_ = 1;
    std::vector<RECT> exclusion_rectangles_;
    std::atomic<int> cursor_x_{0};
    std::atomic<int> cursor_y_{0};
    std::atomic<int> cursor_remote_width_{0};
    std::atomic<int> cursor_remote_height_{0};
    std::atomic<SOCKET> socket_{INVALID_SOCKET};
    std::thread worker_;
    std::unique_ptr<D3DRenderer> renderer_;
    std::unique_ptr<MfH264Decoder> decoder_;
    mutable std::mutex status_lock_;
    std::string state_ = "stopped";
    std::string error_;
    std::string decoder_name_;
    std::string encoder_name_;
    std::string encoder_selection_;
    std::string capture_backend_;
    std::string color_conversion_;
    bool decoder_hardware_ = false;
    bool encoder_hardware_ = false;
    double hardware_benchmark_fps_ = 0.0;
    double software_benchmark_fps_ = 0.0;
    double sender_capture_fps_ = 0.0;
    double sender_encode_fps_ = 0.0;
    double sender_send_fps_ = 0.0;
    double sender_send_mbps_ = 0.0;
    double sender_capture_ms_ = 0.0;
    double sender_conversion_ms_ = 0.0;
    double sender_encode_ms_ = 0.0;
    double sender_output_age_ms_ = 0.0;
    int sender_adaptive_fps_ = 0;
    int sender_queue_depth_ = 0;
    std::uint64_t sender_dropped_frames_ = 0;
    mutable double actual_decode_fps_ = 0.0;
    mutable double actual_render_fps_ = 0.0;
    mutable std::uint64_t last_rate_timestamp_us_ = 0;
    mutable std::uint64_t last_rate_decoded_frames_ = 0;
    mutable std::uint64_t last_rate_rendered_frames_ = 0;
    std::atomic<std::uint64_t> decoded_frames_{0};
    std::atomic<std::uint64_t> rendered_frames_{0};
    std::atomic<std::uint64_t> dropped_frames_{0};
    std::atomic<std::uint64_t> render_microseconds_{0};
    std::atomic<std::uint64_t> render_attempts_{0};
    PresentationLatencyTracker transport_latency_;
    PresentationLatencyTracker presentation_latency_;
    std::atomic<std::uint32_t> generation_{0};
    UINT encoded_width_ = 0;
    UINT encoded_height_ = 0;
    UINT coordinate_width_ = 0;
    UINT coordinate_height_ = 0;
    UINT bitrate_ = 0;
};

}  // namespace

extern "C" __declspec(dllexport) void* __stdcall LANRemoteVideoCreate(const HWND parent) {
    try {
        return new VideoClient(parent);
    } catch (const std::exception& error) {
        SetApiError(error.what());
        return nullptr;
    }
}

extern "C" __declspec(dllexport) int __stdcall LANRemoteVideoConfigure(
    void* handle, const wchar_t* host, const UINT port, const wchar_t* token,
    const wchar_t* monitor, const UINT fps,
    const int left, const int top, const int width, const int height) {
    if (!handle || !host || !token) return FALSE;
    try {
        return static_cast<VideoClient*>(handle)->Configure(
            host, port, token, monitor ? monitor : L"all", fps,
            left, top, width, height) ? TRUE : FALSE;
    } catch (...) {
        SetApiError("native video configuration failed before the worker started");
        return FALSE;
    }
}

extern "C" __declspec(dllexport) void __stdcall LANRemoteVideoSetLayout(
    void* handle, const int left, const int top, const int width, const int height, const int visible) {
    if (handle) static_cast<VideoClient*>(handle)->SetLayout(left, top, width, height, visible != 0);
}

extern "C" __declspec(dllexport) void __stdcall LANRemoteVideoSetExclusions(
    void* handle, const int* rectangles, const int count) {
    if (handle) static_cast<VideoClient*>(handle)->SetExclusions(rectangles, count);
}

extern "C" __declspec(dllexport) void __stdcall LANRemoteVideoSetScaleMode(
    void* handle, const int fill) {
    if (handle) static_cast<VideoClient*>(handle)->SetScaleMode(fill != 0);
}

extern "C" __declspec(dllexport) void __stdcall LANRemoteVideoSetCursor(
    void* handle, const int x, const int y, const int visible,
    const int remote_width, const int remote_height, const int remote_owner) {
    if (handle) {
        static_cast<VideoClient*>(handle)->SetRemoteCursor(
            x, y, visible != 0, remote_width, remote_height, remote_owner != 0);
    }
}

extern "C" __declspec(dllexport) int __stdcall LANRemoteVideoGetStatus(
    void* handle, wchar_t* output, const int capacity) {
    if (!handle || !output || capacity < 2) return 0;
    const std::wstring value = static_cast<VideoClient*>(handle)->StatusJson();
    const int length = (std::min)(capacity - 1, static_cast<int>(value.size()));
    std::memcpy(output, value.data(), static_cast<std::size_t>(length) * sizeof(wchar_t));
    output[length] = L'\0';
    return length;
}

extern "C" __declspec(dllexport) void __stdcall LANRemoteVideoStop(void* handle) {
    if (handle) static_cast<VideoClient*>(handle)->Stop();
}

extern "C" __declspec(dllexport) void __stdcall LANRemoteVideoDestroy(void* handle) {
    delete static_cast<VideoClient*>(handle);
}

extern "C" __declspec(dllexport) int __stdcall LANRemoteVideoGetLastError(
    wchar_t* output, const int capacity) {
    if (!output || capacity < 2) return 0;
    std::string value;
    {
        std::lock_guard<std::mutex> guard(api_error_lock);
        value = api_error;
    }
    const std::wstring wide = Utf8ToWide(value);
    const int length = (std::min)(capacity - 1, static_cast<int>(wide.size()));
    std::memcpy(output, wide.data(), static_cast<std::size_t>(length) * sizeof(wchar_t));
    output[length] = L'\0';
    return length;
}
