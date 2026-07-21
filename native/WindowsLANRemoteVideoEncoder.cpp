#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>
#include <codecapi.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dxgi1_2.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mmsystem.h>
#include <icodecapi.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <iomanip>
#include <iostream>
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

constexpr UINT32 kDefaultBitrate30 = 8'000'000;
constexpr UINT32 kDefaultBitrate60 = 16'000'000;
constexpr UINT32 kDefaultBitrate120 = 12'000'000;

void ThrowIfFailed(const HRESULT hr, const char* operation) {
    if (FAILED(hr)) {
        std::ostringstream message;
        message << operation << " failed (0x" << std::hex << std::uppercase
                << static_cast<unsigned long>(hr) << ')';
        throw std::runtime_error(message.str());
    }
}

std::string WideToUtf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        nullptr, 0, nullptr, nullptr);
    if (size <= 0) return {};
    std::string result(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        result.data(), size, nullptr, nullptr);
    return result;
}

std::string JsonEscape(const std::string& value) {
    std::ostringstream output;
    for (const unsigned char character : value) {
        switch (character) {
            case '\\': output << "\\\\"; break;
            case '"': output << "\\\""; break;
            case '\b': output << "\\b"; break;
            case '\f': output << "\\f"; break;
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

double JsonNumber(const std::string& json, const std::string& name, const double fallback = 0.0) {
    const std::string marker = "\"" + name + "\":";
    const std::size_t marker_position = json.find(marker);
    if (marker_position == std::string::npos) return fallback;
    const char* start = json.c_str() + marker_position + marker.size();
    char* end = nullptr;
    const double value = std::strtod(start, &end);
    return end && end != start ? value : fallback;
}

std::uint64_t MonotonicMicroseconds() {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
}

struct Options {
    std::wstring monitor = L"all";
    std::uint16_t fps = 60;
    std::uint32_t generation = 1;
    bool test_pattern = false;
};

Options ParseOptions(const int argc, wchar_t** argv) {
    Options result;
    for (int index = 1; index < argc; ++index) {
        const std::wstring argument = argv[index];
        if (argument == L"--monitor" && index + 1 < argc) {
            result.monitor = argv[++index];
        } else if (argument == L"--fps" && index + 1 < argc) {
            result.fps = static_cast<std::uint16_t>(_wtoi(argv[++index]));
        } else if (argument == L"--generation" && index + 1 < argc) {
            wchar_t* end = nullptr;
            const unsigned long value = wcstoul(argv[++index], &end, 10);
            if (!end || *end != L'\0') throw std::runtime_error("--generation is invalid");
            result.generation = static_cast<std::uint32_t>(value);
        } else if (argument == L"--test-pattern") {
            result.test_pattern = true;
        } else {
            throw std::runtime_error("unknown or incomplete command-line option");
        }
    }
    if (result.fps != 30 && result.fps != 60 && result.fps != 120) {
        throw std::runtime_error("--fps must be 30, 60, or 120");
    }
    if (result.generation == 0) result.generation = 1;
    return result;
}

class OutputCapture {
public:
    OutputCapture(
        ComPtr<ID3D11Device> device, ComPtr<ID3D11DeviceContext> context,
        ComPtr<IDXGIOutput> output)
        : device_(std::move(device)), context_(std::move(context)) {
        ThrowIfFailed(output->GetDesc(&description_), "IDXGIOutput::GetDesc");
        ComPtr<IDXGIOutput1> output1;
        ThrowIfFailed(output.As(&output1), "IDXGIOutput1 query");
        ThrowIfFailed(output1->DuplicateOutput(device_.Get(), &duplication_), "DuplicateOutput");
        logical_width_ = static_cast<UINT>(
            description_.DesktopCoordinates.right - description_.DesktopCoordinates.left);
        logical_height_ = static_cast<UINT>(
            description_.DesktopCoordinates.bottom - description_.DesktopCoordinates.top);
        pixels_.resize(static_cast<std::size_t>(logical_width_) * logical_height_ * 4);
    }

    const DXGI_OUTPUT_DESC& description() const { return description_; }
    UINT width() const { return logical_width_; }
    UINT height() const { return logical_height_; }
    const std::vector<std::uint8_t>& pixels() const { return pixels_; }
    bool initialized() const { return initialized_; }
    bool rotation_identity() const {
        return description_.Rotation == DXGI_MODE_ROTATION_UNSPECIFIED ||
            description_.Rotation == DXGI_MODE_ROTATION_IDENTITY;
    }

    bool Acquire(const UINT timeout_ms, const bool copy_to_cpu) {
        DXGI_OUTDUPL_FRAME_INFO frame_info{};
        ComPtr<IDXGIResource> desktop_resource;
        const HRESULT acquire = duplication_->AcquireNextFrame(
            timeout_ms, &frame_info, &desktop_resource);
        if (acquire == DXGI_ERROR_WAIT_TIMEOUT) return false;
        if (acquire == DXGI_ERROR_ACCESS_LOST) {
            throw std::runtime_error("desktop duplication access was lost");
        }
        ThrowIfFailed(acquire, "AcquireNextFrame");

        struct FrameGuard {
            IDXGIOutputDuplication* duplication;
            ~FrameGuard() { if (duplication) duplication->ReleaseFrame(); }
        } guard{duplication_.Get()};

        ComPtr<ID3D11Texture2D> frame;
        ThrowIfFailed(desktop_resource.As(&frame), "desktop texture query");
        D3D11_TEXTURE2D_DESC frame_desc{};
        frame->GetDesc(&frame_desc);
        EnsureFrameTexture(frame_desc);
        context_->CopyResource(frame_texture_.Get(), frame.Get());
        if (!copy_to_cpu) {
            initialized_ = true;
            return true;
        }
        EnsureStaging(frame_desc);
        context_->CopyResource(staging_.Get(), frame_texture_.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};
        ThrowIfFailed(context_->Map(staging_.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Map desktop frame");
        CopyRotated(
            static_cast<const std::uint8_t*>(mapped.pData), mapped.RowPitch,
            frame_desc.Width, frame_desc.Height);
        context_->Unmap(staging_.Get(), 0);
        initialized_ = true;
        return true;
    }

    void CopyTo(ID3D11Texture2D* destination, const UINT left, const UINT top) {
        if (!initialized_ || !frame_texture_ || !destination) return;
        context_->CopySubresourceRegion(
            destination, 0, left, top, 0, frame_texture_.Get(), 0, nullptr);
    }

private:
    void EnsureFrameTexture(const D3D11_TEXTURE2D_DESC& source) {
        if (frame_texture_) {
            D3D11_TEXTURE2D_DESC existing{};
            frame_texture_->GetDesc(&existing);
            if (existing.Width == source.Width && existing.Height == source.Height &&
                existing.Format == source.Format) return;
            frame_texture_.Reset();
        }
        D3D11_TEXTURE2D_DESC texture = source;
        texture.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        texture.MiscFlags = 0;
        texture.Usage = D3D11_USAGE_DEFAULT;
        texture.CPUAccessFlags = 0;
        ThrowIfFailed(device_->CreateTexture2D(&texture, nullptr, &frame_texture_),
            "create persistent desktop texture");
    }

    void EnsureStaging(const D3D11_TEXTURE2D_DESC& source) {
        if (staging_) {
            D3D11_TEXTURE2D_DESC existing{};
            staging_->GetDesc(&existing);
            if (existing.Width == source.Width && existing.Height == source.Height &&
                existing.Format == source.Format) return;
            staging_.Reset();
        }
        D3D11_TEXTURE2D_DESC staging_desc = source;
        staging_desc.BindFlags = 0;
        staging_desc.MiscFlags = 0;
        staging_desc.Usage = D3D11_USAGE_STAGING;
        staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        ThrowIfFailed(device_->CreateTexture2D(&staging_desc, nullptr, &staging_), "Create staging texture");
    }

    void CopyRotated(
        const std::uint8_t* source, const UINT source_pitch,
        const UINT source_width, const UINT source_height) {
        if (description_.Rotation == DXGI_MODE_ROTATION_UNSPECIFIED ||
            description_.Rotation == DXGI_MODE_ROTATION_IDENTITY) {
            const UINT copy_width = (std::min)(logical_width_, source_width);
            const UINT copy_height = (std::min)(logical_height_, source_height);
            for (UINT y = 0; y < copy_height; ++y) {
                std::memcpy(
                    pixels_.data() + static_cast<std::size_t>(y) * logical_width_ * 4,
                    source + static_cast<std::size_t>(y) * source_pitch,
                    static_cast<std::size_t>(copy_width) * 4);
            }
            return;
        }
        for (UINT y = 0; y < logical_height_; ++y) {
            for (UINT x = 0; x < logical_width_; ++x) {
                UINT source_x = x;
                UINT source_y = y;
                switch (description_.Rotation) {
                    case DXGI_MODE_ROTATION_ROTATE90:
                        source_x = y;
                        source_y = source_height - 1 - x;
                        break;
                    case DXGI_MODE_ROTATION_ROTATE180:
                        source_x = source_width - 1 - x;
                        source_y = source_height - 1 - y;
                        break;
                    case DXGI_MODE_ROTATION_ROTATE270:
                        source_x = source_width - 1 - y;
                        source_y = x;
                        break;
                    default:
                        break;
                }
                if (source_x >= source_width || source_y >= source_height) continue;
                const auto* input = source + static_cast<std::size_t>(source_y) * source_pitch + source_x * 4;
                auto* output = pixels_.data() +
                    (static_cast<std::size_t>(y) * logical_width_ + x) * 4;
                output[0] = input[0];
                output[1] = input[1];
                output[2] = input[2];
                output[3] = 255;
            }
        }
    }

    DXGI_OUTPUT_DESC description_{};
    UINT logical_width_ = 0;
    UINT logical_height_ = 0;
    bool initialized_ = false;
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    ComPtr<IDXGIOutputDuplication> duplication_;
    ComPtr<ID3D11Texture2D> frame_texture_;
    ComPtr<ID3D11Texture2D> staging_;
    std::vector<std::uint8_t> pixels_;
};

class DesktopCapture {
public:
    explicit DesktopCapture(const std::wstring& monitor) {
        ComPtr<IDXGIFactory1> factory;
        ThrowIfFailed(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");
        RECT union_rect{LONG_MAX, LONG_MAX, LONG_MIN, LONG_MIN};
        for (UINT adapter_index = 0;; ++adapter_index) {
            ComPtr<IDXGIAdapter1> adapter;
            if (factory->EnumAdapters1(adapter_index, &adapter) == DXGI_ERROR_NOT_FOUND) break;
            ComPtr<ID3D11Device> adapter_device;
            ComPtr<ID3D11DeviceContext> adapter_context;
            for (UINT output_index = 0;; ++output_index) {
                ComPtr<IDXGIOutput> output;
                if (adapter->EnumOutputs(output_index, &output) == DXGI_ERROR_NOT_FOUND) break;
                DXGI_OUTPUT_DESC description{};
                ThrowIfFailed(output->GetDesc(&description), "Get output description");
                if (!description.AttachedToDesktop) continue;
                if (monitor != L"all" && _wcsicmp(monitor.c_str(), description.DeviceName) != 0) continue;
                try {
                    if (!adapter_device) {
                        ThrowIfFailed(
                            D3D11CreateDevice(
                                adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                                D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
                                nullptr, 0, D3D11_SDK_VERSION,
                                &adapter_device, nullptr, &adapter_context),
                            "create desktop duplication device");
                    }
                    outputs_.push_back(std::make_unique<OutputCapture>(
                        adapter_device, adapter_context, output));
                    if (!gpu_device_) {
                        gpu_device_ = adapter_device;
                        gpu_context_ = adapter_context;
                        gpu_capture_available_ = outputs_.back()->rotation_identity();
                    } else if (gpu_device_.Get() != adapter_device.Get() ||
                               !outputs_.back()->rotation_identity()) {
                        gpu_capture_available_ = false;
                    }
                } catch (const std::exception& error) {
                    std::cerr << "capture output skipped: " << error.what() << std::endl;
                    continue;
                }
                union_rect.left = (std::min)(union_rect.left, description.DesktopCoordinates.left);
                union_rect.top = (std::min)(union_rect.top, description.DesktopCoordinates.top);
                union_rect.right = (std::max)(union_rect.right, description.DesktopCoordinates.right);
                union_rect.bottom = (std::max)(union_rect.bottom, description.DesktopCoordinates.bottom);
            }
        }
        if (outputs_.empty()) throw std::runtime_error("no capturable desktop output was found");
        if (gpu_capture_available_ && gpu_context_) {
            ComPtr<ID3D11Multithread> multithread;
            if (SUCCEEDED(gpu_context_.As(&multithread))) {
                multithread->SetMultithreadProtected(TRUE);
            }
        }
        bounds_ = union_rect;
        width_ = static_cast<UINT>(bounds_.right - bounds_.left);
        height_ = static_cast<UINT>(bounds_.bottom - bounds_.top);
        composed_.resize(static_cast<std::size_t>(width_) * height_ * 4);
        CaptureGdi(&composed_);
        if (gpu_capture_available_) {
            D3D11_TEXTURE2D_DESC texture{};
            texture.Width = width_;
            texture.Height = height_;
            texture.MipLevels = 1;
            texture.ArraySize = 1;
            texture.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
            texture.SampleDesc.Count = 1;
            texture.Usage = D3D11_USAGE_DEFAULT;
            texture.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
            D3D11_SUBRESOURCE_DATA initial{};
            initial.pSysMem = composed_.data();
            initial.SysMemPitch = width_ * 4;
            initial.SysMemSlicePitch = static_cast<UINT>(composed_.size());
            try {
                ThrowIfFailed(
                    gpu_device_->CreateTexture2D(&texture, &initial, &composed_texture_),
                    "create GPU desktop composition texture");
            } catch (const std::exception& error) {
                std::cerr << "capture_backend=dxgi_cpu_readback reason="
                          << error.what() << std::endl;
                gpu_capture_available_ = false;
                composed_texture_.Reset();
            }
        }
        bootstrap_pending_ = true;
        last_dxgi_frame_ = std::chrono::steady_clock::now();
    }

    UINT width() const { return width_; }
    UINT height() const { return height_; }
    const RECT& bounds() const { return bounds_; }
    const std::vector<std::uint8_t>& pixels() const { return composed_; }
    bool gpu_capture_available() const { return gpu_capture_available_; }
    ID3D11Device* gpu_device() const { return gpu_device_.Get(); }
    ID3D11Texture2D* gpu_texture() const { return composed_texture_.Get(); }
    void DisableGpuCapture() {
        gpu_capture_available_ = false;
        composed_texture_.Reset();
    }

    bool Acquire() {
        if (bootstrap_pending_) {
            bootstrap_pending_ = false;
            return true;
        }
        bool changed = false;
        for (std::size_t index = 0; index < outputs_.size(); ++index) {
            const bool output_changed = outputs_[index]->Acquire(
                index == 0 ? 2 : 0, !gpu_capture_available_);
            if (output_changed && gpu_capture_available_) {
                const DXGI_OUTPUT_DESC& description = outputs_[index]->description();
                outputs_[index]->CopyTo(
                    composed_texture_.Get(),
                    static_cast<UINT>(description.DesktopCoordinates.left - bounds_.left),
                    static_cast<UINT>(description.DesktopCoordinates.top - bounds_.top));
            }
            changed = output_changed || changed;
        }
        if (!changed) return false;
        last_dxgi_frame_ = std::chrono::steady_clock::now();
        if (gdi_fallback_active_) {
            gdi_fallback_active_ = false;
            std::cerr << "capture_backend=dxgi_d3d11" << std::endl;
        }
        if (gpu_capture_available_) return true;
        for (const auto& output : outputs_) {
            if (!output->initialized()) continue;
            const DXGI_OUTPUT_DESC& description = output->description();
            const UINT left = static_cast<UINT>(description.DesktopCoordinates.left - bounds_.left);
            const UINT top = static_cast<UINT>(description.DesktopCoordinates.top - bounds_.top);
            for (UINT row = 0; row < output->height(); ++row) {
                std::memcpy(
                    composed_.data() + (static_cast<std::size_t>(top + row) * width_ + left) * 4,
                    output->pixels().data() + static_cast<std::size_t>(row) * output->width() * 4,
                    static_cast<std::size_t>(output->width()) * 4);
            }
        }
        return true;
    }

    bool AcquireWithCompatibilityFallback() {
        if (Acquire()) return true;
        const auto now = std::chrono::steady_clock::now();
        if (now - last_dxgi_frame_ < std::chrono::milliseconds(250)) return false;
        std::vector<std::uint8_t> current;
        CaptureGdi(&current);
        if (!gdi_fallback_active_) {
            gdi_fallback_active_ = true;
            std::cerr << "capture_backend=gdi_compatibility reason=dxgi_no_frames" << std::endl;
        }
        if (current == composed_) return false;
        composed_.swap(current);
        if (gpu_capture_available_) {
            gpu_context_->UpdateSubresource(
                composed_texture_.Get(), 0, nullptr, composed_.data(), width_ * 4,
                static_cast<UINT>(composed_.size()));
        }
        return true;
    }

    bool gdi_fallback_active() const { return gdi_fallback_active_; }

    void ApplyTestPattern(const std::uint64_t frame) {
        const UINT block_width = (std::min)(width_, 320U);
        const UINT block_height = (std::min)(height_, 96U);
        for (UINT y = 0; y < block_height; ++y) {
            for (UINT x = 0; x < block_width; ++x) {
                auto* pixel = composed_.data() + (static_cast<std::size_t>(y) * width_ + x) * 4;
                pixel[0] = static_cast<std::uint8_t>((x + frame * 7) & 0xff);
                pixel[1] = static_cast<std::uint8_t>((y * 2 + frame * 13) & 0xff);
                pixel[2] = static_cast<std::uint8_t>((x + y + frame * 19) & 0xff);
                pixel[3] = 255;
            }
        }
        if (gpu_capture_available_) {
            D3D11_BOX box{0, 0, 0, block_width, block_height, 1};
            gpu_context_->UpdateSubresource(
                composed_texture_.Get(), 0, &box, composed_.data(), width_ * 4,
                static_cast<UINT>(composed_.size()));
        }
    }

private:
    void CaptureGdi(std::vector<std::uint8_t>* destination) {
        destination->resize(static_cast<std::size_t>(width_) * height_ * 4);
        HDC desktop = GetDC(nullptr);
        if (!desktop) throw std::runtime_error("initial desktop DC could not be opened");
        HDC memory = CreateCompatibleDC(desktop);
        if (!memory) {
            ReleaseDC(nullptr, desktop);
            throw std::runtime_error("initial desktop memory DC could not be created");
        }
        BITMAPINFO information{};
        information.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        information.bmiHeader.biWidth = static_cast<LONG>(width_);
        information.bmiHeader.biHeight = -static_cast<LONG>(height_);
        information.bmiHeader.biPlanes = 1;
        information.bmiHeader.biBitCount = 32;
        information.bmiHeader.biCompression = BI_RGB;
        void* pixels = nullptr;
        HBITMAP bitmap = CreateDIBSection(
            desktop, &information, DIB_RGB_COLORS, &pixels, nullptr, 0);
        if (!bitmap || !pixels) {
            DeleteDC(memory);
            ReleaseDC(nullptr, desktop);
            throw std::runtime_error("initial desktop bitmap could not be created");
        }
        HGDIOBJ previous = SelectObject(memory, bitmap);
        const BOOL copied = BitBlt(
            memory, 0, 0, static_cast<int>(width_), static_cast<int>(height_),
            desktop, bounds_.left, bounds_.top, SRCCOPY | CAPTUREBLT);
        if (copied) {
            std::memcpy(destination->data(), pixels, destination->size());
        }
        SelectObject(memory, previous);
        DeleteObject(bitmap);
        DeleteDC(memory);
        ReleaseDC(nullptr, desktop);
        if (!copied) throw std::runtime_error("initial desktop snapshot failed");
    }

    RECT bounds_{};
    UINT width_ = 0;
    UINT height_ = 0;
    std::vector<std::unique_ptr<OutputCapture>> outputs_;
    std::vector<std::uint8_t> composed_;
    bool gpu_capture_available_ = false;
    ComPtr<ID3D11Device> gpu_device_;
    ComPtr<ID3D11DeviceContext> gpu_context_;
    ComPtr<ID3D11Texture2D> composed_texture_;
    bool bootstrap_pending_ = false;
    bool gdi_fallback_active_ = false;
    std::chrono::steady_clock::time_point last_dxgi_frame_{};
};

struct CapturedFrame {
    std::vector<std::uint8_t> pixels;
    ComPtr<IMFSample> sample;
    std::uint64_t timestamp_us = 0;
};

class LatestFrameExchange {
public:
    std::vector<std::uint8_t> AcquireBuffer(const std::size_t size) {
        std::vector<std::uint8_t> result;
        {
            std::lock_guard<std::mutex> guard(lock_);
            if (!recycled_.empty()) {
                result = std::move(recycled_.back());
                recycled_.pop_back();
            }
        }
        result.resize(size);
        return result;
    }

    void Publish(CapturedFrame frame) {
        {
            std::lock_guard<std::mutex> guard(lock_);
            if (stopping_) return;
            if (available_) {
                RecycleLocked(std::move(latest_.pixels));
                ++dropped_;
            }
            latest_ = std::move(frame);
            available_ = true;
        }
        signal_.notify_one();
    }

    bool Wait(CapturedFrame* frame, const std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> guard(lock_);
        signal_.wait_for(guard, timeout, [&] { return stopping_ || available_; });
        if (!available_) return false;
        *frame = std::move(latest_);
        available_ = false;
        return true;
    }

    void Recycle(std::vector<std::uint8_t> pixels) {
        std::lock_guard<std::mutex> guard(lock_);
        RecycleLocked(std::move(pixels));
    }

    void Stop() {
        {
            std::lock_guard<std::mutex> guard(lock_);
            stopping_ = true;
        }
        signal_.notify_all();
    }

    std::uint64_t dropped() const { return dropped_; }

private:
    void RecycleLocked(std::vector<std::uint8_t> pixels) {
        if (pixels.empty() || recycled_.size() >= 3) return;
        recycled_.push_back(std::move(pixels));
    }

    mutable std::mutex lock_;
    std::condition_variable signal_;
    CapturedFrame latest_;
    std::vector<std::vector<std::uint8_t>> recycled_;
    bool available_ = false;
    bool stopping_ = false;
    std::atomic<std::uint64_t> dropped_{0};
};

struct EncodedSize {
    UINT width;
    UINT height;
};

EncodedSize SelectEncodedSize(const UINT source_width, const UINT source_height, const UINT fps) {
    const UINT max_width = fps >= 120 ? 1280 : 1920;
    const UINT max_height = fps >= 120 ? 720 : 1080;
    const double scale = (std::min)(
        1.0,
        (std::min)(
            static_cast<double>(max_width) / source_width,
            static_cast<double>(max_height) / source_height));
    UINT width = static_cast<UINT>(source_width * scale) & ~1U;
    UINT height = static_cast<UINT>(source_height * scale) & ~1U;
    return {(std::max)(2U, width), (std::max)(2U, height)};
}

inline std::uint8_t ClampByte(const int value) {
    return static_cast<std::uint8_t>((std::min)(255, (std::max)(0, value)));
}

void BgraToNv12(
    const std::vector<std::uint8_t>& source, const UINT source_width, const UINT source_height,
    std::vector<std::uint8_t>* destination, const UINT target_width, const UINT target_height) {
    destination->resize(static_cast<std::size_t>(target_width) * target_height * 3 / 2);
    auto* y_plane = destination->data();
    auto* uv_plane = y_plane + static_cast<std::size_t>(target_width) * target_height;
    for (UINT y = 0; y < target_height; ++y) {
        const UINT source_y = static_cast<UINT>(static_cast<std::uint64_t>(y) * source_height / target_height);
        for (UINT x = 0; x < target_width; ++x) {
            const UINT source_x = static_cast<UINT>(static_cast<std::uint64_t>(x) * source_width / target_width);
            const auto* pixel = source.data() +
                (static_cast<std::size_t>(source_y) * source_width + source_x) * 4;
            const int b = pixel[0];
            const int g = pixel[1];
            const int r = pixel[2];
            y_plane[static_cast<std::size_t>(y) * target_width + x] =
                ClampByte(((66 * r + 129 * g + 25 * b + 128) >> 8) + 16);
        }
    }
    for (UINT y = 0; y < target_height; y += 2) {
        for (UINT x = 0; x < target_width; x += 2) {
            int u_sum = 0;
            int v_sum = 0;
            for (UINT dy = 0; dy < 2; ++dy) {
                for (UINT dx = 0; dx < 2; ++dx) {
                    const UINT source_x = static_cast<UINT>(
                        static_cast<std::uint64_t>(x + dx) * source_width / target_width);
                    const UINT source_y = static_cast<UINT>(
                        static_cast<std::uint64_t>(y + dy) * source_height / target_height);
                    const auto* pixel = source.data() +
                        (static_cast<std::size_t>(source_y) * source_width + source_x) * 4;
                    const int b = pixel[0];
                    const int g = pixel[1];
                    const int r = pixel[2];
                    u_sum += ((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128;
                    v_sum += ((112 * r - 94 * g - 18 * b + 128) >> 8) + 128;
                }
            }
            const std::size_t offset = static_cast<std::size_t>(y / 2) * target_width + x;
            uv_plane[offset] = ClampByte(u_sum / 4);
            uv_plane[offset + 1] = ClampByte(v_sum / 4);
        }
    }
}

class ProtocolWriter {
public:
    explicit ProtocolWriter(const HANDLE output) : output_(output), worker_(&ProtocolWriter::Run, this) {}
    ~ProtocolWriter() { Stop(); }

    bool Enqueue(Message message) {
        std::lock_guard<std::mutex> guard(lock_);
        if (stopping_ || failed_) return false;
        if (message.type == MessageType::VideoAccessUnit) {
            auto video_depth = [&] {
                return static_cast<std::size_t>(std::count_if(
                    queue_.begin(), queue_.end(), [](const Message& queued) {
                        return queued.type == MessageType::VideoAccessUnit;
                    }));
            };
            while (video_depth() >= 2) {
                auto candidate = std::find_if(queue_.begin(), queue_.end(), [](const Message& queued) {
                    return queued.type == MessageType::VideoAccessUnit &&
                        !(queued.flags & lanremote::video::Keyframe);
                });
                if (candidate == queue_.end()) {
                    candidate = std::find_if(
                        queue_.begin(), queue_.end(), [](const Message& queued) {
                            return queued.type == MessageType::VideoAccessUnit;
                        });
                    if (candidate == queue_.end()) break;
                    queue_.erase(candidate);
                } else {
                    queue_.erase(candidate);
                }
                ++dropped_;
            }
        }
        queue_.push_back(std::move(message));
        signal_.notify_one();
        return true;
    }

    void Stop() {
        {
            std::lock_guard<std::mutex> guard(lock_);
            if (stopping_) return;
            stopping_ = true;
        }
        signal_.notify_all();
        if (worker_.joinable()) worker_.join();
    }

    bool failed() const { return failed_; }
    std::uint64_t dropped() const { return dropped_; }
    std::uint64_t sent() const { return sent_; }
    std::uint64_t sent_bytes() const { return sent_bytes_; }
    std::size_t depth() const {
        std::lock_guard<std::mutex> guard(lock_);
        return queue_.size();
    }

private:
    void Run() {
        while (true) {
            Message message;
            {
                std::unique_lock<std::mutex> guard(lock_);
                signal_.wait(guard, [&] { return stopping_ || !queue_.empty(); });
                if (queue_.empty()) {
                    if (stopping_) return;
                    continue;
                }
                message = std::move(queue_.front());
                queue_.pop_front();
            }
            try {
                if (!lanremote::video::WriteMessage(output_, message)) {
                    failed_ = true;
                    return;
                }
                if (message.type == MessageType::VideoAccessUnit) {
                    ++sent_;
                    sent_bytes_ += message.payload.size();
                }
            } catch (...) {
                failed_ = true;
                return;
            }
        }
    }

    HANDLE output_;
    mutable std::mutex lock_;
    std::condition_variable signal_;
    std::deque<Message> queue_;
    std::thread worker_;
    bool stopping_ = false;
    std::atomic<bool> failed_{false};
    std::atomic<std::uint64_t> dropped_{0};
    std::atomic<std::uint64_t> sent_{0};
    std::atomic<std::uint64_t> sent_bytes_{0};
};

bool SetCodecBoolean(ICodecAPI* codec, const GUID& property, const bool value) {
    VARIANT setting;
    VariantInit(&setting);
    setting.vt = VT_BOOL;
    setting.boolVal = value ? VARIANT_TRUE : VARIANT_FALSE;
    return SUCCEEDED(codec->SetValue(&property, &setting));
}

bool SetCodecUInt32(ICodecAPI* codec, const GUID& property, const UINT32 value) {
    VARIANT setting;
    VariantInit(&setting);
    setting.vt = VT_UI4;
    setting.ulVal = value;
    return SUCCEEDED(codec->SetValue(&property, &setting));
}

std::vector<std::uint8_t> SampleBytes(IMFSample* sample) {
    ComPtr<IMFMediaBuffer> buffer;
    ThrowIfFailed(sample->ConvertToContiguousBuffer(&buffer), "Convert encoded sample buffer");
    BYTE* data = nullptr;
    DWORD length = 0;
    ThrowIfFailed(buffer->Lock(&data, nullptr, &length), "Lock encoded sample buffer");
    std::vector<std::uint8_t> result(data, data + length);
    buffer->Unlock();
    return result;
}

bool HasAnnexBStartCode(const std::vector<std::uint8_t>& bytes) {
    return bytes.size() >= 4 && bytes[0] == 0 && bytes[1] == 0 &&
        (bytes[2] == 1 || (bytes[2] == 0 && bytes[3] == 1));
}

std::vector<std::uint8_t> LengthPrefixedToAnnexB(const std::vector<std::uint8_t>& input) {
    if (HasAnnexBStartCode(input)) return input;
    std::vector<std::uint8_t> output;
    std::size_t offset = 0;
    while (offset + 4 <= input.size()) {
        const std::uint32_t length =
            (static_cast<std::uint32_t>(input[offset]) << 24) |
            (static_cast<std::uint32_t>(input[offset + 1]) << 16) |
            (static_cast<std::uint32_t>(input[offset + 2]) << 8) |
            input[offset + 3];
        offset += 4;
        if (length == 0 || length > input.size() - offset) return input;
        output.insert(output.end(), {0, 0, 0, 1});
        output.insert(output.end(), input.begin() + offset, input.begin() + offset + length);
        offset += length;
    }
    return offset == input.size() ? output : input;
}

bool ContainsNalType(const std::vector<std::uint8_t>& bytes, const std::uint8_t type) {
    for (std::size_t index = 0; index + 4 < bytes.size(); ++index) {
        std::size_t nal = std::string::npos;
        if (bytes[index] == 0 && bytes[index + 1] == 0 && bytes[index + 2] == 1) nal = index + 3;
        if (index + 4 < bytes.size() && bytes[index] == 0 && bytes[index + 1] == 0 &&
            bytes[index + 2] == 0 && bytes[index + 3] == 1) nal = index + 4;
        if (nal != std::string::npos && nal < bytes.size() && (bytes[nal] & 0x1f) == type) return true;
    }
    return false;
}

struct EncodedAccessUnit {
    std::vector<std::uint8_t> bytes;
    std::uint64_t timestamp_us = 0;
};

class GpuFrameConverter {
public:
    GpuFrameConverter(
        const UINT source_width, const UINT source_height,
        const UINT target_width, const UINT target_height,
        ID3D11Device* shared_device = nullptr,
        ID3D11Texture2D* shared_source_texture = nullptr)
        : source_width_(source_width), source_height_(source_height),
          target_width_(target_width), target_height_(target_height) {
        if (shared_device) {
            device_ = shared_device;
            shared_device->GetImmediateContext(&context_);
        } else {
            ThrowIfFailed(
                D3D11CreateDevice(
                    nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
                    D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
                    nullptr, 0, D3D11_SDK_VERSION, &device_, nullptr, &context_),
                "create D3D11 color conversion device");
        }
        ThrowIfFailed(device_.As(&video_device_), "query D3D11 video conversion device");
        ThrowIfFailed(context_.As(&video_context_), "query D3D11 video conversion context");

        if (shared_source_texture) {
            source_texture_ = shared_source_texture;
        } else {
            D3D11_TEXTURE2D_DESC source{};
            source.Width = source_width_;
            source.Height = source_height_;
            source.MipLevels = 1;
            source.ArraySize = 1;
            source.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
            source.SampleDesc.Count = 1;
            source.Usage = D3D11_USAGE_DEFAULT;
            source.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
            ThrowIfFailed(device_->CreateTexture2D(
                &source, nullptr, &source_texture_), "create BGRA upload texture");
        }

        D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
        content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        content.InputFrameRate = {120, 1};
        content.InputWidth = source_width_;
        content.InputHeight = source_height_;
        content.OutputFrameRate = {120, 1};
        content.OutputWidth = target_width_;
        content.OutputHeight = target_height_;
        content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
        ThrowIfFailed(
            video_device_->CreateVideoProcessorEnumerator(&content, &enumerator_),
            "create BGRA to NV12 processor enumerator");
        ThrowIfFailed(video_device_->CreateVideoProcessor(enumerator_.Get(), 0, &processor_),
            "create BGRA to NV12 processor");
        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
        input_description.FourCC = 0;
        input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        ThrowIfFailed(
            video_device_->CreateVideoProcessorInputView(
                source_texture_.Get(), enumerator_.Get(), &input_description, &input_view_),
            "create BGRA video processor input view");
        D3D11_TEXTURE2D_DESC target{};
        target.Width = target_width_;
        target.Height = target_height_;
        target.MipLevels = 1;
        target.ArraySize = 1;
        target.Format = DXGI_FORMAT_NV12;
        target.SampleDesc.Count = 1;
        target.Usage = D3D11_USAGE_DEFAULT;
        target.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_VIDEO_ENCODER;
        for (std::size_t index = 0; index < 4; ++index) {
            ComPtr<ID3D11Texture2D> texture;
            ThrowIfFailed(device_->CreateTexture2D(
                &target, nullptr, &texture), "create NV12 encoder texture");
            D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_description{};
            output_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
            ComPtr<ID3D11VideoProcessorOutputView> output_view;
            ThrowIfFailed(
                video_device_->CreateVideoProcessorOutputView(
                    texture.Get(), enumerator_.Get(), &output_description, &output_view),
                "create NV12 video processor output view");
            target_textures_.push_back(std::move(texture));
            output_views_.push_back(std::move(output_view));
        }
    }

    ID3D11Device* device() const { return device_.Get(); }

    ComPtr<IMFSample> Convert(const std::vector<std::uint8_t>& bgra) {
        const std::size_t expected = static_cast<std::size_t>(source_width_) * source_height_ * 4;
        if (bgra.size() != expected) throw std::runtime_error("BGRA desktop frame size is invalid");
        context_->UpdateSubresource(
            source_texture_.Get(), 0, nullptr, bgra.data(), source_width_ * 4,
            static_cast<UINT>(bgra.size()));
        return ConvertTexture();
    }

    ComPtr<IMFSample> ConvertTexture() {
        const std::size_t target_index = next_target_++ % target_textures_.size();
        ID3D11Texture2D* target_texture = target_textures_[target_index].Get();
        RECT source{0, 0, static_cast<LONG>(source_width_), static_cast<LONG>(source_height_)};
        RECT target{0, 0, static_cast<LONG>(target_width_), static_cast<LONG>(target_height_)};
        video_context_->VideoProcessorSetStreamSourceRect(processor_.Get(), 0, TRUE, &source);
        video_context_->VideoProcessorSetStreamDestRect(processor_.Get(), 0, TRUE, &target);
        video_context_->VideoProcessorSetStreamFrameFormat(
            processor_.Get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.pInputSurface = input_view_.Get();
        ThrowIfFailed(
            video_context_->VideoProcessorBlt(
                processor_.Get(), output_views_[target_index].Get(), 0, 1, &stream),
            "convert BGRA desktop frame to NV12");
        ComPtr<IMFMediaBuffer> buffer;
        ThrowIfFailed(
            MFCreateDXGISurfaceBuffer(
                __uuidof(ID3D11Texture2D), target_texture, 0, FALSE, &buffer),
            "wrap NV12 D3D11 texture");
        ComPtr<IMFSample> sample;
        ThrowIfFailed(MFCreateSample(&sample), "create D3D11 encoder sample");
        ThrowIfFailed(sample->AddBuffer(buffer.Get()), "attach NV12 D3D11 texture");
        return sample;
    }

private:
    UINT source_width_;
    UINT source_height_;
    UINT target_width_;
    UINT target_height_;
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    ComPtr<ID3D11VideoDevice> video_device_;
    ComPtr<ID3D11VideoContext> video_context_;
    ComPtr<ID3D11Texture2D> source_texture_;
    std::vector<ComPtr<ID3D11Texture2D>> target_textures_;
    ComPtr<ID3D11VideoProcessorEnumerator> enumerator_;
    ComPtr<ID3D11VideoProcessor> processor_;
    ComPtr<ID3D11VideoProcessorInputView> input_view_;
    std::vector<ComPtr<ID3D11VideoProcessorOutputView>> output_views_;
    std::size_t next_target_ = 0;
};

class MfH264Encoder {
public:
    MfH264Encoder(
        const UINT width, const UINT height, const UINT fps, const UINT32 bitrate,
        ID3D11Device* d3d_device, const bool force_software = false)
        : width_(width), height_(height), fps_(fps), bitrate_(bitrate) {
        if (!force_software &&
            CreateTransform(MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER)) {
            // Preferred path selected.
        } else {
            if (!CreateTransform(MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER)) {
                throw std::runtime_error("no Media Foundation H.264 encoder is available");
            }
        }
        ConfigureD3D(d3d_device);
        // Several encoder MFTs latch structural properties while media types
        // are negotiated. Apply the real-time/zero-reorder contract both before
        // and after type configuration so the software fallback cannot build a
        // multi-frame look-ahead queue.
        ConfigureCodec();
        ConfigureTypes();
        ConfigureCodec();
        transform_->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
        ThrowIfFailed(transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0), "encoder begin streaming");
        ThrowIfFailed(transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0), "encoder start stream");
        if (asynchronous_) WaitForNeedInput(2000);
    }

    ~MfH264Encoder() {
        if (transform_) {
            transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
            transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
        }
    }

    const std::string& name() const { return name_; }
    bool hardware() const { return hardware_; }
    bool SetBitrate(const UINT32 bitrate) {
        if (!codec_ || !SetCodecUInt32(codec_.Get(), CODECAPI_AVEncCommonMeanBitRate, bitrate)) {
            return false;
        }
        bitrate_ = bitrate;
        return true;
    }

    std::vector<EncodedAccessUnit> Encode(
        const std::vector<std::uint8_t>& nv12, const LONGLONG time_100ns,
        const bool force_keyframe) {
        ComPtr<IMFMediaBuffer> buffer;
        ThrowIfFailed(MFCreateMemoryBuffer(static_cast<DWORD>(nv12.size()), &buffer), "create input buffer");
        BYTE* bytes = nullptr;
        ThrowIfFailed(buffer->Lock(&bytes, nullptr, nullptr), "lock input buffer");
        std::memcpy(bytes, nv12.data(), nv12.size());
        buffer->Unlock();
        ThrowIfFailed(buffer->SetCurrentLength(static_cast<DWORD>(nv12.size())), "set input length");
        ComPtr<IMFSample> sample;
        ThrowIfFailed(MFCreateSample(&sample), "create input sample");
        ThrowIfFailed(sample->AddBuffer(buffer.Get()), "attach input buffer");
        return EncodeSample(sample.Get(), time_100ns, force_keyframe);
    }

    std::vector<EncodedAccessUnit> EncodeD3D11(
        IMFSample* sample, const LONGLONG time_100ns, const bool force_keyframe) {
        return EncodeSample(sample, time_100ns, force_keyframe);
    }

    std::vector<EncodedAccessUnit> Poll() {
        return asynchronous_ ? DrainAsync(2) : std::vector<EncodedAccessUnit>{};
    }

private:
    std::vector<EncodedAccessUnit> EncodeSample(
        IMFSample* sample, const LONGLONG time_100ns, const bool force_keyframe) {
        if (force_keyframe && codec_) {
            SetCodecBoolean(codec_.Get(), CODECAPI_AVEncVideoForceKeyFrame, true);
        }
        sample->SetSampleTime(time_100ns);
        sample->SetSampleDuration(10'000'000LL / fps_);

        if (asynchronous_) {
            if (need_input_requests_ == 0) WaitForNeedInput(1000);
            ThrowIfFailed(transform_->ProcessInput(input_stream_, sample, 0), "encoder ProcessInput");
            --need_input_requests_;
            input_timestamps_us_.push_back(static_cast<std::uint64_t>(time_100ns / 10));
            return DrainAsync(1000);
        }
        ThrowIfFailed(transform_->ProcessInput(input_stream_, sample, 0), "encoder ProcessInput");
        input_timestamps_us_.push_back(static_cast<std::uint64_t>(time_100ns / 10));
        return DrainSync();
    }
    bool CreateTransform(const UINT32 flags) {
        MFT_REGISTER_TYPE_INFO input{MFMediaType_Video, MFVideoFormat_NV12};
        MFT_REGISTER_TYPE_INFO output{MFMediaType_Video, MFVideoFormat_H264};
        IMFActivate** activations = nullptr;
        UINT32 count = 0;
        const HRESULT enumeration = MFTEnumEx(
            MFT_CATEGORY_VIDEO_ENCODER, flags, &input, &output, &activations, &count);
        if (FAILED(enumeration) || count == 0) {
            if (activations) CoTaskMemFree(activations);
            return false;
        }
        ComPtr<IMFActivate> selected;
        for (UINT32 index = 0; index < count && !selected; ++index) {
            ComPtr<IMFTransform> candidate;
            if (SUCCEEDED(activations[index]->ActivateObject(IID_PPV_ARGS(&candidate)))) {
                selected = activations[index];
                transform_ = candidate;
            }
        }
        for (UINT32 index = 0; index < count; ++index) activations[index]->Release();
        CoTaskMemFree(activations);
        if (!selected || !transform_) return false;
        hardware_ = (flags & MFT_ENUM_FLAG_HARDWARE) != 0;
        WCHAR* friendly_name = nullptr;
        UINT32 name_length = 0;
        if (SUCCEEDED(selected->GetAllocatedString(
                MFT_FRIENDLY_NAME_Attribute, &friendly_name, &name_length))) {
            name_ = WideToUtf8(std::wstring(friendly_name, name_length));
            CoTaskMemFree(friendly_name);
        }
        if (name_.empty()) name_ = hardware_ ? "Media Foundation hardware H.264 encoder" : "Media Foundation H.264 encoder";
        DWORD input_count = 0;
        DWORD output_count = 0;
        ThrowIfFailed(transform_->GetStreamCount(&input_count, &output_count), "encoder stream count");
        input_stream_ = 0;
        output_stream_ = 0;
        if (input_count == 1 && output_count == 1) {
            transform_->GetStreamIDs(1, &input_stream_, 1, &output_stream_);
        }
        ComPtr<IMFAttributes> attributes;
        if (SUCCEEDED(transform_->GetAttributes(&attributes))) {
            UINT32 async_value = 0;
            if (SUCCEEDED(attributes->GetUINT32(MF_TRANSFORM_ASYNC, &async_value)) && async_value) {
                asynchronous_ = true;
                ThrowIfFailed(attributes->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE), "unlock async encoder");
                ThrowIfFailed(transform_.As(&event_generator_), "query async encoder events");
            }
        }
        return true;
    }

    void ConfigureD3D(ID3D11Device* device) {
        if (!hardware_ || !device) return;
        UINT reset_token = 0;
        if (FAILED(MFCreateDXGIDeviceManager(&reset_token, &device_manager_))) return;
        if (FAILED(device_manager_->ResetDevice(device, reset_token))) {
            device_manager_.Reset();
            return;
        }
        transform_->ProcessMessage(
            MFT_MESSAGE_SET_D3D_MANAGER,
            reinterpret_cast<ULONG_PTR>(device_manager_.Get()));
    }

    void ConfigureTypes() {
        ComPtr<IMFMediaType> output_type;
        ThrowIfFailed(MFCreateMediaType(&output_type), "create H.264 output type");
        ThrowIfFailed(output_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video), "set output major type");
        ThrowIfFailed(output_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264), "set H.264 subtype");
        ThrowIfFailed(MFSetAttributeSize(output_type.Get(), MF_MT_FRAME_SIZE, width_, height_), "set output size");
        ThrowIfFailed(MFSetAttributeRatio(output_type.Get(), MF_MT_FRAME_RATE, fps_, 1), "set output rate");
        ThrowIfFailed(MFSetAttributeRatio(output_type.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1), "set output aspect");
        output_type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        output_type->SetUINT32(MF_MT_AVG_BITRATE, bitrate_);
        output_type->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_Main);
        ThrowIfFailed(transform_->SetOutputType(output_stream_, output_type.Get(), 0), "set H.264 output type");

        ComPtr<IMFMediaType> input_type;
        ThrowIfFailed(MFCreateMediaType(&input_type), "create NV12 input type");
        ThrowIfFailed(input_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video), "set input major type");
        ThrowIfFailed(input_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12), "set NV12 subtype");
        ThrowIfFailed(MFSetAttributeSize(input_type.Get(), MF_MT_FRAME_SIZE, width_, height_), "set input size");
        ThrowIfFailed(MFSetAttributeRatio(input_type.Get(), MF_MT_FRAME_RATE, fps_, 1), "set input rate");
        ThrowIfFailed(MFSetAttributeRatio(input_type.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1), "set input aspect");
        input_type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        ThrowIfFailed(transform_->SetInputType(input_stream_, input_type.Get(), 0), "set NV12 input type");
    }

    void ConfigureCodec() {
        if (FAILED(transform_.As(&codec_))) return;
        SetCodecBoolean(codec_.Get(), CODECAPI_AVLowLatencyMode, true);
        SetCodecBoolean(codec_.Get(), CODECAPI_AVEncCommonRealTime, true);
        SetCodecUInt32(codec_.Get(), CODECAPI_AVEncMPVGOPSize, fps_);
        SetCodecUInt32(codec_.Get(), CODECAPI_AVEncMPVDefaultBPictureCount, 0);
        SetCodecUInt32(codec_.Get(), CODECAPI_AVEncCommonMeanBitRate, bitrate_);
    }

    std::vector<std::uint8_t> DrainOne(HRESULT* status, std::uint64_t* timestamp_us) {
        MFT_OUTPUT_STREAM_INFO info{};
        ThrowIfFailed(transform_->GetOutputStreamInfo(output_stream_, &info), "encoder output stream info");
        ComPtr<IMFSample> provided_sample;
        if (!(info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES)) {
            if (!reusable_output_sample_) {
                const DWORD capacity = (std::max)(
                    info.cbSize, static_cast<DWORD>(4U * 1024U * 1024U));
                ThrowIfFailed(MFCreateMemoryBuffer(
                    capacity, &reusable_output_buffer_), "create encoder output buffer");
                ThrowIfFailed(MFCreateSample(
                    &reusable_output_sample_), "create encoder output sample");
                ThrowIfFailed(reusable_output_sample_->AddBuffer(
                    reusable_output_buffer_.Get()), "attach encoder output buffer");
            }
            reusable_output_buffer_->SetCurrentLength(0);
            provided_sample = reusable_output_sample_;
        }
        MFT_OUTPUT_DATA_BUFFER output{};
        output.dwStreamID = output_stream_;
        output.pSample = provided_sample.Get();
        DWORD process_status = 0;
        *status = transform_->ProcessOutput(0, 1, &output, &process_status);
        ComPtr<IMFSample> returned;
        if (!provided_sample && output.pSample) returned.Attach(output.pSample);
        if (output.pEvents) output.pEvents->Release();
        if (FAILED(*status)) return {};
        IMFSample* sample = returned ? returned.Get() : provided_sample.Get();
        LONGLONG sample_time = 0;
        if (sample && SUCCEEDED(sample->GetSampleTime(&sample_time)) && sample_time >= 0) {
            *timestamp_us = static_cast<std::uint64_t>(sample_time / 10);
        }
        return sample ? LengthPrefixedToAnnexB(SampleBytes(sample)) : std::vector<std::uint8_t>{};
    }

    std::vector<EncodedAccessUnit> DrainSync() {
        std::vector<EncodedAccessUnit> outputs;
        while (true) {
            HRESULT status = S_OK;
            std::uint64_t timestamp_us = 0;
            auto bytes = DrainOne(&status, &timestamp_us);
            if (status == MF_E_TRANSFORM_NEED_MORE_INPUT) break;
            if (status == MF_E_TRANSFORM_STREAM_CHANGE) {
                ConfigureTypes();
                continue;
            }
            ThrowIfFailed(status, "encoder ProcessOutput");
            if (!bytes.empty()) outputs.push_back({std::move(bytes), ResolveOutputTimestamp(timestamp_us)});
        }
        return outputs;
    }

    bool PollEvent(MediaEventType* type) {
        ComPtr<IMFMediaEvent> event;
        const HRESULT status = event_generator_->GetEvent(MF_EVENT_FLAG_NO_WAIT, &event);
        if (status == MF_E_NO_EVENTS_AVAILABLE) return false;
        ThrowIfFailed(status, "read async encoder event");
        ThrowIfFailed(event->GetType(type), "read encoder event type");
        HRESULT event_status = S_OK;
        event->GetStatus(&event_status);
        ThrowIfFailed(event_status, "async encoder event");
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
        throw std::runtime_error("timed out waiting for the H.264 encoder to accept input");
    }

    std::vector<EncodedAccessUnit> DrainAsync(const DWORD timeout_ms) {
        std::vector<EncodedAccessUnit> outputs;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            MediaEventType type = MEUnknown;
            bool saw_event = false;
            while (PollEvent(&type)) {
                saw_event = true;
                if (type == METransformNeedInput) ++need_input_requests_;
                if (type == METransformHaveOutput) have_output_ = true;
            }
            while (have_output_) {
                HRESULT status = S_OK;
                std::uint64_t timestamp_us = 0;
                auto bytes = DrainOne(&status, &timestamp_us);
                have_output_ = false;
                if (status == MF_E_TRANSFORM_STREAM_CHANGE) {
                    ConfigureTypes();
                } else if (status != MF_E_TRANSFORM_NEED_MORE_INPUT) {
                    ThrowIfFailed(status, "async encoder ProcessOutput");
                    if (!bytes.empty()) {
                        outputs.push_back({std::move(bytes), ResolveOutputTimestamp(timestamp_us)});
                    }
                }
            }
            if (!outputs.empty() || need_input_requests_ > 0) return outputs;
            if (!saw_event) Sleep(1);
        }
        return outputs;
    }

    std::uint64_t ResolveOutputTimestamp(const std::uint64_t transform_timestamp_us) {
        // The inbox software encoder may normalize large absolute sample times.
        // Low-latency H.264 is configured with B=0, so preserve the capture
        // timestamp through an explicit FIFO instead of trusting MFT rewriting.
        if (input_timestamps_us_.empty()) return transform_timestamp_us;
        const std::uint64_t capture_timestamp_us = input_timestamps_us_.front();
        input_timestamps_us_.pop_front();
        return capture_timestamp_us;
    }

    UINT width_;
    UINT height_;
    UINT fps_;
    UINT32 bitrate_;
    DWORD input_stream_ = 0;
    DWORD output_stream_ = 0;
    bool hardware_ = false;
    bool asynchronous_ = false;
    UINT need_input_requests_ = 0;
    bool have_output_ = false;
    std::string name_;
    ComPtr<IMFTransform> transform_;
    ComPtr<IMFMediaEventGenerator> event_generator_;
    ComPtr<ICodecAPI> codec_;
    ComPtr<IMFDXGIDeviceManager> device_manager_;
    ComPtr<IMFMediaBuffer> reusable_output_buffer_;
    ComPtr<IMFSample> reusable_output_sample_;
    std::deque<std::uint64_t> input_timestamps_us_;
};

