FROM nvidia/cuda:10.1-base-ubuntu18.04
LABEL maintainer "Luka Cehovin Zajc <luka.cehovin@fri.uni-lj.si>"

RUN apt-get update --fix-missing && \
    apt-get install -y python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install Flask docker cachetools