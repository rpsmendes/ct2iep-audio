# Single image used by both services (engine and web).
# Contains: JACK, zita-ajbridge, mod-host (built from source), LV2 plugins and the web backend.

# ---------- 1) build mod-host ----------
FROM debian:trixie-slim AS build
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates pkg-config python3 \
        libjack-jackd2-dev liblilv-dev lv2-dev libreadline-dev libfftw3-dev \
    && rm -rf /var/lib/apt/lists/*
ARG MOD_HOST_REF=master
RUN git clone --depth 1 --branch ${MOD_HOST_REF} https://github.com/mod-audio/mod-host.git /src/mod-host \
    && make -C /src/mod-host -j"$(nproc)"

# ---------- 2) build the RNNoise noise suppression LV2 (optional) ----------
# werman/noise-suppression-for-voice (GPL-3.0, RNNoise model), LV2 mono only.
# Not packaged in Debian. Built with JUCE, which needs the X11/freetype headers
# here (build stage only). If the build fails the image is still built, without
# this plugin: the "Noise suppression" block then says "plugin not installed".
FROM debian:trixie-slim AS rnnoise
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git ca-certificates pkg-config \
        libx11-dev libxrandr-dev libxinerama-dev libxcursor-dev libxext-dev \
        libfreetype-dev libfontconfig-dev libasound2-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
ARG RNNOISE_REF=9c4e5c28d8950e2cef837d8a0abd36c2fd9b5c2d
RUN mkdir -p /out \
    && { git init -q /src/rnnoise \
         && git -C /src/rnnoise fetch -q --depth 1 https://github.com/werman/noise-suppression-for-voice "${RNNOISE_REF}" \
         && git -C /src/rnnoise checkout -q FETCH_HEAD \
         && cmake -S /src/rnnoise -B /src/rnnoise/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF \
              -DBUILD_LV2_PLUGIN=ON -DBUILD_VST_PLUGIN=OFF -DBUILD_VST3_PLUGIN=OFF \
              -DBUILD_LADSPA_PLUGIN=OFF -DBUILD_AU_PLUGIN=OFF -DBUILD_AUV3_PLUGIN=OFF \
         && cmake --build /src/rnnoise/build \
         && strip --strip-unneeded /src/rnnoise/build/bin/rnnoise_mono.lv2/*.so \
         && cp -r /src/rnnoise/build/bin/rnnoise_mono.lv2 /out/ \
         || echo "WARNING: RNNoise LV2 build failed; the image is built without it"; } \
    && rm -rf /src/rnnoise

# ---------- 3) final image ----------
FROM debian:trixie-slim
ARG DEBIAN_FRONTEND=noninteractive
RUN echo "jackd2 jackd/tweak_rt_limits boolean false" | debconf-set-selections \
    && apt-get update && apt-get install -y --no-install-recommends \
        jackd2 jack-example-tools zita-ajbridge alsa-utils \
        liblilv-0-0 liblilv-dev libreadline8t64 libfftw3-single3 libfftw3-double3 \
        lsp-plugins-lv2 calf-plugins x42-plugins dragonfly-reverb-lv2 \
        python3 python3-lilv python3-aiohttp python3-yaml python3-numpy python3-jack-client \
        libfreetype6 libatomic1 \
        procps tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/mod-host/mod-host /usr/local/bin/mod-host
COPY --from=build /src/mod-host/*.so /usr/local/lib/mod-host/
# RNNoise LV2 bundle (empty if its build failed)
COPY --from=rnnoise /out/ /usr/local/lib/lv2/

COPY docker/engine.sh /usr/local/bin/engine.sh
COPY app /app
# Build-time checks (fail here rather than at startup):
#  - jack_lsp comes from jack-example-tools (engine.sh uses it to wait for JACK)
#  - mod-host has no missing libraries
#  - python3-lilv loads "liblilv-0.so", which Debian only ships in liblilv-dev
RUN chmod +x /usr/local/bin/engine.sh \
    && command -v jack_lsp \
    && ! ldd /usr/local/bin/mod-host | grep "not found" \
    && ! ldd /usr/local/lib/lv2/*/*.so 2>/dev/null | grep "not found" \
    && python3 -c "import jack" \
    && python3 -c "import numpy, lilv; w = lilv.World(); w.load_all(); print(len(w.get_all_plugins()), 'plugins LV2')"

# Self-test of the EQ graph measurement host (informative, never fails the
# build): a +12 dB bell at 1 kHz in the LSP EQ should print {"db": [~12.0]}
RUN echo '{"uri":"http://lsp-plug.in/plugins/lv2/para_equalizer_x8_mono","params":{"ft_0":1,"f_0":1000,"g_0":3.981,"q_0":2},"points":[1000]}' \
      | python3 /app/measure_host.py; echo " <- EQ graph self-test"

# /data/lv2/bundles: plugins installed on demand from the UI (persistent);
# /data/lv2/lib: shared libraries they need
ENV JACK_NO_AUDIO_RESERVATION=1 \
    LV2_PATH=/usr/lib/lv2:/usr/local/lib/lv2:/data/lv2/bundles \
    EXTRA_LV2_ROOT=/data/lv2 \
    LD_LIBRARY_PATH=/data/lv2/lib
ENTRYPOINT ["/usr/bin/tini", "--"]
