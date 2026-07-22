#include <windows.h>
#include <credentialprovider.h>

#include <iostream>

namespace {

const CLSID CLSID_LanRemoteCredentialProvider = {
    0x7c5a4a6f, 0x4e17, 0x4f3a, {0xa2, 0xc6, 0x0a, 0x9d, 0x40, 0xb0, 0xe1, 0x05}};

using DllGetClassObjectFunction = HRESULT(__stdcall*)(REFCLSID, REFIID, void**);

int Fail(const wchar_t* message) {
    std::wcerr << message << std::endl;
    return 1;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc != 2) {
        return Fail(L"credential provider DLL path required");
    }
    HMODULE module = LoadLibraryW(argv[1]);
    if (!module) {
        return Fail(L"could not load credential provider DLL");
    }
    auto getClassObject = reinterpret_cast<DllGetClassObjectFunction>(
        GetProcAddress(module, "DllGetClassObject"));
    if (!getClassObject) {
        FreeLibrary(module);
        return Fail(L"DllGetClassObject export missing");
    }

    IClassFactory* factory = nullptr;
    HRESULT result = getClassObject(
        CLSID_LanRemoteCredentialProvider,
        IID_IClassFactory,
        reinterpret_cast<void**>(&factory));
    if (FAILED(result) || !factory) {
        FreeLibrary(module);
        return Fail(L"credential provider class factory unavailable");
    }
    ICredentialProvider* provider = nullptr;
    result = factory->CreateInstance(
        nullptr,
        IID_ICredentialProvider,
        reinterpret_cast<void**>(&provider));
    factory->Release();
    if (FAILED(result) || !provider) {
        FreeLibrary(module);
        return Fail(L"credential provider instance unavailable");
    }
    result = provider->SetUsageScenario(CPUS_UNLOCK_WORKSTATION, 0);
    DWORD fieldCount = 0;
    if (FAILED(result) || FAILED(provider->GetFieldDescriptorCount(&fieldCount)) || fieldCount != 2) {
        provider->Release();
        FreeLibrary(module);
        return Fail(L"credential provider field contract invalid");
    }
    DWORD credentialCount = 0;
    DWORD defaultIndex = 0;
    BOOL autoLogon = FALSE;
    result = provider->GetCredentialCount(&credentialCount, &defaultIndex, &autoLogon);
    provider->Release();
    FreeLibrary(module);
    if (FAILED(result) || credentialCount != 0 || autoLogon) {
        return Fail(L"credential provider must stay hidden without a pending request");
    }
    std::wcout << L"CREDENTIAL_PROVIDER_SMOKE_TEST_OK" << std::endl;
    return 0;
}
