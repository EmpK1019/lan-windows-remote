#define SECURITY_WIN32
#ifndef WIN32_NO_STATUS
#include <ntstatus.h>
#define WIN32_NO_STATUS
#endif

#include <windows.h>
#include <initguid.h>
#include <credentialprovider.h>
#include <intsafe.h>
#include <ntsecapi.h>
#include <propkey.h>
#include <security.h>
#include <shlguid.h>
#include <shlwapi.h>
#include <strsafe.h>
#include <wincred.h>

#include <new>
#include <string>
#include <vector>

namespace {

// {7C5A4A6F-4E17-4F3A-A2C6-0A9D40B0E105}
const CLSID CLSID_LanRemoteCredentialProvider = {
    0x7c5a4a6f, 0x4e17, 0x4f3a, {0xa2, 0xc6, 0x0a, 0x9d, 0x40, 0xb0, 0xe1, 0x05}};

constexpr wchar_t kPendingRegistryPath[] = L"SOFTWARE\\Windows LAN Remote\\PendingUnlock";
constexpr wchar_t kPendingEventName[] = L"Global\\WindowsLANRemoteUnlockPending";
constexpr ULONGLONG kMaximumRequestAgeMs = 60000;

enum FieldId : DWORD {
    FieldTitle = 0,
    FieldStatus = 1,
    FieldCount = 2,
};

wchar_t g_titleLabel[] = L"LAN Remote";
wchar_t g_statusLabel[] = L"远程解锁";

const CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR kFieldDescriptors[FieldCount] = {
    {FieldTitle, CPFT_LARGE_TEXT, g_titleLabel, CPFG_CREDENTIAL_PROVIDER_LABEL},
    {FieldStatus, CPFT_SMALL_TEXT, g_statusLabel, GUID_NULL},
};

const CREDENTIAL_PROVIDER_FIELD_STATE kFieldStates[FieldCount] = {
    CPFS_DISPLAY_IN_BOTH,
    CPFS_DISPLAY_IN_SELECTED_TILE,
};

LONG g_dllReferences = 0;
HINSTANCE g_instance = nullptr;

struct PendingCredential {
    std::wstring domain;
    std::wstring username;
    std::wstring password;
};

ULONGLONG UnixTimeMilliseconds() {
    FILETIME fileTime{};
    GetSystemTimeAsFileTime(&fileTime);
    ULARGE_INTEGER value{};
    value.LowPart = fileTime.dwLowDateTime;
    value.HighPart = fileTime.dwHighDateTime;
    constexpr ULONGLONG windowsEpoch = 116444736000000000ULL;
    return value.QuadPart > windowsEpoch ? (value.QuadPart - windowsEpoch) / 10000ULL : 0;
}

HRESULT DuplicateString(PCWSTR source, PWSTR* destination) {
    if (!destination) {
        return E_INVALIDARG;
    }
    *destination = nullptr;
    return SHStrDupW(source ? source : L"", destination);
}

HRESULT CopyFieldDescriptor(
    const CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR& source,
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** destination) {
    if (!destination) {
        return E_INVALIDARG;
    }
    *destination = nullptr;
    auto* copy = static_cast<CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR*>(
        CoTaskMemAlloc(sizeof(CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR)));
    if (!copy) {
        return E_OUTOFMEMORY;
    }
    copy->dwFieldID = source.dwFieldID;
    copy->cpft = source.cpft;
    copy->guidFieldType = source.guidFieldType;
    copy->pszLabel = nullptr;
    HRESULT result = DuplicateString(source.pszLabel, &copy->pszLabel);
    if (FAILED(result)) {
        CoTaskMemFree(copy);
        return result;
    }
    *destination = copy;
    return S_OK;
}

bool QueryRegistryString(HKEY key, PCWSTR name, std::wstring* value) {
    DWORD type = 0;
    DWORD bytes = 0;
    if (RegQueryValueExW(key, name, nullptr, &type, nullptr, &bytes) != ERROR_SUCCESS ||
        type != REG_SZ || bytes < sizeof(wchar_t) || bytes > 4096) {
        return false;
    }
    std::vector<wchar_t> buffer(bytes / sizeof(wchar_t) + 1, L'\0');
    if (RegQueryValueExW(
            key,
            name,
            nullptr,
            &type,
            reinterpret_cast<BYTE*>(buffer.data()),
            &bytes) != ERROR_SUCCESS) {
        return false;
    }
    *value = buffer.data();
    return true;
}

bool QueryRegistryQword(HKEY key, PCWSTR name, ULONGLONG* value) {
    DWORD type = 0;
    DWORD bytes = sizeof(*value);
    return RegQueryValueExW(
               key,
               name,
               nullptr,
               &type,
               reinterpret_cast<BYTE*>(value),
               &bytes) == ERROR_SUCCESS &&
        type == REG_QWORD && bytes == sizeof(*value);
}

bool QueryRegistryDword(HKEY key, PCWSTR name, DWORD* value) {
    DWORD type = 0;
    DWORD bytes = sizeof(*value);
    return RegQueryValueExW(
               key,
               name,
               nullptr,
               &type,
               reinterpret_cast<BYTE*>(value),
               &bytes) == ERROR_SUCCESS &&
        type == REG_DWORD && bytes == sizeof(*value);
}

bool QueryRegistryBinary(HKEY key, PCWSTR name, std::vector<BYTE>* value) {
    DWORD type = 0;
    DWORD bytes = 0;
    if (RegQueryValueExW(key, name, nullptr, &type, nullptr, &bytes) != ERROR_SUCCESS ||
        type != REG_BINARY || bytes == 0 || bytes > 16384) {
        return false;
    }
    value->resize(bytes);
    return RegQueryValueExW(key, name, nullptr, &type, value->data(), &bytes) == ERROR_SUCCESS;
}

void DeletePendingCredential() {
    RegDeleteKeyExW(HKEY_LOCAL_MACHINE, kPendingRegistryPath, KEY_WOW64_64KEY, 0);
}

bool ReadPendingCredential(PendingCredential* pending) {
    if (!pending) {
        return false;
    }
    HKEY key = nullptr;
    if (RegOpenKeyExW(
            HKEY_LOCAL_MACHINE,
            kPendingRegistryPath,
            0,
            KEY_QUERY_VALUE | KEY_WOW64_64KEY,
            &key) != ERROR_SUCCESS) {
        return false;
    }

    DWORD version = 0;
    ULONGLONG expiresAt = 0;
    std::vector<BYTE> protectedPassword;
    bool valid = QueryRegistryDword(key, L"Version", &version) && version == 1 &&
        QueryRegistryQword(key, L"ExpiresAtMs", &expiresAt) &&
        QueryRegistryString(key, L"Domain", &pending->domain) &&
        QueryRegistryString(key, L"Username", &pending->username) &&
        QueryRegistryBinary(key, L"ProtectedPassword", &protectedPassword);
    RegCloseKey(key);

    const ULONGLONG now = UnixTimeMilliseconds();
    if (!valid || pending->username.empty() || expiresAt < now || expiresAt > now + kMaximumRequestAgeMs) {
        DeletePendingCredential();
        return false;
    }

    DATA_BLOB input{};
    input.cbData = static_cast<DWORD>(protectedPassword.size());
    input.pbData = protectedPassword.data();
    DATA_BLOB output{};
    LPWSTR description = nullptr;
    if (!CryptUnprotectData(&input, &description, nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &output)) {
        DeletePendingCredential();
        return false;
    }
    if (description) {
        LocalFree(description);
    }
    if (!output.pbData || output.cbData == 0 || output.cbData % sizeof(wchar_t) != 0) {
        if (output.pbData) {
            SecureZeroMemory(output.pbData, output.cbData);
            LocalFree(output.pbData);
        }
        DeletePendingCredential();
        return false;
    }

    pending->password.assign(
        reinterpret_cast<const wchar_t*>(output.pbData),
        output.cbData / sizeof(wchar_t));
    SecureZeroMemory(output.pbData, output.cbData);
    LocalFree(output.pbData);
    SecureZeroMemory(protectedPassword.data(), protectedPassword.size());
    DeletePendingCredential();
    return !pending->password.empty() && pending->password.find(L'\0') == std::wstring::npos;
}

bool EqualInsensitive(const std::wstring& left, const std::wstring& right) {
    return CompareStringOrdinal(left.c_str(), -1, right.c_str(), -1, TRUE) == CSTR_EQUAL;
}

HRESULT InitializeUnicodeString(PWSTR value, UNICODE_STRING* output) {
    if (!value || !output) {
        return E_INVALIDARG;
    }
    USHORT characters = 0;
    HRESULT result = SizeTToUShort(wcslen(value), &characters);
    if (FAILED(result)) {
        return result;
    }
    USHORT bytes = 0;
    result = UShortMult(characters, static_cast<USHORT>(sizeof(wchar_t)), &bytes);
    if (FAILED(result)) {
        return HRESULT_FROM_WIN32(ERROR_ARITHMETIC_OVERFLOW);
    }
    output->Length = bytes;
    output->MaximumLength = bytes;
    output->Buffer = value;
    return S_OK;
}

void CopyPackedUnicodeString(const UNICODE_STRING& source, PWSTR buffer, UNICODE_STRING* destination) {
    destination->Length = source.Length;
    destination->MaximumLength = source.Length;
    destination->Buffer = buffer;
    CopyMemory(destination->Buffer, source.Buffer, source.Length);
}

HRESULT PackInteractiveUnlockLogon(
    PWSTR domain,
    PWSTR username,
    PWSTR password,
    CREDENTIAL_PROVIDER_USAGE_SCENARIO scenario,
    BYTE** serialization,
    DWORD* serializationBytes) {
    KERB_INTERACTIVE_UNLOCK_LOGON unlock{};
    HRESULT result = InitializeUnicodeString(domain, &unlock.Logon.LogonDomainName);
    if (SUCCEEDED(result)) {
        result = InitializeUnicodeString(username, &unlock.Logon.UserName);
    }
    if (SUCCEEDED(result)) {
        result = InitializeUnicodeString(password, &unlock.Logon.Password);
    }
    if (FAILED(result)) {
        return result;
    }
    unlock.Logon.MessageType = scenario == CPUS_UNLOCK_WORKSTATION
        ? KerbWorkstationUnlockLogon
        : KerbInteractiveLogon;

    const DWORD bytes = sizeof(unlock) + unlock.Logon.LogonDomainName.Length +
        unlock.Logon.UserName.Length + unlock.Logon.Password.Length;
    auto* packed = static_cast<KERB_INTERACTIVE_UNLOCK_LOGON*>(CoTaskMemAlloc(bytes));
    if (!packed) {
        return E_OUTOFMEMORY;
    }
    ZeroMemory(packed, sizeof(*packed));
    packed->Logon.MessageType = unlock.Logon.MessageType;
    BYTE* cursor = reinterpret_cast<BYTE*>(packed) + sizeof(*packed);
    CopyPackedUnicodeString(unlock.Logon.LogonDomainName, reinterpret_cast<PWSTR>(cursor), &packed->Logon.LogonDomainName);
    packed->Logon.LogonDomainName.Buffer = reinterpret_cast<PWSTR>(cursor - reinterpret_cast<BYTE*>(packed));
    cursor += packed->Logon.LogonDomainName.Length;
    CopyPackedUnicodeString(unlock.Logon.UserName, reinterpret_cast<PWSTR>(cursor), &packed->Logon.UserName);
    packed->Logon.UserName.Buffer = reinterpret_cast<PWSTR>(cursor - reinterpret_cast<BYTE*>(packed));
    cursor += packed->Logon.UserName.Length;
    CopyPackedUnicodeString(unlock.Logon.Password, reinterpret_cast<PWSTR>(cursor), &packed->Logon.Password);
    packed->Logon.Password.Buffer = reinterpret_cast<PWSTR>(cursor - reinterpret_cast<BYTE*>(packed));
    *serialization = reinterpret_cast<BYTE*>(packed);
    *serializationBytes = bytes;
    return S_OK;
}

HRESULT RetrieveNegotiatePackage(ULONG* package) {
    if (!package) {
        return E_INVALIDARG;
    }
    HANDLE lsa = nullptr;
    NTSTATUS status = LsaConnectUntrusted(&lsa);
    if (status != STATUS_SUCCESS) {
        return HRESULT_FROM_NT(status);
    }
    char packageName[] = NEGOSSP_NAME_A;
    LSA_STRING name{};
    name.Buffer = packageName;
    name.Length = static_cast<USHORT>(strlen(packageName));
    name.MaximumLength = name.Length + 1;
    status = LsaLookupAuthenticationPackage(lsa, &name, package);
    LsaDeregisterLogonProcess(lsa);
    return status == STATUS_SUCCESS ? S_OK : HRESULT_FROM_NT(status);
}

HRESULT ProtectPassword(PCWSTR password, PWSTR* protectedPassword) {
    if (!password || !protectedPassword) {
        return E_INVALIDARG;
    }
    *protectedPassword = nullptr;
    if (!*password) {
        return DuplicateString(L"", protectedPassword);
    }
    PWSTR copy = nullptr;
    HRESULT result = DuplicateString(password, &copy);
    if (FAILED(result)) {
        return result;
    }
    DWORD characters = 0;
    CredProtectW(FALSE, copy, static_cast<DWORD>(wcslen(copy) + 1), nullptr, &characters, nullptr);
    if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || characters == 0) {
        SecureZeroMemory(copy, wcslen(copy) * sizeof(wchar_t));
        CoTaskMemFree(copy);
        return HRESULT_FROM_WIN32(GetLastError());
    }
    auto* output = static_cast<PWSTR>(CoTaskMemAlloc(characters * sizeof(wchar_t)));
    if (!output) {
        SecureZeroMemory(copy, wcslen(copy) * sizeof(wchar_t));
        CoTaskMemFree(copy);
        return E_OUTOFMEMORY;
    }
    if (!CredProtectW(FALSE, copy, static_cast<DWORD>(wcslen(copy) + 1), output, &characters, nullptr)) {
        result = HRESULT_FROM_WIN32(GetLastError());
        CoTaskMemFree(output);
    } else {
        *protectedPassword = output;
        result = S_OK;
    }
    SecureZeroMemory(copy, wcslen(copy) * sizeof(wchar_t));
    CoTaskMemFree(copy);
    return result;
}

