FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive \
    NEEDRESTART_MODE=a \
    TERM=xterm-256color

WORKDIR /app

COPY config config

RUN apt-get update -y && apt-get upgrade -y

RUN apt-get install -y make
RUN apt-get install -y cmake
RUN apt-get install -y build-essential
RUN apt-get install -y gcc
RUN apt-get install -y g++
RUN apt-get install -y libstdc++-11-dev
# RUN apt-get install -y clang
RUN apt-get install -y libomp-dev
RUN apt-get install -y libbenchmark-dev
RUN apt-get install -y libcurl4-openssl-dev
RUN apt-get install -y libprotobuf-dev
RUN apt-get install -y protobuf-compiler
RUN apt-get install -y git git ca-certificates \
    && git config --global http.postBuffer 524288000 \
    && git config --global http.version HTTP/1.1

COPY . .

RUN chmod +x ./Docker_start.sh

CMD ["./Docker_start.sh"]
