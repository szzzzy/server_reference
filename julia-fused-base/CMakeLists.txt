# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.22)

# 生成 compile_commands.json 供 clangd / VSCode C/C++ 插件的 IntelliSense 使用。
# 注意：必须在 include(project.cmake) 之前设置（在 project() 调用前）。
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# 稳定版本号（与 fused 一致）：OTA 判断/状态上报/镜像头全部读取
# esp_app_desc_t.version，格式严格 major.minor.patch；不依赖 git 派生。
set(PROJECT_VER "0.1.0")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# "Trim" the build. Include the minimal set of components, main, and anything it depends on.
idf_build_set_property(MINIMAL_BUILD ON)

# This workstation's ESP-IDF extension invokes nested CMake projects (the
# bootloader) without inheriting the tool paths exported for the top-level
# build. Pass the existing local Ninja and Xtensa bin directory explicitly so
# both the application and bootloader configure with the same toolchain.
idf_build_set_property(EXTRA_CMAKE_ARGS
    "-DCMAKE_MAKE_PROGRAM=D:/Espressif/tools/ninja/1.12.1/ninja.exe" APPEND)
idf_build_set_property(EXTRA_CMAKE_ARGS
    "-DCMAKE_PROGRAM_PATH=D:/Espressif/tools/xtensa-esp-elf/esp-14.2.0_20260121/xtensa-esp-elf/bin" APPEND)
project(julia_fused_base LANGUAGES C CXX)
