FROM nvidia/cuda:10.1-base-ubuntu18.04
LABEL maintainer "Luka Cehovin Zajc <luka.cehovin@fri.uni-lj.si>"

RUN apt-get update --fix-missing && \
    apt-get install -y python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/code

ADD patroller /code/patroller
ADD requirements.txt /code/

RUN pip3 install -r /code/requirements.txt

EXPOSE 80

ENTRYPOINT ["python3", "-m", "patroller" ]

