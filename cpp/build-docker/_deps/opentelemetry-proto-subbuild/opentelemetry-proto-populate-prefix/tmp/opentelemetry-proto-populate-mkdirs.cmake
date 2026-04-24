# Distributed under the OSI-approved BSD 3-Clause License.  See accompanying
# file Copyright.txt or https://cmake.org/licensing for details.

cmake_minimum_required(VERSION 3.5)

file(MAKE_DIRECTORY
  "/app/external/opentelemetry-cpp/third_party/opentelemetry-proto"
  "/app/build/_deps/opentelemetry-proto-build"
  "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix"
  "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix/tmp"
  "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix/src/opentelemetry-proto-populate-stamp"
  "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix/src"
  "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix/src/opentelemetry-proto-populate-stamp"
)

set(configSubDirs )
foreach(subDir IN LISTS configSubDirs)
    file(MAKE_DIRECTORY "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix/src/opentelemetry-proto-populate-stamp/${subDir}")
endforeach()
if(cfgdir)
  file(MAKE_DIRECTORY "/app/build/_deps/opentelemetry-proto-subbuild/opentelemetry-proto-populate-prefix/src/opentelemetry-proto-populate-stamp${cfgdir}") # cfgdir has leading slash
endif()
