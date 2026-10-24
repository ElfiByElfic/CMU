cmake_minimum_required(VERSION 3.21)
project(chainapp VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Core Network HttpServer)

add_library(chainapp STATIC
    include/chainapp/chainapp.h
    src/chainapp.cpp
)
target_include_directories(chainapp PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)
target_link_libraries(chainapp PUBLIC Qt6::Core Qt6::Network Qt6::HttpServer)

option(CHAINAPP_BUILD_EXAMPLE "Build example app" ON)
if(CHAINAPP_BUILD_EXAMPLE)
    add_executable(example-app example/main.cpp)
    target_link_libraries(example-app PRIVATE chainapp)
endif()
