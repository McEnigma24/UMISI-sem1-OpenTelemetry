#!/bin/bash

if [ "$#" -eq 0 ]; then
    exit 0
    
elif [ "$#" -eq 1 ]; then
    touch _inc/$1.h
    touch _src/$1.cpp
    
elif [ "$#" -eq 2 ]; then
    cp _inc/$2.h    _inc/$1.h
    cp _src/$2.cpp  _src/$1.cpp
fi

