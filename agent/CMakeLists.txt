cmake_minimum_required(VERSION 3.21)
project(chain-agent VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

# QHttpServer je v Qt od 6.4
find_package(Qt6 6.4 REQUIRED COMPONENTS Core Network HttpServer)

add_executable(chain-agent
    src/main.cpp
    src/model.h
    src/catalog.h
    src/operations.h
    src/appinstance.h src/appinstance.cpp
    src/chainmanager.h src/chainmanager.cpp
    src/apiserver.h src/apiserver.cpp
)
target_link_libraries(chain-agent PRIVATE Qt6::Core Qt6::Network Qt6::HttpServer)

install(TARGETS chain-agent RUNTIME DESTINATION bin)
install(FILES config/agent.example.json DESTINATION share/chain-agent)
install(FILES systemd/chain-agent.service DESTINATION lib/systemd/system)
