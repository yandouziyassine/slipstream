cmake_minimum_required(VERSION 3.22)
project(slipstream_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(SLIPSTREAM_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
if(SLIPSTREAM_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all)
  add_link_options(-fsanitize=address,undefined)
endif()

set(SLIPSTREAM_WARNINGS -Wall -Wextra -Wpedantic -Wshadow -Werror)

add_library(slipstream_core STATIC
  src/order_book.cpp
  src/slicing.cpp
  src/twap.cpp
  src/vwap.cpp
  src/almgren_chriss.cpp
  src/pov.cpp
  src/schedule_factory.cpp
  src/risk.cpp
  src/fill_simulator.cpp
  src/engine.cpp
  src/config.cpp
  src/router.cpp
  src/clock.cpp
  src/engine_loop.cpp
)
target_include_directories(slipstream_core PUBLIC src)
target_compile_options(slipstream_core PRIVATE ${SLIPSTREAM_WARNINGS})
find_package(Threads REQUIRED)
target_link_libraries(slipstream_core PUBLIC Threads::Threads)

find_package(Protobuf REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(GRPCPP REQUIRED IMPORTED_TARGET grpc++)
find_program(GRPC_CPP_PLUGIN grpc_cpp_plugin REQUIRED)

set(PROTO_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../proto)
set(PROTO_FILE ${PROTO_ROOT}/slipstream/v1/execution.proto)
set(GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/gen)
set(GEN_SRCS
  ${GEN_DIR}/slipstream/v1/execution.pb.cc
  ${GEN_DIR}/slipstream/v1/execution.grpc.pb.cc)
set(GEN_HDRS
  ${GEN_DIR}/slipstream/v1/execution.pb.h
  ${GEN_DIR}/slipstream/v1/execution.grpc.pb.h)
file(MAKE_DIRECTORY ${GEN_DIR})
add_custom_command(
  OUTPUT ${GEN_SRCS} ${GEN_HDRS}
  COMMAND ${Protobuf_PROTOC_EXECUTABLE}
    --proto_path=${PROTO_ROOT}
    --cpp_out=${GEN_DIR}
    --grpc_out=${GEN_DIR}
    --plugin=protoc-gen-grpc=${GRPC_CPP_PLUGIN}
    ${PROTO_FILE}
  DEPENDS ${PROTO_FILE}
  COMMENT "Generating C++ code from execution.proto")

add_library(slipstream_proto STATIC ${GEN_SRCS})
target_include_directories(slipstream_proto SYSTEM PUBLIC ${GEN_DIR})
target_link_libraries(slipstream_proto PUBLIC protobuf::libprotobuf PkgConfig::GRPCPP)

add_library(slipstream_service STATIC src/service.cpp)
target_link_libraries(slipstream_service PUBLIC slipstream_core slipstream_proto)
target_compile_options(slipstream_service PRIVATE ${SLIPSTREAM_WARNINGS})

add_executable(slipstream_engine src/main.cpp)
target_link_libraries(slipstream_engine PRIVATE slipstream_service)
target_compile_options(slipstream_engine PRIVATE ${SLIPSTREAM_WARNINGS})

enable_testing()
find_package(GTest REQUIRED)
include(GoogleTest)

add_executable(engine_tests
  tests/order_book_test.cpp
  tests/twap_test.cpp
  tests/vwap_test.cpp
  tests/almgren_chriss_test.cpp
  tests/pov_test.cpp
  tests/schedule_invariants_test.cpp
  tests/risk_test.cpp
  tests/fill_simulator_test.cpp
  tests/engine_test.cpp
  tests/config_test.cpp
  tests/service_test.cpp
  tests/engine_schedules_test.cpp
  tests/router_test.cpp
  tests/engine_routing_test.cpp
  tests/clock_test.cpp
  tests/bounded_queue_test.cpp
  tests/engine_loop_test.cpp
)
target_link_libraries(engine_tests PRIVATE slipstream_service GTest::gtest_main Threads::Threads)
target_compile_options(engine_tests PRIVATE ${SLIPSTREAM_WARNINGS})
gtest_discover_tests(engine_tests)
