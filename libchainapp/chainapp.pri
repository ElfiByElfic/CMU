# Pro aplikace stavěné přes qmake:  include(/cesta/k/libchainapp/chainapp.pri)
QT += core network httpserver
CONFIG += c++17
INCLUDEPATH += $$PWD/include
HEADERS += $$PWD/include/chainapp/chainapp.h
SOURCES += $$PWD/src/chainapp.cpp
