/* RanchuWoA：android/base/properties.h 的最小替代。GetProperty/GetUintProperty 链接
   vendor 的 libbase.so（真实现），模板 GetUintProperty 在头内展开。 */
#pragma once

#include <cstdint>
#include <cstdlib>
#include <limits>
#include <string>

namespace android {
namespace base {

std::string GetProperty(const std::string &key, const std::string &default_value);

template <typename T>
T GetUintProperty(const std::string &key, T default_value, T max_value) {
    std::string v = GetProperty(key, "");
    if (v.empty()) return default_value;
    unsigned long long parsed = strtoull(v.c_str(), nullptr, 0);
    if (parsed > static_cast<unsigned long long>(max_value)) return default_value;
    return static_cast<T>(parsed);
}

template <typename T>
T GetUintProperty(const std::string &key, T default_value) {
    return GetUintProperty(key, default_value, std::numeric_limits<T>::max());
}

}  // namespace base
}  // namespace android
