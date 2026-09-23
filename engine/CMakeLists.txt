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
  src/twap.cpp
  src/risk.cpp
)
target_include_directories(slipstream_core PUBLIC src)
target_compile_options(slipstream_core PRIVATE ${SLIPSTREAM_WARNINGS})

enable_testing()
find_package(GTest REQUIRED)
include(GoogleTest)

add_executable(engine_tests
  tests/order_book_test.cpp
  tests/twap_test.cpp
  tests/risk_test.cpp
)
target_link_libraries(engine_tests PRIVATE slipstream_core GTest::gtest_main)
target_compile_options(engine_tests PRIVATE ${SLIPSTREAM_WARNINGS})
gtest_discover_tests(engine_tests)
