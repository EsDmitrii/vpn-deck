FROM ghcr.io/steamdeckhomebrew/holo-base:latest

RUN pacman -Sy --noconfirm && \
    pacman -S --noconfirm go git base-devel libmnl && \
    pacman -Scc --noconfirm

ARG AMNEZIAWG_GO_REF=v3.1.20260828
ARG AMNEZIAWG_TOOLS_REF=v3.1.20260812

RUN git clone --depth 1 --branch "${AMNEZIAWG_GO_REF}" https://github.com/amnezia-vpn/amneziawg-go.git /tmp/amneziawg-go && \
    make -C /tmp/amneziawg-go

RUN git clone --depth 1 --branch "${AMNEZIAWG_TOOLS_REF}" https://github.com/amnezia-vpn/amneziawg-tools.git /tmp/amneziawg-tools && \
    make -C /tmp/amneziawg-tools/src

RUN mkdir /binaries && \
    cp /tmp/amneziawg-go/amneziawg-go /binaries/ && \
    cp /tmp/amneziawg-tools/src/wg /binaries/awg
