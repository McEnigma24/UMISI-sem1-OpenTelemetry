cmake_minimum_required(VERSION 3.18)
project(MyProject LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# set(CMAKE_CXX_COMPILER g++)
# set(CMAKE_CXX_COMPILER clang++)

include_directories(_inc)
aux_source_directory(_src SOURCES)

aux_source_directory(_src TEST_SOURCES)
list(FILTER TEST_SOURCES EXCLUDE REGEX ".*main\\.cpp")
aux_source_directory(_test TEST_SOURCES)


# MACRO FOR LOGS # - dont show the whole realpath to file

string(LENGTH "${CMAKE_SOURCE_DIR}/" SOURCE_PATH_SIZE)
add_definitions(-DSOURCE_PATH_SIZE=${SOURCE_PATH_SIZE})

# Change names of:
# TEMPLATE___CTEST_ACTIVE
# TEMPLATE___BUILD_LIBRARY

set(CONST_TARGET_NAME "TEMPLATE")

# COMPILER FLAGS #

# https://docs.oracle.com/cd/E19957-01/806-3572/Rev.html
# https://caiorss.github.io/C-Cpp-Notes/compiler-flags-options.html
# https://gcc.gnu.org/onlinedocs/gcc/Warning-Options.html#index-Wfatal-errors

# set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -g -Wall -Wextra")                                                             # to see everything
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -g -Wfatal-errors -Werror=uninitialized -Werror=init-self -Werror=reorder")    # for normal coding
# set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O3 -march=native -w")                                                         # for release

set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fopenmp")
# set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -mavx2")

#                                                                  https://hjlebbink.github.io/x86doc/
#                                                                  - look for *.o file
# set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -S") # ASSEMBLY
# set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -S -fverbose-asm") # ASSEMBLY with comments


# set(COMPILE_FLAGS "${COMPILE_FLAGS} -m64")
# set(LINK_FLAGS "${LINK_FLAGS} -m64")


# 1 - core #
# Private repo: pass a classic PAT (repo scope) at configure time, e.g.
#   export GITHUB_TOKEN=ghp_...
#   cmake -S . -B build
# or: docker run -e GITHUB_TOKEN=ghp_... ...
# Also accepts GH_TOKEN. Do not commit tokens; URL is not written to sources.

set(_gh_token "$ENV{GITHUB_TOKEN}")
if(_gh_token STREQUAL "")
    set(_gh_token "$ENV{GH_TOKEN}")
endif()
if(NOT _gh_token STREQUAL "")
    set(CORE_GIT_REPO "https://x-access-token:${_gh_token}@github.com/McEnigma24/CORE_lib.git")
else()
    set(CORE_GIT_REPO "https://github.com/McEnigma24/CORE_lib.git")
endif()
unset(_gh_token)

include(FetchContent)
FetchContent_Declare(
    core
    GIT_REPOSITORY ${CORE_GIT_REPO}
    GIT_TAG        main # lub inny tag/commit
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(core)

set(ALL_LIBRARIES ${ALL_LIBRARIES} core)
include_directories(${core_SOURCE_DIR}/_inc)





# 2 - opentelemetry-cpp #
# Must set BUILD_TESTING OFF *before the first* FetchContent_MakeAvailable, or OTel
# configures tests and find_package(GTest) (often picking Windows Conda from /mnt/c).
# OTLP/HTTP exporter needs CURL (see config: libcurl4-openssl-dev).
# After first configure without OTLP, delete cpp/build once so this option is picked up.
set(WITH_OTLP_HTTP ON CACHE BOOL "OTLP/HTTP span exporter" FORCE)
include(FetchContent)
FetchContent_Declare(
    opentelemetry-cpp
    GIT_REPOSITORY https://github.com/open-telemetry/opentelemetry-cpp.git
    GIT_TAG        v1.20.0
    GIT_SHALLOW    TRUE
)
set(_otel_saved_BUILD_TESTING "${BUILD_TESTING}")
set(BUILD_TESTING OFF CACHE BOOL "Build tests" FORCE)
FetchContent_MakeAvailable(opentelemetry-cpp)
if(_otel_saved_BUILD_TESTING STREQUAL "")
    unset(BUILD_TESTING CACHE)
else()
    set(BUILD_TESTING "${_otel_saved_BUILD_TESTING}" CACHE BOOL "Build tests" FORCE)
endif()
unset(_otel_saved_BUILD_TESTING)

# Trace API + SDK: ostream (stderr) and/or OTLP/HTTP (pick in code via OTEL_DEMO_TRACE_EXPORT)
set(ALL_LIBRARIES ${ALL_LIBRARIES} opentelemetry_api)
set(ALL_LIBRARIES ${ALL_LIBRARIES} opentelemetry_trace)
set(ALL_LIBRARIES ${ALL_LIBRARIES} opentelemetry_exporter_ostream_span)
set(ALL_LIBRARIES ${ALL_LIBRARIES} opentelemetry_exporter_otlp_http)


# EXAMPLE - ssd1306 #
# include(FetchContent)
# FetchContent_Declare(
#     ssd1306
#     GIT_REPOSITORY https://github.com/armlabs/ssd1306_linux.git
#     GIT_TAG        master # lub inny tag/commit
# )
# FetchContent_MakeAvailable(ssd1306) # tutaj nie dzial, a normanie zbudowalby projekt, ale tutaj nie zrobi bo nie ma pliku CMakeLists.txt

# include_directories(${ssd1306_SOURCE_DIR})

# # Dodaj pliki źródłowe ręcznie
# file(GLOB SSD1306_SOURCES
#     ${ssd1306_SOURCE_DIR}/*.c
# )

# add_library(ssd1306 STATIC ${SSD1306_SOURCES})
# target_include_directories(ssd1306 PUBLIC ${ssd1306_SOURCE_DIR})

# set(ALL_LIBRARIES ${ALL_LIBRARIES} ssd1306) # dodajemy do glownego EXE



option(TEMPLATE___BUILD_LIBRARY "Build library instead of executable" OFF) # default value - could be overwritten by provided FLAG

if(TEMPLATE___BUILD_LIBRARY)
    message(WARNING "Building library")

    set(TARGET_NAME "${CONST_TARGET_NAME}")
    add_library(${TARGET_NAME} SHARED ${SOURCES})
    target_compile_definitions(${TARGET_NAME} PRIVATE LINUX)
    # set_target_properties(${TARGET_NAME} PROPERTIES ${COMPILE_FLAGS} ${LINK_FLAGS})
    target_link_libraries(${TARGET_NAME} PUBLIC ${ALL_LIBRARIES})
else()
    message(WARNING "Building exe")

    set(TARGET_NAME "${CONST_TARGET_NAME}.exe")
    add_executable(${TARGET_NAME} ${SOURCES})
    target_compile_definitions(${TARGET_NAME} PRIVATE LINUX BUILD_EXECUTABLE)
    # set_target_properties(${TARGET_NAME} PROPERTIES ${COMPILE_FLAGS} ${LINK_FLAGS})
    target_link_libraries(${TARGET_NAME} PUBLIC ${ALL_LIBRARIES})
endif()

option(TEMPLATE___CTEST_ACTIVE "Testing is active" OFF)  # default value - could be overwritten by provided FLAG

if(TEMPLATE___CTEST_ACTIVE)
    message(WARNING "TESTING...")
    # Włączenie testów
    enable_testing()

    include(FetchContent)
    FetchContent_Declare(
        googletest
        GIT_REPOSITORY https://github.com/google/googletest.git
        GIT_TAG        v1.14.0  # możesz zmienić tag na nowszy gdy będzie dostępny
        GIT_SHALLOW    TRUE
    )
    # For Windows: Prevent overriding the parent project's compiler/linker settings
    set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(googletest)

    # Dodanie testów
    set(TEST_EXE_NAME "test.texe")
    add_executable(${TEST_EXE_NAME} ${TEST_SOURCES})
    target_link_libraries(${TEST_EXE_NAME} PRIVATE gtest_main ${ALL_LIBRARIES})

    # Rejestracja testów
    include(GoogleTest)
    gtest_discover_tests(${TEST_EXE_NAME})
endif()