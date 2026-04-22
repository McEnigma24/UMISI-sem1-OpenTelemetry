#!/bin/bash

clear

# THIS one works the same way #
# . config
source config


# # # # # # # # # # # # # # # #

var_start=""; var_end="";
SCRIPT="./run.sh"
LOG="../output/start.log"
PATH_DONE_install="${DIR_RUN_TIME_CONFIG}/DONE_installed.txt"

function install_hook()
{
    DIR_SCRIPTS="scripts"
    PATH_MY_HOOK="$DIR_SCRIPTS/formating_hook.sh"
    PATH_GIT_HOOK=".git/hooks/pre-commit"

    cp $PATH_MY_HOOK $PATH_GIT_HOOK && chmod +x $PATH_GIT_HOOK
    chmod +x $PATH_GIT_HOOK
}
function timer_start() { var_start=$(date +%s); }
function timer_end() { var_end=$(date +%s); }
timer_print()
{
    elapsed=$((var_end - var_start))
    hours=$((elapsed / 3600))
    minutes=$(( (elapsed % 3600) / 60 ))
    seconds=$((elapsed % 60))
    printf "\nProgram          - took: %02d:%02d:%02d\n" $hours $minutes $seconds
}
function install_packages()
{
    if [ -f $PATH_DONE_install ]; then return; fi

    clear_file "$PATH_LAST_ARCH_MARKER"

    # Funkcja sprawdzająca czy pakiet jest zainstalowany
    check_and_install()
    {
        PACKAGE=$1
        if ! dpkg-query -W -f='${Status}' "$PACKAGE" 2>/dev/null | grep "install ok installed" > /dev/null; then
            echo "$PACKAGE is not installed. Installing..."

            sudo apt install -y "$PACKAGE"

            if [ $? -eq 0 ]; then
                echo ""
            else
                echo -e "\nstart_all.sh - ERROR - unable to install this package: $PACKAGE\n"
                exit
            fi
        fi
    }

    # Aktualizacja listy pakietów
    sudo apt update -y > /dev/null 2>&1 && sudo apt upgrade -y > /dev/null 2>&1

    # Sprawdzanie i instalowanie każdego pakietu
    for PACKAGE in "${PACKAGES[@]}"; do
        check_and_install "$PACKAGE"
    done

    echo -ne "\n\n"
    echo "Instalation completed"
    echo "DONE" > $PATH_DONE_install
    echo -ne "\n\n"
}
function env_prep()
{
    create_dir "$DIR_INPUT"
    create_dir "$DIR_BUILD"
    create_dir "$DIR_TARGET"
    create_dir "$DIR_EXTERNAL"
    create_dir "$DIR_LOG"
    create_dir "$DIR_RUN_TIME_CONFIG"

    chmod +x scripts/*.sh

    # clear_file "$DIR_BUILD/CMakeCache.txt" # nie dało się inaczej, bo co chwila Cachował zmienne TEMPLATE___BUILD_LIBRARY i TEMPLATE___CTEST_ACTIVE,
    #                                        # nawet jeśli po zbudowaniu odtwarzałem je do poprzednich wartości

    # 1. Pętla getopts
    while getopts "ctl" opt; do
    case "$opt" in
        c)
            # just clean the env #       single makes exe -> ct cleans test, cl cleans lib
            {
                clear_dir "$DIR_BUILD"
            }
        ;;
        t)
            # Testing #
            {
                MARKER="TEST"
                [ "$( cat "$PATH_LAST_ARCH_MARKER" )" != "$MARKER" ] && clear_dir "$DIR_BUILD" && echo "$MARKER" > $PATH_LAST_ARCH_MARKER

                export FLAG_TESTING_ACTIVE="Yes"
            }
            break
        ;;
        l)
            # Lib generation #
            {
                MARKER="LIB"
                [ "$( cat "$PATH_LAST_ARCH_MARKER" )" != "$MARKER" ] && clear_dir "$DIR_BUILD" && echo "$MARKER" > $PATH_LAST_ARCH_MARKER

                export FLAG_BUILDING_LIBRARY="Yes"
            }
            break
        ;;
        \?)
        echo "Error: $0 getopts switch -$OPTARG" >&2
        exit 1
        ;;
    esac
    done

    # Usuń przetworzone opcje z listy argumentów #
    shift $((OPTIND -1))
    # echo "Pozostałe argumenty: $@"
}

#####################   START   #####################

# install_hook

env_prep "$@"

install_packages

timer_start
{
    cd scripts
    script -q -c "$SCRIPT 2>&1" /dev/null | tee $LOG
}
timer_end

timer_print
