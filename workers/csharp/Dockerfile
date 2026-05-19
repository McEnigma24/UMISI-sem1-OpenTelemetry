# Jak workers/rust: builder z volumenem (./docker_build.sh), runner tylko COPY publish-docker z kontekstu.
#
#   cd workers/csharp && ./docker_build.sh
#
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS sdk-base
WORKDIR /workspace
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    NUGET_PACKAGES=/workspace/.nuget_packages

FROM sdk-base AS builder
# Pełne czyszczenie: przy zmianie AssemblyName (np. client_csharp → worker_csharp) zostające
# ``obj/`` / ``publish-docker/`` na bind-mount hościa powodują MSB3030 (szukanie starych *.deps.json).
CMD ["/bin/sh", "-c", "sleep 1 && rm -rf /workspace/bin /workspace/obj /workspace/publish-docker && dotnet publish /workspace/OtelDemo.csproj -c Release -o /workspace/publish-docker --verbosity minimal"]

FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runner
WORKDIR /app
COPY publish-docker/ .
ENV ASPNETCORE_URLS=http://0.0.0.0:8080
CMD ["/bin/sh", "-c", "sleep 2 && exec dotnet worker_csharp.dll"]