UINT32 BitrateForFps(const UINT fps) {
    if (fps >= 120) return kDefaultBitrate120;
    if (fps >= 60) return kDefaultBitrate60;
    return kDefaultBitrate30;
}

std::atomic<bool> force_keyframe{true};
std::atomic<UINT> adaptive_fps_limit{120};
std::atomic<UINT32> adaptive_bitrate{8'000'000};

void ReadControlMessages(const UINT configured_fps, const UINT32 configured_bitrate) {
    const HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    UINT receiver_capacity = configured_fps;
    UINT target_fps = configured_fps;
    UINT low_decode_reports = 0;
    Message message;
    std::string error;
    while (lanremote::video::ReadMessage(input, &message, &error)) {
        if (message.type == MessageType::RequestKeyframe) {
            force_keyframe = true;
        } else if (message.type == MessageType::ReceiverReport) {
            const std::string json(message.payload.begin(), message.payload.end());
            const double capacity = JsonNumber(json, "render_capacity_fps");
            const double decode_fps = JsonNumber(json, "decode_fps");
            if (capacity >= 24.0) {
                receiver_capacity = static_cast<UINT>((std::clamp)(
                    capacity, 24.0, static_cast<double>(configured_fps)));
                target_fps = (std::min)(target_fps, receiver_capacity);
            }
            // Render FPS may legitimately be below the selected ceiling when
            // the display refresh is lower. Decode FPS is the transport
            // backpressure signal; use wide hysteresis so ordinary sampling
            // jitter cannot ratchet the stream downward.
            if (decode_fps >= 24.0 && decode_fps < target_fps * 0.80) {
                if (++low_decode_reports >= 2) {
                    target_fps = static_cast<UINT>((std::clamp)(
                        decode_fps * 1.05, 24.0, static_cast<double>(receiver_capacity)));
                    low_decode_reports = 0;
                    adaptive_bitrate = (std::max)(
                        2'000'000U, adaptive_bitrate.load() * 4 / 5);
                }
            } else if (decode_fps >= target_fps * 0.95 && target_fps < receiver_capacity) {
                low_decode_reports = 0;
                target_fps = (std::min)(receiver_capacity, target_fps + 5);
                adaptive_bitrate = (std::min)(
                    configured_bitrate,
                    adaptive_bitrate.load() + configured_bitrate / 10);
            } else {
                low_decode_reports = 0;
            }
            adaptive_fps_limit = target_fps;
        }
    }
}

