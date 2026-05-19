#!/bin/bash

remove_single_file() { [ -f "$1" ] && rm "$1"; }
clear_dir_only_specific_extension() { if ls $1/*$2 1> /dev/null 2>&1; then rm -f $1/*$2; fi }

function clean_env() { cd $DIR_ROOT; echo -e "\nBuild (1/5) - Cleaning env"; clear_dir "$DIR_TARGET"; clear_dir_only_specific_extension "$DIR_BUILD" ".exe"; }
function prep_env()  { cd $DIR_ROOT; echo -e "\nBuild (2/5) - Preparing env"; }
function build_all()
{
    cd $DIR_ROOT; echo -e "\nBuild (3/5) - Building";

    CMAKE_FLAGS=""
    
    if [ "$FLAG_TESTING_ACTIVE" == "Yes" ]; then
    {
        CMAKE_FLAGS="$CMAKE_FLAGS -DTEMPLATE___CTEST_ACTIVE=ON"
    }
    fi
    if [ "$FLAG_BUILDING_LIBRARY" == "Yes" ]; then
    {
        CMAKE_FLAGS="$CMAKE_FLAGS -DTEMPLATE___BUILD_LIBRARY=ON"
    }
    fi

    cmake -S . -B $DIR_BUILD $CMAKE_FLAGS;
    cmake --build $DIR_BUILD;

    if [ $? -eq 0 ]; then
        echo ""
    else
        echo -e "\nproduction.sh - ERROR - unable to BUILD\n"
        exit
    fi
}
function run_tests()
{
    cd $DIR_ROOT; echo -ne "\nBuild (4/5) - Testing"; cd $DIR_BUILD;

    if [ "$FLAG_TESTING_ACTIVE" == "Yes" ]; then
    {
        echo -e " ✅\n";
        if ! ctest --rerun-failed --output-on-failure; then exit; fi
    }
    else
    {
        echo -e " ❌\n";
    }
    fi
}
function copy_exe()
{
    cd $DIR_ROOT; echo -ne "\nBuild (5/5) - Copying to exe";
    
    if [ "$FLAG_BUILDING_LIBRARY" != "Yes" ]; then
    {
        echo -e " ✅\n";
        cp $DIR_BUILD/*.exe $DIR_TARGET;
    }
    else
    {
        echo -e " ❌\n";
    }
    fi
}


# START #

clean_env

prep_env

build_all

run_tests

copy_exe
