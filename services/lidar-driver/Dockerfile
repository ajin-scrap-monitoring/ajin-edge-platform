FROM debian:bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates libgrpc++-dev protobuf-compiler-grpc libprotobuf-dev nlohmann-json3-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY contracts contracts
COPY services/lidar-driver services/lidar-driver
RUN cmake -S services/lidar-driver -B /build -DCMAKE_BUILD_TYPE=Release && cmake --build /build -j2 && ctest --test-dir /build --output-on-failure
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgrpc++1.51 libprotobuf32 && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/lidar-driver /usr/local/bin/lidar-driver
COPY services/lidar-driver/NOTICE /usr/share/doc/ajin-lidar/NOTICE
USER 10001:10001
ENTRYPOINT ["lidar-driver"]