int Run(const Options& options) {
    const bool high_resolution_timer = timeBeginPeriod(1) == TIMERR_NOERROR;
    struct TimerGuard {
        bool active;
        ~TimerGuard() { if (active) timeEndPeriod(1); }
    } timer_guard{high_resolution_timer};
    ThrowIfFailed(CoInitializeEx(nullptr, COINIT_MULTITHREADED), "CoInitializeEx");
    struct CoGuard { ~CoGuard() { CoUninitialize(); } } co_guard;
    ThrowIfFailed(MFStartup(MF_VERSION, MFSTARTUP_FULL), "MFStartup");
    struct MfGuard { ~MfGuard() { MFShutdown(); } } mf_guard;

    DesktopCapture capture(options.monitor);
    const EncodedSize encoded = SelectEncodedSize(capture.width(), capture.height(), options.fps);
    const UINT32 bitrate = BitrateForFps(options.fps);
    std::unique_ptr<GpuFrameConverter> gpu_converter;
    try {
        gpu_converter = std::make_unique<GpuFrameConverter>(
            capture.width(), capture.height(), encoded.width, encoded.height,
            capture.gpu_capture_available() ? capture.gpu_device() : nullptr,
            capture.gpu_capture_available() ? capture.gpu_texture() : nullptr);
    } catch (const std::exception& error) {
        std::cerr << "color_conversion=cpu reason=" << error.what() << std::endl;
    }
    const bool force_software =
        GetEnvironmentVariableW(L"LAN_REMOTE_NATIVE_VIDEO_FORCE_SOFTWARE", nullptr, 0) > 0;
    std::unique_ptr<MfH264Encoder> encoder;
    double hardware_benchmark_fps = 0.0;
    double software_benchmark_fps = 0.0;
    std::string encoder_selection = force_software ? "forced_software" : "hardware_preferred";
    const auto create_encoder = [&](const bool software) {
        return std::make_unique<MfH264Encoder>(
            encoded.width, encoded.height, options.fps, bitrate,
            gpu_converter ? gpu_converter->device() : nullptr, software);
    };
    const auto benchmark_encoder = [&](MfH264Encoder* candidate) {
        constexpr int sample_count = 24;
        const auto started = std::chrono::steady_clock::now();
        for (int index = 0; index < sample_count; ++index) {
            if (gpu_converter) {
                ComPtr<IMFSample> sample = capture.gpu_capture_available()
                    ? gpu_converter->ConvertTexture()
                    : gpu_converter->Convert(capture.pixels());
                candidate->EncodeD3D11(
                    sample.Get(), static_cast<LONGLONG>(index) * 10'000'000LL /
                        options.fps, index == 0);
            }
        }
        const double seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - started).count();
        return seconds > 0.0 ? sample_count / seconds : 0.0;
    };
    if (force_software) {
        encoder = create_encoder(true);
    } else {
        encoder = create_encoder(false);
        if (options.fps >= 120 && encoder->hardware() && gpu_converter) {
            hardware_benchmark_fps = benchmark_encoder(encoder.get());
            if (hardware_benchmark_fps < 110.0) {
                try {
                    auto software = create_encoder(true);
                    software_benchmark_fps = benchmark_encoder(software.get());
                    if (software_benchmark_fps >= 90.0 &&
                        software_benchmark_fps > hardware_benchmark_fps * 1.05) {
                        encoder = std::move(software);
                        encoder_selection = "adaptive_software_for_120fps";
                    }
                } catch (const std::exception& error) {
                    std::cerr << "software_encoder_benchmark_failed=" << error.what() << std::endl;
                }
            }
            encoder = create_encoder(
                encoder_selection == "adaptive_software_for_120fps");
        }
    }
    force_keyframe = true;
    adaptive_fps_limit = options.fps;
    adaptive_bitrate = bitrate;
    ProtocolWriter writer(GetStdHandle(STD_OUTPUT_HANDLE));
    std::thread control_thread(ReadControlMessages, options.fps, bitrate);
    control_thread.detach();

    std::ostringstream config;
    config << "{\"codec\":\"h264_annexb\",\"encoded_width\":" << encoded.width
           << ",\"encoded_height\":" << encoded.height
           << ",\"coordinate_left\":" << capture.bounds().left
           << ",\"coordinate_top\":" << capture.bounds().top
           << ",\"coordinate_width\":" << capture.width()
           << ",\"coordinate_height\":" << capture.height()
           << ",\"fps_limit\":" << options.fps
           << ",\"bitrate\":" << bitrate
           << ",\"encoder\":\"" << JsonEscape(encoder->name()) << "\""
           << ",\"hardware\":" << (encoder->hardware() ? "true" : "false")
           << ",\"encoder_selection\":\"" << encoder_selection << "\""
           << ",\"hardware_benchmark_fps\":" << hardware_benchmark_fps
           << ",\"software_benchmark_fps\":" << software_benchmark_fps
           << ",\"pixel_format\":\"nv12\",\"color_conversion\":\""
           << (gpu_converter ? "d3d11_video_processor" : "cpu")
           << "\",\"capture\":\""
           << (capture.gpu_capture_available() && gpu_converter
               ? "dxgi_d3d11_gpu_texture" : "dxgi_cpu_readback")
           << "\",\"queue_capacity\":2}";
    const std::string config_text = config.str();
    Message config_message;
    config_message.type = MessageType::StreamConfig;
    config_message.generation = options.generation;
    config_message.timestamp_us = MonotonicMicroseconds();
    config_message.coordinate_width = static_cast<std::uint16_t>((std::min)(capture.width(), 65535U));
    config_message.coordinate_height = static_cast<std::uint16_t>((std::min)(capture.height(), 65535U));
    config_message.fps_limit = options.fps;
    config_message.payload.assign(config_text.begin(), config_text.end());
    writer.Enqueue(std::move(config_message));

    std::vector<std::uint8_t> nv12;
    std::uint64_t sequence = 0;
    std::uint64_t submitted_frames = 0;
    std::uint64_t output_age_microseconds = 0;
    std::uint64_t output_age_samples = 0;
    std::atomic<std::uint64_t> conversion_microseconds{0};
    std::uint64_t encode_microseconds = 0;
    auto next_diagnostic = std::chrono::steady_clock::now() + std::chrono::seconds(1);
    std::uint64_t last_diagnostic_submitted = 0;
    std::uint64_t last_diagnostic_encoded = 0;
    std::uint64_t last_diagnostic_sent = 0;
    std::uint64_t last_diagnostic_sent_bytes = 0;
    std::uint64_t last_diagnostic_writer_dropped = 0;
    UINT32 active_bitrate = bitrate;
    std::uint64_t last_diagnostic_captured = 0;
    std::uint64_t last_diagnostic_capture_microseconds = 0;
    std::atomic<bool> stop_capture{false};
    std::atomic<std::uint64_t> captured_frames{0};
    std::atomic<std::uint64_t> capture_microseconds{0};
    LatestFrameExchange frame_exchange;
    std::mutex capture_error_lock;
    std::exception_ptr capture_error;
    const auto emit_access_units = [&](std::vector<EncodedAccessUnit> access_units) {
        for (auto& access_unit : access_units) {
            const std::uint64_t now_us = MonotonicMicroseconds();
            if (access_unit.timestamp_us > 0 && now_us >= access_unit.timestamp_us) {
                output_age_microseconds += now_us - access_unit.timestamp_us;
                ++output_age_samples;
            }
            Message frame;
            frame.type = MessageType::VideoAccessUnit;
            frame.generation = options.generation;
            frame.sequence = ++sequence;
            frame.timestamp_us = access_unit.timestamp_us;
            frame.coordinate_width = config_message.coordinate_width;
            frame.coordinate_height = config_message.coordinate_height;
            frame.fps_limit = options.fps;
            const bool keyframe = ContainsNalType(access_unit.bytes, 5);
            const bool codec_config = ContainsNalType(access_unit.bytes, 7) ||
                ContainsNalType(access_unit.bytes, 8);
            if (keyframe) frame.flags |= lanremote::video::Keyframe;
            if (codec_config) frame.flags |= lanremote::video::CodecConfig;
            frame.payload = std::move(access_unit.bytes);
            writer.Enqueue(std::move(frame));
        }
    };
    const auto encode_frame = [&](const std::vector<std::uint8_t>& pixels,
                                  IMFSample* prepared_sample,
                                  const LONGLONG time_100ns,
                                  const bool request_keyframe) {
        if (prepared_sample) {
            const auto encode_started = std::chrono::steady_clock::now();
            auto result = encoder->EncodeD3D11(
                prepared_sample, time_100ns, request_keyframe);
            encode_microseconds += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - encode_started).count());
            return result;
        }
        const auto conversion_started = std::chrono::steady_clock::now();
        if (gpu_converter) {
            ComPtr<IMFSample> sample = capture.gpu_capture_available()
                ? gpu_converter->ConvertTexture()
                : gpu_converter->Convert(pixels);
            const auto conversion_finished = std::chrono::steady_clock::now();
            conversion_microseconds += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    conversion_finished - conversion_started).count());
            auto result = encoder->EncodeD3D11(sample.Get(), time_100ns, request_keyframe);
            encode_microseconds += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - conversion_finished).count());
            return result;
        }
        BgraToNv12(
            pixels, capture.width(), capture.height(), &nv12,
            encoded.width, encoded.height);
        const auto conversion_finished = std::chrono::steady_clock::now();
        conversion_microseconds += static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                conversion_finished - conversion_started).count());
        auto result = encoder->Encode(nv12, time_100ns, request_keyframe);
        encode_microseconds += static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - conversion_finished).count());
        return result;
    };
    const auto current_frame_interval = [] {
        return std::chrono::microseconds(
            1'000'000 / (std::max)(24U, adaptive_fps_limit.load()));
    };
    const bool threaded_capture = !capture.gpu_capture_available() || !encoder->hardware();
    std::thread capture_thread;
    if (threaded_capture) {
        capture_thread = std::thread([&] {
            try {
                auto next_frame = std::chrono::steady_clock::now();
                std::uint64_t test_pattern_frame = 0;
                while (!stop_capture && !writer.failed()) {
                    const auto now = std::chrono::steady_clock::now();
                    if (now < next_frame) std::this_thread::sleep_until(next_frame);
                    const auto frame_interval = current_frame_interval();
                    next_frame += frame_interval;
                    if (next_frame + frame_interval < std::chrono::steady_clock::now()) {
                        next_frame = std::chrono::steady_clock::now();
                    }
                    const auto capture_started = std::chrono::steady_clock::now();
                    const bool desktop_changed = options.test_pattern
                        ? capture.Acquire()
                        : capture.AcquireWithCompatibilityFallback();
                    if (options.test_pattern) capture.ApplyTestPattern(++test_pattern_frame);
                    if (!desktop_changed && !options.test_pattern) continue;
                    std::vector<std::uint8_t> pixels;
                    if (!capture.gpu_capture_available()) {
                        pixels = frame_exchange.AcquireBuffer(capture.pixels().size());
                        std::memcpy(pixels.data(), capture.pixels().data(), pixels.size());
                    }
                    const auto capture_finished = std::chrono::steady_clock::now();
                    capture_microseconds += static_cast<std::uint64_t>(
                        std::chrono::duration_cast<std::chrono::microseconds>(
                            capture_finished - capture_started).count());
                    ++captured_frames;
                    CapturedFrame frame;
                    frame.pixels = std::move(pixels);
                    if (!encoder->hardware() && gpu_converter &&
                        capture.gpu_capture_available()) {
                        const auto conversion_started = std::chrono::steady_clock::now();
                        frame.sample = gpu_converter->ConvertTexture();
                        conversion_microseconds += static_cast<std::uint64_t>(
                            std::chrono::duration_cast<std::chrono::microseconds>(
                                std::chrono::steady_clock::now() - conversion_started).count());
                    }
                    frame.timestamp_us = MonotonicMicroseconds();
                    frame_exchange.Publish(std::move(frame));
                }
            } catch (...) {
                {
                    std::lock_guard<std::mutex> guard(capture_error_lock);
                    capture_error = std::current_exception();
                }
                frame_exchange.Stop();
            }
        });
    }

    std::vector<std::uint8_t> last_pixels;
    ComPtr<IMFSample> last_sample;
    bool have_last_frame = false;
    std::exception_ptr run_error;
    auto gpu_next_frame = std::chrono::steady_clock::now();
    std::uint64_t gpu_test_pattern_frame = 0;
    try {
        while (!writer.failed()) {
            CapturedFrame frame;
            bool received_frame = false;
            if (!threaded_capture) {
                const auto now = std::chrono::steady_clock::now();
                if (now < gpu_next_frame) std::this_thread::sleep_until(gpu_next_frame);
                const auto frame_interval = current_frame_interval();
                gpu_next_frame += frame_interval;
                if (gpu_next_frame + frame_interval < std::chrono::steady_clock::now()) {
                    gpu_next_frame = std::chrono::steady_clock::now();
                }
                const auto capture_started = std::chrono::steady_clock::now();
                const bool desktop_changed = options.test_pattern
                    ? capture.Acquire()
                    : capture.AcquireWithCompatibilityFallback();
                if (options.test_pattern) capture.ApplyTestPattern(++gpu_test_pattern_frame);
                const auto capture_finished = std::chrono::steady_clock::now();
                if (desktop_changed || options.test_pattern) {
                    capture_microseconds += static_cast<std::uint64_t>(
                        std::chrono::duration_cast<std::chrono::microseconds>(
                            capture_finished - capture_started).count());
                    ++captured_frames;
                    frame.timestamp_us = MonotonicMicroseconds();
                    received_frame = true;
                }
            } else {
                received_frame = frame_exchange.Wait(&frame, std::chrono::milliseconds(10));
            }
            if (received_frame) {
                const bool request_keyframe = force_keyframe.exchange(false);
                ++submitted_frames;
                emit_access_units(encode_frame(
                    frame.pixels, frame.sample.Get(),
                    static_cast<LONGLONG>(frame.timestamp_us) * 10,
                    request_keyframe));
                if (!last_pixels.empty()) frame_exchange.Recycle(std::move(last_pixels));
                last_pixels = std::move(frame.pixels);
                last_sample = std::move(frame.sample);
                have_last_frame = true;
            } else {
                emit_access_units(encoder->Poll());
                if (force_keyframe.exchange(false) && have_last_frame) {
                    ++submitted_frames;
                    emit_access_units(encode_frame(
                        last_pixels, last_sample.Get(),
                        static_cast<LONGLONG>(MonotonicMicroseconds()) * 10, true));
                }
                std::lock_guard<std::mutex> guard(capture_error_lock);
                if (capture_error) std::rethrow_exception(capture_error);
            }

            const auto now = std::chrono::steady_clock::now();
            if (now >= next_diagnostic) {
                const std::uint64_t current_captured = captured_frames.load();
                const std::uint64_t current_capture_microseconds = capture_microseconds.load();
                const std::uint64_t diagnostic_captured = current_captured - last_diagnostic_captured;
                const std::uint64_t diagnostic_submitted = submitted_frames - last_diagnostic_submitted;
                const std::uint64_t diagnostic_encoded = sequence - last_diagnostic_encoded;
                const std::uint64_t current_sent = writer.sent();
                const std::uint64_t current_sent_bytes = writer.sent_bytes();
                const std::uint64_t current_writer_dropped = writer.dropped();
                if (current_writer_dropped > last_diagnostic_writer_dropped + 2 ||
                    writer.depth() >= 2) {
                    adaptive_bitrate = (std::max)(
                        2'000'000U, adaptive_bitrate.load() * 4 / 5);
                }
                const UINT32 requested_bitrate = adaptive_bitrate.load();
                if (requested_bitrate != active_bitrate && encoder->SetBitrate(requested_bitrate)) {
                    active_bitrate = requested_bitrate;
                }
                const std::uint64_t diagnostic_sent = current_sent - last_diagnostic_sent;
                const double send_mbps =
                    (current_sent_bytes - last_diagnostic_sent_bytes) * 8.0 / 1'000'000.0;
                const std::uint64_t diagnostic_conversion_microseconds =
                    conversion_microseconds.exchange(0);
                const double capture_ms = diagnostic_captured
                    ? (current_capture_microseconds - last_diagnostic_capture_microseconds) /
                        diagnostic_captured / 1000.0 : 0.0;
                const double conversion_ms = diagnostic_submitted
                    ? diagnostic_conversion_microseconds /
                        diagnostic_submitted / 1000.0 : 0.0;
                const double encoder_ms = diagnostic_submitted
                    ? encode_microseconds / diagnostic_submitted / 1000.0 : 0.0;
                const double output_age_ms = output_age_samples
                    ? output_age_microseconds / output_age_samples / 1000.0 : 0.0;
                std::cerr << "capture=" << current_captured
                          << " submitted=" << submitted_frames
                          << " encoded=" << sequence
                          << " capture_fps=" << diagnostic_captured
                          << " encode_fps=" << diagnostic_encoded
                          << " capture_dropped=" << frame_exchange.dropped()
                          << " queue_dropped=" << writer.dropped()
                          << " capture_ms=" << capture_ms
                          << " convert_ms=" << conversion_ms
                          << " encode_ms=" << encoder_ms
                          << " output_age_ms=" << output_age_ms
                          << " adaptive_fps=" << adaptive_fps_limit.load()
                          << std::endl;
                std::ostringstream report_json;
                report_json << "{\"capture_fps\":" << diagnostic_captured
                            << ",\"encode_fps\":" << diagnostic_encoded
                            << ",\"send_fps\":" << diagnostic_sent
                            << ",\"send_mbps\":" << send_mbps
                            << ",\"capture_ms\":" << capture_ms
                            << ",\"conversion_ms\":" << conversion_ms
                            << ",\"encode_ms\":" << encoder_ms
                            << ",\"output_age_ms\":" << output_age_ms
                            << ",\"adaptive_fps\":" << adaptive_fps_limit.load()
                            << ",\"bitrate\":" << active_bitrate
                            << ",\"capture_dropped\":" << frame_exchange.dropped()
                            << ",\"send_dropped\":" << writer.dropped()
                            << ",\"send_queue_depth\":" << writer.depth() << '}';
                const std::string report_text = report_json.str();
                Message report;
                report.type = MessageType::SenderReport;
                report.generation = options.generation;
                report.timestamp_us = MonotonicMicroseconds();
                report.coordinate_width = config_message.coordinate_width;
                report.coordinate_height = config_message.coordinate_height;
                report.fps_limit = options.fps;
                report.payload.assign(report_text.begin(), report_text.end());
                writer.Enqueue(std::move(report));
                last_diagnostic_captured = current_captured;
                last_diagnostic_submitted = submitted_frames;
                last_diagnostic_encoded = sequence;
                last_diagnostic_sent = current_sent;
                last_diagnostic_sent_bytes = current_sent_bytes;
                last_diagnostic_writer_dropped = current_writer_dropped;
                last_diagnostic_capture_microseconds = current_capture_microseconds;
                encode_microseconds = 0;
                output_age_microseconds = 0;
                output_age_samples = 0;
                next_diagnostic = now + std::chrono::seconds(1);
            }
        }
    } catch (...) {
        run_error = std::current_exception();
    }
    stop_capture = true;
    frame_exchange.Stop();
    if (capture_thread.joinable()) capture_thread.join();
    writer.Stop();
    if (run_error) std::rethrow_exception(run_error);
    return 0;
}

}  // namespace

int wmain(const int argc, wchar_t** argv) {
    try {
        return Run(ParseOptions(argc, argv));
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        Message message;
        message.type = MessageType::Error;
        const std::string text = error.what();
        message.payload.assign(text.begin(), text.end());
        try { lanremote::video::WriteMessage(GetStdHandle(STD_OUTPUT_HANDLE), message); } catch (...) {}
        return 1;
    }
}