bool SplitQualifiedName(const std::wstring& qualified, std::wstring* domain, std::wstring* username) {
    const size_t slash = qualified.find(L'\\');
    if (slash == std::wstring::npos || slash == 0 || slash + 1 >= qualified.size()) {
        return false;
    }
    *domain = qualified.substr(0, slash);
    *username = qualified.substr(slash + 1);
    return true;
}

class Credential final : public ICredentialProviderCredential2 {
public:
    Credential(
        CREDENTIAL_PROVIDER_USAGE_SCENARIO scenario,
        ICredentialProviderUser* user,
        PendingCredential&& pending)
        : references_(1), scenario_(scenario), password_(std::move(pending.password)) {
        InterlockedIncrement(&g_dllReferences);
        GUID providerId{};
        if (SUCCEEDED(user->GetProviderID(&providerId))) {
            localUser_ = providerId == Identity_LocalUserProvider;
        }
        user->GetSid(&userSid_);
        user->GetStringValue(PKEY_Identity_QualifiedUserName, &qualifiedUsername_);
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) {
            return E_INVALIDARG;
        }
        *object = nullptr;
        if (iid == IID_IUnknown || iid == IID_ICredentialProviderCredential) {
            *object = static_cast<ICredentialProviderCredential*>(this);
        } else if (iid == IID_ICredentialProviderCredential2) {
            *object = static_cast<ICredentialProviderCredential2*>(this);
        } else {
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }

