#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mftransform.h>
#include <wrl/client.h>

#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

std::string Utf8(const wchar_t* value) {
    if (value == nullptr) {
        return {};
    }
    const int size = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    if (size <= 1) {
        return {};
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), size, nullptr, nullptr);
    result.pop_back();
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
                           << static_cast<int>(character);
                } else {
                    output << character;
                }
        }
    }
    return output.str();
}

struct TransformInfo {
    std::string name;
    bool hardware = false;
};

std::vector<TransformInfo> EnumerateTransforms(
    const GUID& category,
    const MFT_REGISTER_TYPE_INFO& input,
    const MFT_REGISTER_TYPE_INFO& output,
    UINT32 flags,
    bool hardware) {
    IMFActivate** activations = nullptr;
    UINT32 count = 0;
    std::vector<TransformInfo> result;
    const HRESULT status = MFTEnumEx(category, flags, &input, &output, &activations, &count);
    if (FAILED(status)) {
        return result;
    }
    for (UINT32 index = 0; index < count; ++index) {
        WCHAR* friendly_name = nullptr;
        UINT32 friendly_name_length = 0;
        if (SUCCEEDED(activations[index]->GetAllocatedString(
                MFT_FRIENDLY_NAME_Attribute,
                &friendly_name,
                &friendly_name_length))) {
            result.push_back({Utf8(friendly_name), hardware});
            CoTaskMemFree(friendly_name);
        }
        activations[index]->Release();
    }
    CoTaskMemFree(activations);
    return result;
}

void AppendUnique(std::vector<TransformInfo>& target, const std::vector<TransformInfo>& values) {
    for (const TransformInfo& value : values) {
        bool exists = false;
        for (const TransformInfo& current : target) {
            if (current.name == value.name && current.hardware == value.hardware) {
                exists = true;
                break;
            }
        }
        if (!exists) {
            target.push_back(value);
        }
    }
}

void PrintTransforms(const std::vector<TransformInfo>& transforms) {
    std::cout << '[';
    for (size_t index = 0; index < transforms.size(); ++index) {
        if (index != 0) {
            std::cout << ',';
        }
        std::cout << "{\"name\":\"" << JsonEscape(transforms[index].name)
                  << "\",\"hardware\":" << (transforms[index].hardware ? "true" : "false") << '}';
    }
    std::cout << ']';
}

}  // namespace

int wmain() {
    const HRESULT com_status = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(com_status)) {
        std::cerr << "{\"ok\":false,\"error\":\"CoInitializeEx failed\"}\n";
        return 1;
    }
    const HRESULT mf_status = MFStartup(MF_VERSION, MFSTARTUP_FULL);
    if (FAILED(mf_status)) {
        CoUninitialize();
        std::cerr << "{\"ok\":false,\"error\":\"MFStartup failed\"}\n";
        return 1;
    }

    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL feature_level = D3D_FEATURE_LEVEL_9_1;
    const D3D_FEATURE_LEVEL requested_levels[] = {
        D3D_FEATURE_LEVEL_11_1,
        D3D_FEATURE_LEVEL_11_0,
        D3D_FEATURE_LEVEL_10_1,
        D3D_FEATURE_LEVEL_10_0,
    };
    const HRESULT d3d_status = D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
        requested_levels,
        ARRAYSIZE(requested_levels),
        D3D11_SDK_VERSION,
        &device,
        &feature_level,
        &context);

    const MFT_REGISTER_TYPE_INFO nv12 = {MFMediaType_Video, MFVideoFormat_NV12};
    const MFT_REGISTER_TYPE_INFO h264 = {MFMediaType_Video, MFVideoFormat_H264};
    std::vector<TransformInfo> encoders;
    AppendUnique(
        encoders,
        EnumerateTransforms(
            MFT_CATEGORY_VIDEO_ENCODER,
            nv12,
            h264,
            MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
            true));
    AppendUnique(
        encoders,
        EnumerateTransforms(
            MFT_CATEGORY_VIDEO_ENCODER,
            nv12,
            h264,
            MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_ASYNCMFT | MFT_ENUM_FLAG_LOCALMFT |
                MFT_ENUM_FLAG_SORTANDFILTER,
            false));

    std::vector<TransformInfo> decoders;
    AppendUnique(
        decoders,
        EnumerateTransforms(
            MFT_CATEGORY_VIDEO_DECODER,
            h264,
            nv12,
            MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
            true));
    AppendUnique(
        decoders,
        EnumerateTransforms(
            MFT_CATEGORY_VIDEO_DECODER,
            h264,
            nv12,
            MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_ASYNCMFT | MFT_ENUM_FLAG_LOCALMFT |
                MFT_ENUM_FLAG_SORTANDFILTER,
            false));

    std::cout << "{\"ok\":true,\"d3d11_hardware_device\":"
              << (SUCCEEDED(d3d_status) ? "true" : "false")
              << ",\"d3d_feature_level\":" << static_cast<unsigned int>(feature_level)
              << ",\"h264_encoders\":";
    PrintTransforms(encoders);
    std::cout << ",\"h264_decoders\":";
    PrintTransforms(decoders);
    std::cout << "}\n";

    MFShutdown();
    CoUninitialize();
    return encoders.empty() || decoders.empty() || FAILED(d3d_status) ? 2 : 0;
}
