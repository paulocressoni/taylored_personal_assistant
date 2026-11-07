# The minimum CMake version ESP-IDF 5.4 supports.
cmake_minimum_required(VERSION 3.16)

# Pull in ESP-IDF's build system. IDF_PATH is set by export.sh and points
# at ~/esp/esp-idf. This line is what turns a folder into an ESP-IDF project.
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# The project name becomes the name of the .elf and .bin files.
project(satellite_xvf3800_xiao)