    ULONG STDMETHODCALLTYPE AddRef() override { return static_cast<ULONG>(InterlockedIncrement(&references_)); }
    ULONG STDMETHODCALLTYPE Release() override {
        const LONG remaining = InterlockedDecrement(&references_);
        if (!remaining) {
            delete this;
        }
        return static_cast<ULONG>(remaining);
    }

    HRESULT STDMETHODCALLTYPE Advise(ICredentialProviderCredentialEvents*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE UnAdvise() override { return S_OK; }
    HRESULT STDMETHODCALLTYPE SetSelected(BOOL* autoLogon) override {
        if (!autoLogon) {
            return E_INVALIDARG;
        }
        *autoLogon = TRUE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetDeselected() override { return S_OK; }
    HRESULT STDMETHODCALLTYPE GetFieldState(
        DWORD field,
        CREDENTIAL_PROVIDER_FIELD_STATE* state,
        CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* interactive) override {
        if (!state || !interactive || field >= FieldCount) {
            return E_INVALIDARG;
        }
        *state = kFieldStates[field];
        *interactive = CPFIS_NONE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetStringValue(DWORD field, PWSTR* value) override {
        if (field == FieldTitle) {
            return DuplicateString(L"LAN Remote", value);
        }
        if (field == FieldStatus) {
            return DuplicateString(L"正在安全解锁此电脑…", value);
        }
        return E_INVALIDARG;
    }
    HRESULT STDMETHODCALLTYPE GetBitmapValue(DWORD, HBITMAP* bitmap) override {
        if (bitmap) {
            *bitmap = nullptr;
        }
        return E_INVALIDARG;
    }
    HRESULT STDMETHODCALLTYPE GetCheckboxValue(DWORD, BOOL*, PWSTR*) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE GetComboBoxValueCount(DWORD, DWORD*, DWORD*) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE GetComboBoxValueAt(DWORD, DWORD, PWSTR*) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE GetSubmitButtonValue(DWORD, DWORD*) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE SetStringValue(DWORD, PCWSTR) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE SetCheckboxValue(DWORD, BOOL) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE SetComboBoxSelectedValue(DWORD, DWORD) override { return E_INVALIDARG; }
    HRESULT STDMETHODCALLTYPE CommandLinkClicked(DWORD) override { return E_INVALIDARG; }

    HRESULT STDMETHODCALLTYPE GetSerialization(
        CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* response,
        CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* serialization,
        PWSTR* statusText,
        CREDENTIAL_PROVIDER_STATUS_ICON* statusIcon) override {
        if (!response || !serialization || !statusText || !statusIcon || !qualifiedUsername_) {
            return E_INVALIDARG;
        }
        *response = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
        *statusText = nullptr;
        *statusIcon = CPSI_NONE;
        ZeroMemory(serialization, sizeof(*serialization));

        HRESULT result = E_FAIL;
        if (localUser_) {
            std::wstring domain;
            std::wstring username;
            if (!SplitQualifiedName(qualifiedUsername_, &domain, &username)) {
                return E_FAIL;
            }
            PWSTR protectedPassword = nullptr;
            result = ProtectPassword(password_.c_str(), &protectedPassword);
            if (SUCCEEDED(result)) {
                result = PackInteractiveUnlockLogon(
                    domain.data(),
                    username.data(),
                    protectedPassword,
                    scenario_,
                    &serialization->rgbSerialization,
                    &serialization->cbSerialization);
                SecureZeroMemory(protectedPassword, wcslen(protectedPassword) * sizeof(wchar_t));
                CoTaskMemFree(protectedPassword);
            }
        } else {
            const DWORD flags = CRED_PACK_PROTECTED_CREDENTIALS | CRED_PACK_ID_PROVIDER_CREDENTIALS;
            CredPackAuthenticationBufferW(
                flags,
                qualifiedUsername_,
                password_.data(),
                nullptr,
                &serialization->cbSerialization);
            if (GetLastError() == ERROR_INSUFFICIENT_BUFFER) {
                serialization->rgbSerialization = static_cast<BYTE*>(
                    CoTaskMemAlloc(serialization->cbSerialization));
                if (!serialization->rgbSerialization) {
                    result = E_OUTOFMEMORY;
                } else if (CredPackAuthenticationBufferW(
                               flags,
                               qualifiedUsername_,
                               password_.data(),
                               serialization->rgbSerialization,
                               &serialization->cbSerialization)) {
                    result = S_OK;
                } else {
                    result = HRESULT_FROM_WIN32(GetLastError());
                }
            }
        }

        if (SUCCEEDED(result)) {
            result = RetrieveNegotiatePackage(&serialization->ulAuthenticationPackage);
        }
        if (SUCCEEDED(result)) {
            serialization->clsidCredentialProvider = CLSID_LanRemoteCredentialProvider;
            *response = CPGSR_RETURN_CREDENTIAL_FINISHED;
            ClearPassword();
        } else if (serialization->rgbSerialization) {
            SecureZeroMemory(serialization->rgbSerialization, serialization->cbSerialization);
            CoTaskMemFree(serialization->rgbSerialization);
            serialization->rgbSerialization = nullptr;
            serialization->cbSerialization = 0;
        }
        return result;
    }

    HRESULT STDMETHODCALLTYPE ReportResult(
        NTSTATUS,
        NTSTATUS,
        PWSTR* statusText,
        CREDENTIAL_PROVIDER_STATUS_ICON* statusIcon) override {
        if (statusText) {
            *statusText = nullptr;
        }
        if (statusIcon) {
            *statusIcon = CPSI_NONE;
        }
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetUserSid(PWSTR* sid) override {
        return DuplicateString(userSid_, sid);
    }

private:
    ~Credential() {
        ClearPassword();
        CoTaskMemFree(userSid_);
        CoTaskMemFree(qualifiedUsername_);
        InterlockedDecrement(&g_dllReferences);
    }

    void ClearPassword() {
        if (!password_.empty()) {
            SecureZeroMemory(password_.data(), password_.size() * sizeof(wchar_t));
            password_.clear();
        }
    }

    LONG references_;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO scenario_;
    std::wstring password_;
    PWSTR userSid_ = nullptr;
    PWSTR qualifiedUsername_ = nullptr;
    bool localUser_ = false;
};

class Provider final : public ICredentialProvider, public ICredentialProviderSetUserArray {
public:
    Provider() : references_(1) {
        InitializeCriticalSection(&lock_);
        InterlockedIncrement(&g_dllReferences);
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) {
            return E_INVALIDARG;
        }
        *object = nullptr;
        if (iid == IID_IUnknown || iid == IID_ICredentialProvider) {
            *object = static_cast<ICredentialProvider*>(this);
        } else if (iid == IID_ICredentialProviderSetUserArray) {
            *object = static_cast<ICredentialProviderSetUserArray*>(this);
        } else {
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return static_cast<ULONG>(InterlockedIncrement(&references_)); }
    ULONG STDMETHODCALLTYPE Release() override {
        const LONG remaining = InterlockedDecrement(&references_);
        if (!remaining) {
            delete this;
        }
        return static_cast<ULONG>(remaining);
    }

    HRESULT STDMETHODCALLTYPE SetUsageScenario(CREDENTIAL_PROVIDER_USAGE_SCENARIO scenario, DWORD) override {
        if (scenario != CPUS_LOGON && scenario != CPUS_UNLOCK_WORKSTATION) {
            return E_NOTIMPL;
        }
        scenario_ = scenario;
        dirty_ = true;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetSerialization(const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) override {
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE Advise(ICredentialProviderEvents* events, UINT_PTR context) override {
        if (!events) {
            return E_INVALIDARG;
        }
        StopWatcher();
        EnterCriticalSection(&lock_);
        events_ = events;
        events_->AddRef();
        adviseContext_ = context;
        LeaveCriticalSection(&lock_);

        stopEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        unlockEvent_ = CreateEventW(nullptr, FALSE, FALSE, kPendingEventName);
        if (!stopEvent_ || !unlockEvent_) {
            StopWatcher();
            return HRESULT_FROM_WIN32(GetLastError());
        }
        AddRef();
        watcherThread_ = CreateThread(nullptr, 0, &Provider::WatcherEntry, this, 0, nullptr);
        if (!watcherThread_) {
            Release();
            StopWatcher();
            return HRESULT_FROM_WIN32(GetLastError());
        }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE UnAdvise() override {
        StopWatcher();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetFieldDescriptorCount(DWORD* count) override {
        if (!count) {
            return E_INVALIDARG;
        }
        *count = FieldCount;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetFieldDescriptorAt(
        DWORD index,
        CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** descriptor) override {
        if (index >= FieldCount) {
            return E_INVALIDARG;
        }
        return CopyFieldDescriptor(kFieldDescriptors[index], descriptor);
    }
    HRESULT STDMETHODCALLTYPE GetCredentialCount(DWORD* count, DWORD* defaultIndex, BOOL* autoLogon) override {
        if (!count || !defaultIndex || !autoLogon) {
            return E_INVALIDARG;
        }
        RefreshCredentialIfNeeded();
        *count = credential_ ? 1 : 0;
        *defaultIndex = credential_ ? 0 : CREDENTIAL_PROVIDER_NO_DEFAULT;
        *autoLogon = credential_ ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetCredentialAt(DWORD index, ICredentialProviderCredential** credential) override {
        if (!credential) {
            return E_INVALIDARG;
        }
        *credential = nullptr;
        if (index != 0 || !credential_) {
            return E_INVALIDARG;
        }
        return credential_->QueryInterface(IID_PPV_ARGS(credential));
    }
    HRESULT STDMETHODCALLTYPE SetUserArray(ICredentialProviderUserArray* users) override {
        if (!users) {
            return E_INVALIDARG;
        }
        if (users_) {
            users_->Release();
        }
        users_ = users;
        users_->AddRef();
        dirty_ = true;
        return S_OK;
    }

private:
    ~Provider() {
        StopWatcher();
        if (credential_) {
            credential_->Release();
        }
        if (users_) {
            users_->Release();
        }
        DeleteCriticalSection(&lock_);
        InterlockedDecrement(&g_dllReferences);
    }

    static DWORD WINAPI WatcherEntry(void* context) {
        auto* self = static_cast<Provider*>(context);
        self->WatcherLoop();
        self->Release();
        return 0;
    }

    void WatcherLoop() {
        HANDLE handles[] = {stopEvent_, unlockEvent_};
        while (WaitForMultipleObjects(2, handles, FALSE, INFINITE) == WAIT_OBJECT_0 + 1) {
            ICredentialProviderEvents* events = nullptr;
            UINT_PTR context = 0;
            EnterCriticalSection(&lock_);
            dirty_ = true;
            if (events_) {
                events = events_;
                events->AddRef();
                context = adviseContext_;
            }
            LeaveCriticalSection(&lock_);
            if (events) {
                events->CredentialsChanged(context);
                events->Release();
            }
        }
    }

    void StopWatcher() {
        if (stopEvent_) {
            SetEvent(stopEvent_);
        }
        if (watcherThread_) {
            WaitForSingleObject(watcherThread_, 3000);
            CloseHandle(watcherThread_);
            watcherThread_ = nullptr;
        }
        if (stopEvent_) {
            CloseHandle(stopEvent_);
            stopEvent_ = nullptr;
        }
        if (unlockEvent_) {
            CloseHandle(unlockEvent_);
            unlockEvent_ = nullptr;
        }
        EnterCriticalSection(&lock_);
        if (events_) {
            events_->Release();
            events_ = nullptr;
        }
        adviseContext_ = 0;
        LeaveCriticalSection(&lock_);
    }

    void RefreshCredentialIfNeeded() {
        EnterCriticalSection(&lock_);
        const bool refresh = dirty_;
        dirty_ = false;
        LeaveCriticalSection(&lock_);
        if (!refresh) {
            return;
        }
        if (credential_) {
            credential_->Release();
            credential_ = nullptr;
        }
        if (!users_) {
            dirty_ = true;
            return;
        }
        PendingCredential pending;
        if (!ReadPendingCredential(&pending)) {
            return;
        }
        DWORD userCount = 0;
        if (FAILED(users_->GetCount(&userCount))) {
            SecureZeroMemory(pending.password.data(), pending.password.size() * sizeof(wchar_t));
            return;
        }
        for (DWORD index = 0; index < userCount; ++index) {
            ICredentialProviderUser* user = nullptr;
            if (FAILED(users_->GetAt(index, &user)) || !user) {
                continue;
            }
            PWSTR qualified = nullptr;
            PWSTR username = nullptr;
            user->GetStringValue(PKEY_Identity_QualifiedUserName, &qualified);
            user->GetStringValue(PKEY_Identity_UserName, &username);
            const std::wstring requestedQualified = pending.domain.empty()
                ? pending.username
                : pending.domain + L"\\" + pending.username;
            const bool matches = (qualified && EqualInsensitive(qualified, requestedQualified)) ||
                (username && EqualInsensitive(username, pending.username));
            CoTaskMemFree(qualified);
            CoTaskMemFree(username);
            if (matches) {
                credential_ = new (std::nothrow) Credential(scenario_, user, std::move(pending));
                user->Release();
                return;
            }
            user->Release();
        }
        SecureZeroMemory(pending.password.data(), pending.password.size() * sizeof(wchar_t));
    }

    LONG references_;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO scenario_ = CPUS_INVALID;
    ICredentialProviderUserArray* users_ = nullptr;
    Credential* credential_ = nullptr;
    ICredentialProviderEvents* events_ = nullptr;
    UINT_PTR adviseContext_ = 0;
    HANDLE stopEvent_ = nullptr;
    HANDLE unlockEvent_ = nullptr;
    HANDLE watcherThread_ = nullptr;
    CRITICAL_SECTION lock_{};
    bool dirty_ = true;
};

class ClassFactory final : public IClassFactory {
public:
    ClassFactory() { InterlockedIncrement(&g_dllReferences); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) {
            return E_INVALIDARG;
        }
        *object = nullptr;
        if (iid != IID_IUnknown && iid != IID_IClassFactory) {
            return E_NOINTERFACE;
        }
        *object = static_cast<IClassFactory*>(this);
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return static_cast<ULONG>(InterlockedIncrement(&references_)); }
    ULONG STDMETHODCALLTYPE Release() override {
        const LONG remaining = InterlockedDecrement(&references_);
        if (!remaining) {
            delete this;
        }
        return static_cast<ULONG>(remaining);
    }
    HRESULT STDMETHODCALLTYPE CreateInstance(IUnknown* outer, REFIID iid, void** object) override {
        if (outer) {
            return CLASS_E_NOAGGREGATION;
        }
        auto* provider = new (std::nothrow) Provider();
        if (!provider) {
            return E_OUTOFMEMORY;
        }
        const HRESULT result = provider->QueryInterface(iid, object);
        provider->Release();
        return result;
    }
    HRESULT STDMETHODCALLTYPE LockServer(BOOL lock) override {
        lock ? InterlockedIncrement(&g_dllReferences) : InterlockedDecrement(&g_dllReferences);
        return S_OK;
    }

private:
    ~ClassFactory() { InterlockedDecrement(&g_dllReferences); }
    LONG references_ = 1;
};

}  // namespace

extern "C" HRESULT __stdcall DllCanUnloadNow() {
    return g_dllReferences == 0 ? S_OK : S_FALSE;
}

extern "C" HRESULT __stdcall DllGetClassObject(REFCLSID classId, REFIID iid, void** object) {
    if (classId != CLSID_LanRemoteCredentialProvider) {
        return CLASS_E_CLASSNOTAVAILABLE;
    }
    auto* factory = new (std::nothrow) ClassFactory();
    if (!factory) {
        return E_OUTOFMEMORY;
    }
    const HRESULT result = factory->QueryInterface(iid, object);
    factory->Release();
    return result;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, void*) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_instance = instance;
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}
